"""Load funder eligibility policies through a typed YAML boundary."""

from __future__ import annotations

from pathlib import Path

import yaml

from fundermatch.rules.schema import FunderPolicy, PolicySet


def load_policies(path: str | Path) -> tuple[FunderPolicy, ...]:
    source = Path(path)
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    return PolicySet.model_validate(document).policies
