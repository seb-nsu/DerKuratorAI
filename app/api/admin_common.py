"""Shared pieces of the admin routes: the confirm flag of destructive actions, the answer of an admin action and the
renaming of the console's display rows to API field names."""
from __future__ import annotations

import json
from typing import TypeVar

from fastapi import HTTPException, Response, status
from pydantic import BaseModel

from app.api.admin_schemas import ActionResult
from app.api.deps import clear_data_cache
from app.views.admin import Outcome

Result = TypeVar("Result", bound=ActionResult)


class Confirmation(BaseModel):
    confirm: bool = False  # destructive or settings-discarding actions run only with confirm = true


def require_confirm(body: Confirmation) -> None:
    if not body.confirm:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "This action discards data or settings – send confirm: true to run it.")


def respond(outcome: Outcome, response: Response, model: type[Result] = ActionResult,
            data_changed: bool = False) -> Result:
    """success/info/warning answer 200; an error 422 (invalid input) or 409 with the same body. `data_changed` drops
    the API's data cache after a successful action that changed the portfolio data or its validation."""
    if outcome.ok and (data_changed or outcome.data.get("reseeded")):
        clear_data_cache()
    if not outcome.ok:
        response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT if outcome.invalid else status.HTTP_409_CONFLICT
    data = json.loads(json.dumps(outcome.data, default=str)) if outcome.ok else {}
    return model.model_validate({**data, "kind": outcome.kind, "message": outcome.message})


def renamed(rows: list[dict], names: dict[str, str]) -> list[dict]:
    """The console's display rows (column titles as keys) with API field names."""
    return [{names.get(k, k): v for k, v in row.items()} for row in rows]
