"""Identity-based sign-in: local accounts with PBKDF2 password hashes (standard library only; OIDC SSO is planned).

The user file (`AUTH_USERS_FILE`, JSON, plus the optional `AUTH_EXTRA_USERS` and the admin account from
`AUTH_ADMIN_PASSWORD`) is the only source of a user's role – `authorize` / `can_approve` in the
backend stay unchanged and receive that role. Demo accounts (`data/demo_users.json`, entries with `"demo": true`)
are accepted only in DEV and TEST; PROD refuses to start the sign-in with them. An entry may carry an optional
`email`, the address of the account's notifications (until SSO supplies it).

Hash format: `pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>`; plain passwords never reach a file or the log.
Failed attempts are counted per user across all worker processes (data/state/sign_in.json): `max_failed_attempts` failures lock the account for
`lockout_minutes`; all accounts together get `max_attempts_per_minute` attempts; a session expires after `session_timeout_minutes` without activity (both in env_<env>.json,
section "auth").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import shared_state
from app.core.auth_settings import AuthSettings
from app.core.config import PROJECT_ROOT
from app.core.security import ROLE_ADMIN, ROLE_BOARD, ROLE_PM, ROLE_PMO, log_security_event

logger = logging.getLogger("derkurator.auth")

ALGORITHM = "pbkdf2_sha256"
MIN_ITERATIONS = 600_000
DEMO_USERS_FILE = PROJECT_ROOT / "data" / "demo_users.json"
DEMO_ENVIRONMENTS = ("DEV", "TEST")
ROLES = (ROLE_ADMIN, ROLE_PMO, ROLE_PM, ROLE_BOARD)


class AuthConfigError(RuntimeError):
    """Sign-in is not configured (no or invalid user file, demo accounts in PROD). Details go to the log only."""


@dataclass(frozen=True)
class User:
    user_id: str
    role: str
    display_name: str
    demo: bool = False
    password_hash: str = field(default="", repr=False, compare=False)
    email: str = field(default="", repr=False, compare=False)  # optional: notifications (app.core.notifications)
    pmo_head: bool = False  # the PMO identity that owns the governance rules (business rules, required fields)


@dataclass(frozen=True)
class SignInResult:
    ok: bool
    user: User | None = None
    message: str = ""


# =========================================================================== hashing
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, iterations: int = MIN_ITERATIONS, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("Password must not be empty.")
    if iterations < MIN_ITERATIONS:
        raise ValueError(f"At least {MIN_ITERATIONS:,} iterations are required.")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check. Unknown formats and weak hashes (< MIN_ITERATIONS) never verify."""
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        rounds = int(iterations)
        if algorithm != ALGORITHM or rounds < MIN_ITERATIONS:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), base64.b64decode(salt), rounds)
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError, AttributeError):
        return False


# =========================================================================== user file
def users_file() -> Path:
    """`AUTH_USERS_FILE` from the environment; DEV/TEST fall back to the demo accounts."""
    configured = os.getenv("AUTH_USERS_FILE", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else PROJECT_ROOT / path
    return DEMO_USERS_FILE


def extra_users() -> list[dict[str, Any]]:
    """`AUTH_EXTRA_USERS`: optional real accounts merged with the user file – e.g. the owner's own admin account on
    the hosted demo. A path to a JSON user file, or the JSON itself (for hosts that only offer secrets as environment
    variables). Its entries must not be demo accounts."""
    configured = os.getenv("AUTH_EXTRA_USERS", "").strip()
    if not configured:
        return []
    if configured.startswith("{"):
        try:
            rows = json.loads(configured)["users"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthConfigError(f"AUTH_EXTRA_USERS is not a valid user list: {exc.__class__.__name__}") from None
    else:
        path = Path(configured)
        rows = _read_rows(path if path.is_absolute() else PROJECT_ROOT / path)
    if not isinstance(rows, list) or any(not isinstance(r, dict) or r.get("demo") for r in rows):
        raise AuthConfigError("AUTH_EXTRA_USERS must list real accounts (no demo entries).")
    return rows


ADMIN_MIN_LENGTH = 12
_admin_hashes: dict[str, str] = {}  # sha256 of the configured password -> its PBKDF2 hash (hashed once per process)


def _admin_secret() -> str:
    return os.getenv("AUTH_ADMIN_PASSWORD", "")


def admin_user() -> list[dict[str, Any]]:
    """`AUTH_ADMIN_PASSWORD` (and optionally `AUTH_ADMIN_USER`, default `ADMIN`): the simple way to an admin account –
    one secret in `.env` or the host's secrets instead of a hashed user entry. The password is hashed in memory and
    never written anywhere; it must have at least ADMIN_MIN_LENGTH characters. A real account, so it works in PROD
    too."""
    password = _admin_secret()
    if not password:
        return []
    if len(password) < ADMIN_MIN_LENGTH:
        raise AuthConfigError(f"AUTH_ADMIN_PASSWORD needs at least {ADMIN_MIN_LENGTH} characters.")
    key = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if key not in _admin_hashes:
        _admin_hashes.clear()
        _admin_hashes[key] = hash_password(password)
    user_id = os.getenv("AUTH_ADMIN_USER", "").strip() or "ADMIN"
    return [{"user_id": user_id, "role": ROLE_ADMIN, "display_name": "System Admin", "password_hash": _admin_hashes[key]}]


def using_demo_users() -> bool:
    """True if sign-in runs on the shipped demo accounts (then PROD would refuse every sign-in)."""
    return users_file().resolve() == DEMO_USERS_FILE.resolve()


def _active_environment() -> str:
    """The environment the instance runs in now (the Admin Console can switch it at runtime)."""
    from app.core.runtime_config import get_runtime_config

    return get_runtime_config().environment


DEMO_PROJECT_PREFIX = "PRJ-DEMO-"
_demo_data: dict[tuple, bool] = {}


def demo_data_in_use() -> bool:
    """True if the instance reads the synthetic demo portfolio (every project number starts with PRJ-DEMO-): a TEST
    instance on real data never offers the shared demo accounts. Cached per data source and file version."""
    from app.core.config import get_settings

    cfg = get_settings()
    source = {"json": cfg.json_path, "excel": cfg.excel_path, "mapped": cfg.mapping_path}.get(cfg.data_source)
    if source is None:  # ServiceNow and other live sources are never demo data
        return False
    try:
        key = (cfg.data_source, str(source), source.stat().st_mtime_ns)
    except OSError:
        return False
    if key not in _demo_data:
        try:
            from app.data_adapters import get_data_provider

            numbers = get_data_provider(cfg).load_dataset().projects["project_number"].astype(str)
            _demo_data[key] = bool(len(numbers)) and bool(numbers.str.startswith(DEMO_PROJECT_PREFIX).all())
        except Exception:  # unreadable data is no demo data: the demo accounts stay off
            logger.exception("Portfolio data could not be read to decide about the demo accounts")
            _demo_data[key] = False
    return _demo_data[key]


def demo_allowed(environment: str | None = None) -> bool:
    """Demo accounts only in DEV/TEST and only on the synthetic demo portfolio."""
    return (environment or _active_environment()) in DEMO_ENVIRONMENTS and demo_data_in_use()


def load_users(environment: str | None = None) -> dict[str, User]:
    """Users keyed by casefolded user_id.

    Raises AuthConfigError if the file is missing or invalid, or if PROD would use demo accounts.
    """
    env = environment or _active_environment()
    path = users_file()
    if not demo_allowed(env) and path.resolve() == DEMO_USERS_FILE.resolve():
        raise AuthConfigError(f"{env} must not use the demo accounts ({path.name}); set AUTH_USERS_FILE.")
    admin = (os.getenv("AUTH_ADMIN_USER", ""), hashlib.sha256(_admin_secret().encode("utf-8")).hexdigest())
    cache_key = (env, str(path), _mtime(path), os.getenv("AUTH_EXTRA_USERS", ""), admin)
    with _users_lock:
        if _users_cache.get("key") == cache_key:
            return dict(_users_cache["users"])
    users = _parse_users(env, path)
    with _users_lock:
        _users_cache.update(key=cache_key, users=users)
    return dict(users)


def _mtime(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _parse_users(env: str, path: Path) -> dict[str, User]:
    rows = [(path, row) for row in _read_rows(path)] + [(Path("AUTH_EXTRA_USERS"), row) for row in extra_users()]
    rows += [(Path("AUTH_ADMIN_PASSWORD"), row) for row in admin_user()]
    users: dict[str, User] = {}
    for path, row in rows:
        try:
            user = User(str(row["user_id"]).strip(), str(row["role"]), str(row.get("display_name") or row["user_id"]),
                        bool(row.get("demo", False)), str(row["password_hash"]), str(row.get("email") or "").strip(),
                        bool(row.get("pmo_head", False)))
        except (KeyError, TypeError):
            raise AuthConfigError(f"User file {path}: every entry needs user_id, role and password_hash.") from None
        if not user.user_id:
            raise AuthConfigError(f"User file {path}: an entry has an empty user_id.")
        if user.user_id.casefold() in users:  # two accounts for one sign-in name: which role applies would be luck
            raise AuthConfigError(f"User file {path}: user_id '{user.user_id}' appears twice (case-insensitive).")
        if user.role not in ROLES:
            raise AuthConfigError(f"User file {path}: unknown role '{user.role}' for {user.user_id}.")
        if user.pmo_head and user.role != ROLE_PMO:
            raise AuthConfigError(f"User file {path}: pmo_head is only valid for the PMO role ({user.user_id}).")
        if user.demo and not demo_allowed(env):
            raise AuthConfigError(f"{env} must not use demo accounts ({user.user_id} in {path.name}).")
        users[user.user_id.casefold()] = user
    if not users:
        raise AuthConfigError(f"User file {path} has no users.")
    return users


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["users"]
    except FileNotFoundError:
        raise AuthConfigError(f"User file {path} not found.") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuthConfigError(f"User file {path} unreadable: {exc}") from None
    if not isinstance(rows, list):
        raise AuthConfigError(f"User file {path}: 'users' must be a list.")
    return rows


# =========================================================================== sign-in with lockout
# Shared by every worker process (app.core.shared_state): {"failures": {account: [timestamps]}, "locked_until":
# {account: timestamp}, "attempts": [timestamps of all attempts in the last minute]}. The account is stored as a hash
# of the casefolded user id – the state file holds no names.
SIGN_IN_STATE = "sign_in"
_users_lock = threading.Lock()
_users_cache: dict[str, Any] = {}  # parsed user file, re-read only when the file (or AUTH_EXTRA_USERS) changes


_dummy: list[str] = []


def _dummy_hash() -> str:
    if not _dummy:
        _dummy.append(hash_password(secrets.token_hex(16)))
    return _dummy[0]


def _account(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def reset_lockouts() -> None:
    shared_state.clear(SIGN_IN_STATE)
    with _users_lock:
        _users_cache.clear()


def _settings() -> AuthSettings:
    from app.core.runtime_config import get_runtime_config

    return get_runtime_config().auth


def sign_in(user_id: str, password: str, now: float | None = None) -> SignInResult:
    """Checks the credentials against the user file. Same answer for an unknown user and a wrong password."""
    now = time.time() if now is None else now
    cfg = _settings()
    key = (user_id or "").strip().casefold()
    denied = SignInResult(False, message="Unknown user or wrong password.")
    if not key or not password:
        return denied
    account = _account(key)
    with shared_state.update(SIGN_IN_STATE) as state:
        locked_until = state.setdefault("locked_until", {})
        if locked_until.get(account, 0) > now:
            log_security_event("Login refused – account locked", level=logging.WARNING, user=user_id.strip())
            return SignInResult(False, message=f"Too many failed attempts – try again in {cfg.lockout_minutes:g} minutes.")
        attempts = [t for t in state.get("attempts", []) if now - t < 60]
        if len(attempts) >= cfg.max_attempts_per_minute:
            state["attempts"] = attempts
            log_security_event("Login refused – global attempt budget exhausted", level=logging.WARNING,
                               attempts=len(attempts))
            return SignInResult(False, message="Too many sign-in attempts – try again in a minute.")
        state["attempts"] = [*attempts, now]
    user = load_users().get(key)
    # An unknown user costs the same hashing time as a wrong password (prevents user enumeration via timing).
    if verify_password(password, user.password_hash if user else _dummy_hash()) and user is not None:
        with shared_state.update(SIGN_IN_STATE) as state:
            state.setdefault("failures", {}).pop(account, None)
            state.setdefault("locked_until", {}).pop(account, None)
        log_security_event("Login ok", user=user.user_id, role=user.role)
        return SignInResult(True, user)
    with shared_state.update(SIGN_IN_STATE) as state:
        window = cfg.lockout_minutes * 60
        failures, locked_until = state.setdefault("failures", {}), state.setdefault("locked_until", {})
        recent = [t for t in failures.get(account, []) if now - t < window] + [now]
        failures[account] = recent
        locked = len(recent) >= cfg.max_failed_attempts
        if locked:
            locked_until[account] = now + window
            failures.pop(account, None)
        for other in [a for a, until in locked_until.items() if until <= now]:  # expired entries leave the file
            locked_until.pop(other, None)
        for other in [a for a, times in failures.items() if all(now - t >= window for t in times)]:
            failures.pop(other, None)
    # Log neither the entered password nor whether the user exists.
    log_security_event("Login failed", level=logging.WARNING, user=user_id.strip(), attempts=len(recent))
    if locked:
        log_security_event("Account locked after failed logins", level=logging.WARNING, user=user_id.strip(),
                           minutes=f"{cfg.lockout_minutes:g}")
        return SignInResult(False, message=f"Too many failed attempts – try again in {cfg.lockout_minutes:g} minutes.")
    return denied


def session_expired(last_activity: float | None, now: float | None = None) -> bool:
    if last_activity is None:
        return False
    now = time.time() if now is None else now
    return now - last_activity > _settings().session_timeout_minutes * 60


def demo_password() -> str | None:
    """The public demo password, shown on the sign-in page – only in DEV/TEST and only for the demo user file."""
    if not demo_allowed() or users_file().resolve() != DEMO_USERS_FILE.resolve():
        return None
    try:
        return str(json.loads(DEMO_USERS_FILE.read_text(encoding="utf-8")).get("public_demo_password") or "") or None
    except (OSError, ValueError):
        return None
