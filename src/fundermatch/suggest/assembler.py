"""Deterministically assemble eligible precedents into human-review evidence."""

from __future__ import annotations

from decimal import Decimal

from fundermatch.matching.schema import PrecedentMatch, RuleGatedRetrievalResult
from fundermatch.precedent.schema import DecidedLoanCase
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy
from fundermatch.suggest.schema import (
    AdvisoryCandidate,
    ExcludedFunder,
    ExplainedPrecedent,
    SimilarityFactor,
    SuggestionBundle,
)

ADVISORY_NOTICE = (
    "This output summarizes deterministic policy checks and historical synthetic "
    "precedents. It is not an approval, rejection, or recommendation. A human reviewer "
    "must make and record the authoritative decision."
)


class SuggestionAssembler:
    def assemble(
        self,
        application: BorrowerApplication,
        policies: tuple[FunderPolicy, ...],
        retrieval: RuleGatedRetrievalResult,
    ) -> SuggestionBundle:
        if retrieval.application_id != application.application_id:
            raise ValueError("retrieval result belongs to a different application")
        policies_by_id = {policy.funder_id: policy for policy in policies}
        if len(policies_by_id) != len(policies):
            raise ValueError("funder policies contain duplicate identifiers")
        eligibility_by_id = {item.funder_id: item for item in retrieval.eligibility}
        if set(eligibility_by_id) != set(policies_by_id):
            raise ValueError("retrieval eligibility does not cover the configured policies")

        matches_by_funder: dict[str, list[PrecedentMatch]] = {}
        for match in retrieval.matches:
            funder_id = match.precedent.decision.funder_id
            eligibility = eligibility_by_id.get(funder_id)
            if eligibility is None or not eligibility.eligible:
                raise ValueError("retrieval contains an ineligible funder precedent")
            matches_by_funder.setdefault(funder_id, []).append(match)

        candidates = []
        excluded = []
        for policy in policies:
            eligibility = eligibility_by_id[policy.funder_id]
            if not eligibility.eligible:
                excluded.append(
                    ExcludedFunder(
                        funder_id=policy.funder_id,
                        display_name=policy.display_name,
                        failed_checks=tuple(
                            check for check in eligibility.checks if not check.passed
                        ),
                    )
                )
                continue
            matches = tuple(matches_by_funder.get(policy.funder_id, ()))
            explained = tuple(
                ExplainedPrecedent(
                    match=match,
                    factors=self._explain(application, match.precedent),
                )
                for match in matches
            )
            summary = (
                f"{policy.display_name} passed all {len(eligibility.checks)} hard checks. "
                f"{len(explained)} close synthetic precedent(s) are shown for human review."
                if explained
                else (
                    f"{policy.display_name} passed all {len(eligibility.checks)} hard checks, "
                    "but no precedent exceeded the configured similarity threshold."
                )
            )
            candidates.append(
                AdvisoryCandidate(
                    funder_id=policy.funder_id,
                    display_name=policy.display_name,
                    passed_checks=eligibility.checks,
                    precedents=explained,
                    evidence_summary=summary,
                    no_close_precedent=not explained,
                )
            )
        return SuggestionBundle(
            advisory_notice=ADVISORY_NOTICE,
            application=application,
            candidates=tuple(candidates),
            excluded_funders=tuple(excluded),
        )

    @staticmethod
    def _explain(
        application: BorrowerApplication, precedent: DecidedLoanCase
    ) -> tuple[SimilarityFactor, ...]:
        current = application.profile
        historical = precedent.profile
        return (
            SimilarityFactor(
                metric="industry",
                application_value=application.industry,
                precedent_value=precedent.industry,
                observation=(
                    "same industry"
                    if application.industry == precedent.industry
                    else "different industry"
                ),
            ),
            SimilarityFactor(
                metric="region",
                application_value=application.region,
                precedent_value=precedent.region,
                observation=(
                    "same region" if application.region == precedent.region else "different region"
                ),
            ),
            _numeric_factor(
                "annual_revenue_crore",
                current.annual_revenue_crore,
                historical.annual_revenue_crore,
            ),
            _numeric_factor(
                "requested_amount_crore",
                current.requested_amount_crore,
                historical.requested_amount_crore,
            ),
            _numeric_factor("dscr", current.dscr, historical.dscr),
            _numeric_factor(
                "debt_to_ebitda", current.debt_to_ebitda, historical.debt_to_ebitda
            ),
            _numeric_factor(
                "collateral_cover", current.collateral_cover, historical.collateral_cover
            ),
        )


def _numeric_factor(metric: str, current: Decimal, historical: Decimal) -> SimilarityFactor:
    difference = current - historical
    return SimilarityFactor(
        metric=metric,
        application_value=str(current),
        precedent_value=str(historical),
        observation=f"application minus precedent = {difference:+}",
    )
