"""Writes the API contract (OpenAPI) to frontends/react/openapi.json – the source of the React front end's types.

Run after every API change, then `npm run gen:api` in frontends/react. tests/test_api_contract.py fails while the
committed contract differs from the API, so a contract change never goes unnoticed by the React front end."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTRACT = ROOT / "frontends" / "react" / "openapi.json"


def contract() -> str:
    from app.api.main import create_app

    return json.dumps(create_app().openapi(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    CONTRACT.write_text(contract(), encoding="utf-8")
    print(f"API contract written to {CONTRACT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
