"""Load and validate the invented Phase 1 precedent corpus."""

from __future__ import annotations

import json
from pathlib import Path

from fundermatch.precedent.schema import DecidedLoanCase


def load_cases(path: str | Path) -> tuple[DecidedLoanCase, ...]:
    source = Path(path)
    cases = tuple(
        DecidedLoanCase.model_validate(json.loads(line))
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not cases:
        raise ValueError(f"synthetic precedent corpus is empty: {source}")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("synthetic precedent corpus contains duplicate case_id values")
    return cases
