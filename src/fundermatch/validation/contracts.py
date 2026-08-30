"""Validate local consumer models against FinDocIQ's public contract bundle."""

from __future__ import annotations

import difflib
import json
from typing import Any

from pydantic import BaseModel

from fundermatch.clients.findociq_contract import (
    ExtractRequest,
    ExtractResponse,
    IngestBatchRequest,
    IngestBatchResponse,
    IngestDocumentRequest,
    IngestDocumentResponse,
    IngestionActivityResponse,
    ProductionExtractRequest,
    RetentionDeleteRequest,
    RetentionDeleteResponse,
)

MODELS: dict[str, type[BaseModel]] = {
    model.__name__: model
    for model in (
        ExtractRequest,
        ProductionExtractRequest,
        ExtractResponse,
        IngestDocumentRequest,
        IngestDocumentResponse,
        IngestBatchRequest,
        IngestBatchResponse,
        IngestionActivityResponse,
        RetentionDeleteRequest,
        RetentionDeleteResponse,
    )
}


def normalize_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_schema(item)
            for key, item in sorted(value.items())
            if key not in {"description", "title"}
        }
    if isinstance(value, list):
        return [normalize_schema(item) for item in value]
    return value


def consumer_schemas() -> dict[str, object]:
    return {
        name: normalize_schema(model.model_json_schema())
        for name, model in sorted(MODELS.items())
    }


def validate_contract(contract: dict[str, object]) -> tuple[str, ...]:
    failures: list[str] = []
    if contract.get("bundle_version") != "1.0":
        failures.append("unsupported FinDocIQ contract bundle version")
    if contract.get("supported_contract_versions") != ["1.0", "2.0"]:
        failures.append("FinDocIQ must publish development v1.0 and production v2.0")
    producer = contract.get("schemas")
    if not isinstance(producer, dict):
        return (*failures, "contract bundle has no schema mapping")
    consumer = consumer_schemas()
    for name, expected in producer.items():
        actual = consumer.get(name)
        if actual is None:
            failures.append(f"consumer model is missing: {name}")
            continue
        if actual != expected:
            expected_text = json.dumps(expected, indent=2, sort_keys=True).splitlines()
            actual_text = json.dumps(actual, indent=2, sort_keys=True).splitlines()
            diff = "\n".join(
                difflib.unified_diff(
                    expected_text,
                    actual_text,
                    fromfile=f"FinDocIQ/{name}",
                    tofile=f"FunderMatch/{name}",
                    lineterm="",
                )
            )
            failures.append(f"schema mismatch for {name}:\n{diff}")
    unexpected = sorted(set(consumer).difference(producer))
    if unexpected:
        failures.append(f"producer bundle is missing consumer schemas: {', '.join(unexpected)}")
    return tuple(failures)
