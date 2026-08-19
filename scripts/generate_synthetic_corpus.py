"""Generate the deterministic, wholly invented Phase 1 precedent corpus."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fundermatch.clients.findociq_contract import BoundingBox, SourceCitation
from fundermatch.precedent.schema import (
    DecidedLoanCase,
    DecisionOutcome,
    EvidenceMetric,
    FinancialProfile,
    HumanDecision,
    ReviewerComment,
)

OUTPUT = Path("data/synthetic_decided_loans.jsonl")

RAW_CASES = (
    (
        "Aster Forge Components",
        "Auto components",
        "West",
        184,
        28,
        14.2,
        1.72,
        2.1,
        1.45,
        12,
        218,
        "approved",
        "northstar-capital",
    ),
    (
        "Bluehaven Foods",
        "Food processing",
        "South",
        96,
        18,
        11.8,
        1.41,
        2.8,
        1.22,
        8,
        146,
        "approved_with_conditions",
        "harborline-credit",
    ),
    (
        "Cedar Loom Textiles",
        "Textiles",
        "West",
        132,
        24,
        8.4,
        1.08,
        4.2,
        0.94,
        16,
        305,
        "rejected",
        "meridian-growth-finance",
    ),
    (
        "Delta Grid Controls",
        "Electrical equipment",
        "South",
        248,
        42,
        17.6,
        1.93,
        1.7,
        1.61,
        11,
        192,
        "approved",
        "cobalt-infrastructure-fund",
    ),
    (
        "Everbrook Diagnostics",
        "Healthcare services",
        "North",
        74,
        15,
        19.1,
        1.56,
        2.0,
        1.18,
        7,
        124,
        "approved",
        "northstar-capital",
    ),
    (
        "Fircrest Packaging",
        "Packaging",
        "West",
        158,
        31,
        12.7,
        1.34,
        3.1,
        1.33,
        14,
        267,
        "approved_with_conditions",
        "harborline-credit",
    ),
    (
        "Glenrock Ceramics",
        "Building materials",
        "North",
        205,
        39,
        9.2,
        1.12,
        3.9,
        1.08,
        18,
        336,
        "rejected",
        "cobalt-infrastructure-fund",
    ),
    (
        "Highfield Cold Chain",
        "Logistics",
        "East",
        119,
        26,
        15.4,
        1.67,
        2.4,
        1.52,
        9,
        173,
        "approved",
        "meridian-growth-finance",
    ),
    (
        "Indigo Water Systems",
        "Industrial services",
        "South",
        88,
        17,
        13.6,
        1.48,
        2.6,
        1.27,
        10,
        118,
        "approved_with_conditions",
        "northstar-capital",
    ),
    (
        "Juniper Farm Inputs",
        "Agricultural inputs",
        "Central",
        143,
        29,
        10.1,
        1.21,
        3.5,
        1.11,
        13,
        229,
        "approved_with_conditions",
        "harborline-credit",
    ),
    (
        "Keystone Meditech",
        "Medical devices",
        "West",
        267,
        46,
        21.3,
        2.04,
        1.5,
        1.74,
        15,
        284,
        "approved",
        "meridian-growth-finance",
    ),
    (
        "Lakewood Paperworks",
        "Paper products",
        "East",
        111,
        23,
        7.9,
        0.96,
        4.6,
        0.86,
        20,
        312,
        "rejected",
        "cobalt-infrastructure-fund",
    ),
    (
        "Meadowlane Solar Parts",
        "Renewable components",
        "South",
        176,
        36,
        16.8,
        1.79,
        2.2,
        1.46,
        6,
        155,
        "approved",
        "northstar-capital",
    ),
    (
        "Northwind Precision Tools",
        "Engineering",
        "West",
        221,
        38,
        18.2,
        1.88,
        1.9,
        1.58,
        17,
        241,
        "approved",
        "harborline-credit",
    ),
    (
        "Oakridge Dairy Products",
        "Dairy processing",
        "North",
        127,
        27,
        9.8,
        1.19,
        3.7,
        1.04,
        9,
        198,
        "approved_with_conditions",
        "meridian-growth-finance",
    ),
    (
        "Pinecrest Safety Glass",
        "Specialty glass",
        "West",
        193,
        35,
        13.1,
        1.52,
        2.9,
        1.36,
        12,
        276,
        "approved",
        "cobalt-infrastructure-fund",
    ),
    (
        "Quartzline Data Services",
        "Business services",
        "South",
        68,
        14,
        24.6,
        1.83,
        1.3,
        0.72,
        5,
        109,
        "approved_with_conditions",
        "northstar-capital",
    ),
    (
        "Riverbend Pumps",
        "Industrial machinery",
        "Central",
        154,
        32,
        14.9,
        1.61,
        2.5,
        1.49,
        19,
        263,
        "approved",
        "harborline-credit",
    ),
    (
        "Silverfern Homeware",
        "Consumer products",
        "East",
        102,
        21,
        6.8,
        0.91,
        4.9,
        0.81,
        11,
        187,
        "rejected",
        "meridian-growth-finance",
    ),
    (
        "Timberline Biofuels",
        "Bioenergy",
        "Central",
        236,
        44,
        15.7,
        1.69,
        2.3,
        1.63,
        8,
        214,
        "approved_with_conditions",
        "cobalt-infrastructure-fund",
    ),
)


def _decimal(value: int | float) -> Decimal:
    return Decimal(str(value))


def build_case(index: int, raw: tuple[object, ...]) -> DecidedLoanCase:
    (
        borrower,
        industry,
        region,
        revenue,
        requested,
        margin,
        dscr,
        leverage,
        collateral,
        years,
        employees,
        outcome_value,
        funder_id,
    ) = raw
    case_id = f"SYN-{index:03d}"
    document_id = "synthetic-" + hashlib.sha256(case_id.encode()).hexdigest()
    profile = FinancialProfile(
        annual_revenue_crore=_decimal(revenue),
        requested_amount_crore=_decimal(requested),
        ebitda_margin_pct=_decimal(margin),
        dscr=_decimal(dscr),
        debt_to_ebitda=_decimal(leverage),
        collateral_cover=_decimal(collateral),
        years_operating=int(years),
        employee_count=int(employees),
    )
    evidence = tuple(
        EvidenceMetric(
            name=name,
            value=value,
            unit=unit,
            period="FY2025",
            citation=SourceCitation(
                document_id=document_id,
                page_number=page,
                bbox=BoundingBox(x0=42.0, y0=100.0 + page, x1=554.0, y1=124.0 + page),
            ),
        )
        for name, value, unit, page in (
            ("annual_revenue_crore", profile.annual_revenue_crore, "INR crore", 4),
            ("ebitda_margin_pct", profile.ebitda_margin_pct, "percent", 5),
            ("dscr", profile.dscr, "ratio", 6),
        )
    )
    created_at = datetime(2025, 4, 1, tzinfo=UTC) + timedelta(days=index)
    comments = (
        ReviewerComment(
            team="finance",
            author=f"synthetic-finance-{(index % 3) + 1}",
            text=(
                f"Reviewed DSCR of {profile.dscr} and debt-to-EBITDA of "
                f"{profile.debt_to_ebitda}; figures reconcile to the synthetic pack."
            ),
            created_at=created_at,
        ),
        ReviewerComment(
            team="operations",
            author=f"synthetic-ops-{(index % 3) + 1}",
            text=(
                f"Confirmed {profile.years_operating} years of operating history and "
                f"a synthetic workforce of {profile.employee_count}."
            ),
            created_at=created_at + timedelta(hours=2),
        ),
    )
    outcome = DecisionOutcome(str(outcome_value))
    conditions = (
        ("Quarterly covenant reporting", "No additional senior debt without consent")
        if outcome is DecisionOutcome.APPROVED_WITH_CONDITIONS
        else ()
    )
    rationale = {
        DecisionOutcome.APPROVED: "Human committee accepted the documented risk-return profile.",
        DecisionOutcome.REJECTED: "Human committee declined after reviewing weak coverage metrics.",
        DecisionOutcome.APPROVED_WITH_CONDITIONS: (
            "Human committee accepted the case subject to explicit monitoring conditions."
        ),
    }[outcome]
    return DecidedLoanCase(
        case_id=case_id,
        borrower_name=str(borrower),
        industry=str(industry),
        region=str(region),
        profile=profile,
        evidence=evidence,
        comments=comments,
        decision=HumanDecision(
            outcome=outcome,
            funder_id=str(funder_id),
            decided_by=f"synthetic-committee-{(index % 4) + 1}",
            decided_at=created_at + timedelta(days=7),
            rationale=rationale,
            conditions=conditions,
        ),
    )


def main() -> None:
    cases = tuple(build_case(index, raw) for index, raw in enumerate(RAW_CASES, start=1))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(case.model_dump_json() for case in cases) + "\n"
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {len(cases)} invented cases to {OUTPUT}")


if __name__ == "__main__":
    main()
