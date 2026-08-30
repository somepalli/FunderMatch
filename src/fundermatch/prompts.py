"""Typed, content-addressed boundary for repository-owned prompt files."""

import json
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class PromptIdentity(BaseModel):
    """Safe prompt metadata; the template body is intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,119}$")
    version: str = Field(pattern=r"^[0-9a-f]{12}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def load_prompt(name: str, root: Path = Path("prompts")) -> str:
    if not name or Path(name).name != name:
        raise ValueError("unsafe prompt name")
    prompt = (root / f"{name}.txt").read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"prompt {name!r} is empty")
    return prompt


def identify_prompt(path: Path) -> PromptIdentity:
    template = path.read_text(encoding="utf-8").strip()
    if not template:
        raise ValueError(f"prompt {path.name!r} is empty")
    digest = sha256(template.encode("utf-8")).hexdigest()
    return PromptIdentity(template_id=path.stem, version=digest[:12], sha256=digest)


def prompt_config_hash(config: BaseModel, identity: PromptIdentity) -> str:
    """Hash model settings plus prompt content, never its path or body."""
    payload = config.model_dump(mode="json", exclude={"prompt_path"})
    payload["prompt_sha256"] = identity.sha256
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()
