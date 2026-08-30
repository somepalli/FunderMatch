from copy import deepcopy

from fundermatch.validation.contracts import consumer_schemas, validate_contract


def _contract() -> dict[str, object]:
    return {
        "bundle_version": "1.0",
        "supported_contract_versions": ["1.0", "2.0"],
        "schemas": consumer_schemas(),
    }


def test_contract_validator_accepts_matching_v1_and_v2_models() -> None:
    assert validate_contract(_contract()) == ()


def test_contract_validator_reports_producer_consumer_drift() -> None:
    contract = deepcopy(_contract())
    schemas = contract["schemas"]
    assert isinstance(schemas, dict)
    extract = schemas["ProductionExtractRequest"]
    assert isinstance(extract, dict)
    properties = extract["properties"]
    assert isinstance(properties, dict)
    properties["new_required_field"] = {"type": "string"}
    required = extract["required"]
    assert isinstance(required, list)
    required.append("new_required_field")
    failures = validate_contract(contract)
    assert any("ProductionExtractRequest" in item for item in failures)
