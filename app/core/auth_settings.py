"""Sign-in settings (section "auth" of data/env_<env>.json). Separate from app/core/auth.py so the runtime config can
load them without importing the security module (which itself reads the runtime config)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuthSettings(BaseModel):
    """Validated, bounded sign-in settings."""
    model_config = ConfigDict(extra="ignore", frozen=True)

    max_failed_attempts: int = Field(5, ge=1, le=100)
    lockout_minutes: float = Field(5.0, ge=0.5, le=1_440)
    session_timeout_minutes: float = Field(30.0, ge=1, le=1_440)
    # all accounts together: each attempt costs a full password hash, so a flood of sign-ins can't exhaust the CPU
    max_attempts_per_minute: int = Field(60, ge=5, le=10_000)
