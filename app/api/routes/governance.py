"""Governance rules owned by the PMO Head: business rules (verdict thresholds) and the Data Steward's required
fields. Every PMO account reads them, only the PMO Head changes them; the System Admin sees them read-only in the
Admin Console. The core checks the role again (update_runtime_config)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.api import admin_schemas as schemas
from app.api.admin_common import Confirmation, require_confirm, respond
from app.api.deps import pmo, pmo_head
from app.api.routes import admin_settings
from app.core.auth import User
from app.core.business_rules import RULE_LABELS
from app.views import admin as views

router = APIRouter(prefix="/governance", tags=["governance"], dependencies=[Depends(pmo)])


class RequiredFields(BaseModel):
    fields: list[str] = Field(max_length=500)


class RuleValues(BaseModel):
    values: dict[str, float]  # rule name -> value; rules left out keep theirs. Bounds are checked by the rules model


# =========================================================================== required fields
@router.get("/required-fields")
def required_fields() -> schemas.RequiredFieldsView:
    return admin_settings.required_fields()


@router.put("/required-fields")
def save_required_fields(body: RequiredFields, response: Response,
                         user: User = Depends(pmo_head)) -> schemas.RequiredFieldsSaved:
    return respond(views.save_required(body.fields, user.user_id, user.role, user.pmo_head), response,
                   schemas.RequiredFieldsSaved, data_changed=True)


@router.post("/required-fields/reset")
def reset_required_fields(body: Confirmation, response: Response, user: User = Depends(pmo_head)) -> schemas.ActionResult:
    require_confirm(body)
    return respond(views.reset_required(user.user_id, user.role, user.pmo_head), response, data_changed=True)


# =========================================================================== business rules
@router.get("/business-rules")
def business_rules() -> schemas.BusinessRulesView:
    return admin_settings.business_rules()


@router.put("/business-rules")
def save_business_rules(body: RuleValues, response: Response, user: User = Depends(pmo_head)) -> schemas.RulesSaved:
    unknown = sorted(set(body.values) - set(RULE_LABELS))
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown business rules: {', '.join(unknown)}.")
    values = {k: int(v) if k in views.INT_RULES and float(v).is_integer() else v for k, v in body.values.items()}
    return respond(views.save_business_rules(values, user.user_id, user.role, user.pmo_head), response,
                   schemas.RulesSaved, data_changed=True)


@router.post("/business-rules/reset")
def reset_business_rules(body: Confirmation, response: Response, user: User = Depends(pmo_head)) -> schemas.RulesSaved:
    require_confirm(body)
    return respond(views.reset_business_rules(user.user_id, user.role, user.pmo_head), response, schemas.RulesSaved,
                   data_changed=True)
