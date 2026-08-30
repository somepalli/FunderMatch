"""Optional Gemma 3 narrative adapter for already-assembled advisory evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from fundermatch.prompts import PromptIdentity, identify_prompt, prompt_config_hash
from fundermatch.suggest.schema import AdvisoryCandidate

PROHIBITED_AUTHORITY = re.compile(
    r"\b(?:should|must|recommend(?:s|ed|ation)?)\s+(?:be\s+)?(?:approve|approved|reject|rejected)\b"
    r"|\b(?:approve|reject)\s+(?:this|the)\s+application\b",
    flags=re.IGNORECASE,
)
SENSITIVE_OUTPUT = re.compile(
    r"\b[A-Z]{5}[0-9]{4}[A-Z]\b|\b[A-Z]{4}0[A-Z0-9]{6}\b|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    flags=re.IGNORECASE,
)
AUTHORITY_OUTPUT = re.compile(
    r"\b(?:automatically\s+(?:approved|rejected)|guaranteed\s+approval|we\s+(?:approve|reject))\b",
    re.IGNORECASE,
)


class GemmaNarrativeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = "http://127.0.0.1:8900/v1"
    model_id: str = "google/gemma-3-12b-it"
    revision: str = "3b0c67b98eee8fb90633ef1bfbf3d39f43b9cf9d"
    temperature: Literal[0.0] = 0.0
    seed: int = 17
    max_tokens: int = Field(default=500, ge=64, le=2000)
    prompt_path: Path = Path("prompts/suggestion_narrative_system.txt")
    production_guardrails_enabled: bool = False


class GeneratedNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    similarities: tuple[str, ...] = Field(min_length=1, max_length=8)
    differences: tuple[str, ...] = Field(min_length=1, max_length=8)
    caveat: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def prohibit_decision_authority(self) -> GeneratedNarrative:
        combined = " ".join(
            (self.summary, *self.similarities, *self.differences, self.caveat)
        )
        if PROHIBITED_AUTHORITY.search(combined):
            raise ValueError("narrative attempted to exercise decision authority")
        return self


class NarrativePrecedentFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    similarity_score: float
    historical_outcome: str
    comparisons: tuple[str, ...]
    finance_comment: str
    operations_comment: str


class NarrativeFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    funder_id: str
    display_name: str
    passed_rule_count: int
    no_close_precedent: bool
    precedents: tuple[NarrativePrecedentFacts, ...] = Field(max_length=2)

    @classmethod
    def from_candidate(cls, candidate: AdvisoryCandidate) -> NarrativeFacts:
        precedents = []
        for explained in candidate.precedents[:2]:
            precedent = explained.match.precedent
            comments = {comment.team: comment.text for comment in precedent.comments}
            precedents.append(
                NarrativePrecedentFacts(
                    case_id=precedent.case_id,
                    similarity_score=explained.match.score,
                    historical_outcome=precedent.decision.outcome.value,
                    comparisons=tuple(
                        f"{factor.metric}: {factor.observation}"
                        for factor in explained.factors
                    ),
                    finance_comment=comments["finance"],
                    operations_comment=comments["operations"],
                )
            )
        return cls(
            funder_id=candidate.funder_id,
            display_name=candidate.display_name,
            passed_rule_count=len(candidate.passed_checks),
            no_close_precedent=candidate.no_close_precedent,
            precedents=tuple(precedents),
        )


class NarrativeRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    revision: str
    temperature: Literal[0.0]
    seed: int
    prompt_template_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,119}$")
    prompt_version: str = Field(pattern=r"^[0-9a-f]{12}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output: GeneratedNarrative


class NarrativeUnavailable(RuntimeError):
    """Raised without exposing private upstream response details."""


class GemmaNarrativeClient:
    def __init__(
        self,
        config: GemmaNarrativeConfig | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or GemmaNarrativeConfig()
        self._http_client = http_client

    async def explain(self, candidate: AdvisoryCandidate) -> NarrativeRun:
        system_prompt = self.config.prompt_path.read_text(encoding="utf-8").strip()
        identity = identify_prompt(self.config.prompt_path)
        facts = NarrativeFacts.from_candidate(candidate)
        payload = {
            "model": self.config.model_id,
            "temperature": self.config.temperature,
            "seed": self.config.seed,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(facts.model_dump(mode="json"), sort_keys=True),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "fundermatch_advisory_narrative",
                    "strict": True,
                    "schema": GeneratedNarrative.model_json_schema(),
                },
            },
        }
        try:
            if self._http_client is not None:
                response = await self._http_client.post("/chat/completions", json=payload)
            else:
                async with httpx.AsyncClient(
                    base_url=self.config.base_url, timeout=120.0
                ) as client:
                    response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            output = GeneratedNarrative.model_validate_json(content)
            self._validate_grounded(output, facts)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as error:
            if self.config.production_guardrails_enabled:
                return self._fallback(facts, identity)
            raise NarrativeUnavailable("Gemma narrative generation failed") from error
        return NarrativeRun(
            model_id=self.config.model_id,
            revision=self.config.revision,
            temperature=self.config.temperature,
            seed=self.config.seed,
            prompt_template_id=identity.template_id,
            prompt_version=identity.version,
            prompt_sha256=identity.sha256,
            config_hash=prompt_config_hash(self.config, identity),
            output=output,
        )

    @staticmethod
    def _validate_grounded(output: GeneratedNarrative, facts: NarrativeFacts) -> None:
        rendered = " ".join(
            (output.summary, *output.similarities, *output.differences, output.caveat)
        )
        allowed_numbers = set(re.findall(r"[-+]?\d+(?:\.\d+)?", facts.model_dump_json()))
        produced_numbers = set(re.findall(r"[-+]?\d+(?:\.\d+)?", rendered))
        if not produced_numbers <= allowed_numbers:
            raise ValueError("narrative introduced an unsupported numeric claim")
        if SENSITIVE_OUTPUT.search(rendered):
            raise ValueError("narrative contains a protected identifier")
        if AUTHORITY_OUTPUT.search(rendered):
            raise ValueError("narrative attempted to make a lending decision")
        if "human" not in output.caveat.casefold():
            raise ValueError("narrative omitted human-decision authority")

    def _fallback(self, facts: NarrativeFacts, identity: PromptIdentity) -> NarrativeRun:
        precedent_state = (
            "Verified historical precedents are available for reviewer comparison."
            if facts.precedents
            else "No close verified precedent is available."
        )
        return NarrativeRun(
            model_id="deterministic-template",
            revision="guardrail-fallback-v1",
            temperature=0.0,
            seed=self.config.seed,
            prompt_template_id=identity.template_id,
            prompt_version=identity.version,
            prompt_sha256=identity.sha256,
            config_hash=prompt_config_hash(self.config, identity),
            output=GeneratedNarrative(
                summary=f"{facts.display_name} passed the configured deterministic checks.",
                similarities=(precedent_state,),
                differences=("Review the cited application evidence and policy checks.",),
                caveat="Decision support only; a human reviewer makes the lending decision.",
            ),
        )
