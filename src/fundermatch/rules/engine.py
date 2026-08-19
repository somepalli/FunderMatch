"""Small typed rules engine selected for deterministic Phase 2 gating.

The policy set is fixed and numeric, so a JVM-backed engine would add deployment
complexity without adding useful expressiveness. Each criterion is explicit,
independently reported, and evaluated before any vector query can be issued.
"""

from __future__ import annotations

from fundermatch.rules.schema import (
    BorrowerApplication,
    FunderEligibility,
    FunderPolicy,
    RuleCheck,
    RuleCriterion,
)


class EligibilityEngine:
    def evaluate(
        self, application: BorrowerApplication, policy: FunderPolicy
    ) -> FunderEligibility:
        profile = application.profile
        checks = (
            RuleCheck(
                criterion=RuleCriterion.INDUSTRY,
                passed=application.industry in policy.allowed_industries,
                actual=application.industry,
                requirement=f"one of {sorted(policy.allowed_industries)}",
            ),
            RuleCheck(
                criterion=RuleCriterion.REGION,
                passed=application.region in policy.allowed_regions,
                actual=application.region,
                requirement=f"one of {sorted(policy.allowed_regions)}",
            ),
            RuleCheck(
                criterion=RuleCriterion.REQUESTED_AMOUNT,
                passed=(
                    policy.min_requested_amount_crore
                    <= profile.requested_amount_crore
                    <= policy.max_requested_amount_crore
                ),
                actual=str(profile.requested_amount_crore),
                requirement=(
                    f"between {policy.min_requested_amount_crore} and "
                    f"{policy.max_requested_amount_crore} crore"
                ),
            ),
            RuleCheck(
                criterion=RuleCriterion.DSCR,
                passed=profile.dscr >= policy.min_dscr,
                actual=str(profile.dscr),
                requirement=f">= {policy.min_dscr}",
            ),
            RuleCheck(
                criterion=RuleCriterion.DEBT_TO_EBITDA,
                passed=profile.debt_to_ebitda <= policy.max_debt_to_ebitda,
                actual=str(profile.debt_to_ebitda),
                requirement=f"<= {policy.max_debt_to_ebitda}",
            ),
            RuleCheck(
                criterion=RuleCriterion.COLLATERAL_COVER,
                passed=profile.collateral_cover >= policy.min_collateral_cover,
                actual=str(profile.collateral_cover),
                requirement=f">= {policy.min_collateral_cover}",
            ),
            RuleCheck(
                criterion=RuleCriterion.OPERATING_HISTORY,
                passed=profile.years_operating >= policy.min_years_operating,
                actual=str(profile.years_operating),
                requirement=f">= {policy.min_years_operating} years",
            ),
        )
        return FunderEligibility(
            funder_id=policy.funder_id,
            eligible=all(check.passed for check in checks),
            checks=checks,
        )

    def evaluate_all(
        self,
        application: BorrowerApplication,
        policies: tuple[FunderPolicy, ...],
    ) -> tuple[FunderEligibility, ...]:
        return tuple(self.evaluate(application, policy) for policy in policies)
