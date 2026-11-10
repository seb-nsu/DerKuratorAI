"""The committed API contract (frontends/react/openapi.json) matches the API: the React front end's types are
generated from it, so an API change must regenerate it (python scripts/export_openapi.py, then npm run gen:api)."""
from __future__ import annotations

from scripts import export_openapi


def test_committed_contract_matches_the_api():
    assert export_openapi.CONTRACT.read_text(encoding="utf-8") == export_openapi.contract(), (
        "API contract changed: run `python scripts/export_openapi.py` and `npm run gen:api` in frontends/react, "
        "then update the React pages the change affects")


def _typed(schema: dict) -> bool:
    """A model reference (or a list of them), never a bare object whose fields the contract does not name."""
    if "$ref" in schema:
        return True
    if schema.get("type") == "array":
        return _typed(schema.get("items") or {})
    return any(_typed(option) for option in schema.get("anyOf", []) + schema.get("oneOf", []))


def test_every_route_declares_its_answer():
    from app.api.main import create_app

    untyped = []
    for path, operations in create_app().openapi()["paths"].items():
        for method, operation in operations.items():
            answers = operation.get("responses", {})
            answer = answers.get("200") or answers.get("201") or {}
            if "204" in answers or (answer.get("content") and "application/json" not in answer["content"]):
                continue  # no content, or a file download (its media type is declared instead)
            if not _typed(answer.get("content", {}).get("application/json", {}).get("schema") or {}):
                untyped.append(f"{method.upper()} {path}")
    assert not untyped, f"routes without a typed answer (declare a pydantic response model): {untyped}"
