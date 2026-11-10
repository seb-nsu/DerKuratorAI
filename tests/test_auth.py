"""Sign-in per identity – PBKDF2 hashes, lockout, session timeout, demo accounts only in DEV/TEST."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.core import auth
from app.core.runtime_config import update_runtime_config
from app.core.security import ROLE_PM, ROLE_PMO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN = PROJECT_ROOT / "app" / "main.py"
DEMO_PASSWORD = json.loads(auth.DEMO_USERS_FILE.read_text(encoding="utf-8"))["public_demo_password"]
FAST = auth.MIN_ITERATIONS  # the minimum is enforced – tests use it, never fewer


def _user_file(tmp_path: Path, monkeypatch, users: list[dict]) -> Path:
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    monkeypatch.setenv("AUTH_USERS_FILE", str(path))
    return path


@pytest.fixture(scope="module")
def pmo_hash() -> str:
    return auth.hash_password("s3cret-pmo", FAST)


# =========================================================================== hashing
class TestHashing:
    def test_hash_and_verify(self, pmo_hash):
        assert pmo_hash.startswith(f"pbkdf2_sha256${FAST}$")
        assert auth.verify_password("s3cret-pmo", pmo_hash)
        assert not auth.verify_password("s3cret-PMO", pmo_hash)

    def test_same_password_gets_a_new_salt(self, pmo_hash):
        assert auth.hash_password("s3cret-pmo") != pmo_hash

    def test_weak_or_malformed_hashes_never_verify(self):
        weak = auth.hash_password("x").replace(f"${FAST}$", "$1000$")
        assert not auth.verify_password("x", weak)
        assert not auth.verify_password("x", "plain-text")
        assert not auth.verify_password("x", "md5$1$2$3")
        with pytest.raises(ValueError):
            auth.hash_password("x", iterations=1000)
        with pytest.raises(ValueError):
            auth.hash_password("")

    def test_demo_file_holds_hashes_only(self):
        rows = json.loads(auth.DEMO_USERS_FILE.read_text(encoding="utf-8"))["users"]
        assert {r["user_id"] for r in rows} >= {"PMO-ADMIN", "PMO-DEPUTY", "Priya Raman"}
        assert [r["user_id"] for r in rows if r["role"] == auth.ROLE_ADMIN] == ["DEMO-ADMIN"]  # one-click admin for reviewers
        assert all(r["demo"] and r["password_hash"].startswith("pbkdf2_sha256$600000$") for r in rows)
        assert all(DEMO_PASSWORD not in r["password_hash"] for r in rows)


# =========================================================================== sign-in, lockout, timeout
class TestSignIn:
    def test_demo_account_signs_in_with_its_role(self):
        result = auth.sign_in("priya raman", DEMO_PASSWORD)  # user IDs are case-insensitive
        assert result.ok and result.user.user_id == "Priya Raman" and result.user.role == ROLE_PM

    def test_unknown_user_and_wrong_password_get_the_same_answer(self):
        assert auth.sign_in("PMO-ADMIN", "wrong").message == auth.sign_in("NOBODY", "wrong").message

    def test_lockout_after_the_configured_failures_and_release_after_the_lockout(self):
        update_runtime_config(auth={"max_failed_attempts": 3, "lockout_minutes": 5})
        t0 = 1_000_000.0
        for i in range(3):
            assert not auth.sign_in("PMO-ADMIN", "wrong", now=t0 + i).ok
        locked = auth.sign_in("PMO-ADMIN", DEMO_PASSWORD, now=t0 + 10)
        assert not locked.ok and "Too many failed attempts" in locked.message  # even the right password
        assert auth.sign_in("PMO-ADMIN", DEMO_PASSWORD, now=t0 + 5 * 60 + 5).ok

    def test_lockout_is_per_user(self):
        update_runtime_config(auth={"max_failed_attempts": 2})
        for _ in range(2):
            auth.sign_in("PMO-ADMIN", "wrong")
        assert auth.sign_in("PMO-DEPUTY", DEMO_PASSWORD).ok

    def test_security_events_never_contain_the_password(self, caplog):
        caplog.set_level("INFO", logger="derkurator.security")
        auth.sign_in("PMO-ADMIN", "my-typo-password")
        auth.sign_in("PMO-ADMIN", DEMO_PASSWORD)
        text = caplog.text
        assert "SECURITY | Login failed | user=PMO-ADMIN" in text and "SECURITY | Login ok | user=PMO-ADMIN" in text
        assert "my-typo-password" not in text and DEMO_PASSWORD not in text

    def test_session_timeout(self):
        update_runtime_config(auth={"session_timeout_minutes": 30})
        now = time.time()
        assert not auth.session_expired(now - 29 * 60, now)
        assert auth.session_expired(now - 31 * 60, now)
        assert not auth.session_expired(None, now)


# =========================================================================== environments and user files
class TestUserFiles:
    def test_prod_refuses_the_demo_accounts(self):
        assert auth.load_users("TEST")
        with pytest.raises(auth.AuthConfigError, match="demo accounts"):
            auth.load_users("PROD")
        assert auth.demo_allowed("DEV") and auth.demo_allowed("TEST") and not auth.demo_allowed("PROD")

    def test_a_test_instance_on_real_data_offers_no_demo_accounts(self, tmp_path):
        """TEST with a portfolio that is not the synthetic demo (no PRJ-DEMO- numbers): the demo accounts stay off."""
        import json

        from app.core.config import get_settings

        data = json.loads((PROJECT_ROOT / "data" / "DerKurator_Demo_Portfolio.json").read_text("utf-8"))
        for table in data.values():
            for row in table if isinstance(table, list) else []:
                if "project_number" in row:
                    row["project_number"] = str(row["project_number"]).replace("PRJ-DEMO-", "PRJ-")
        (tmp_path / "real.json").write_text(json.dumps(data), encoding="utf-8")
        with pytest.MonkeyPatch.context() as env:  # own context: the shared isolation stays in place
            env.setenv("DATA_SOURCE", "json")
            env.setenv("JSON_PATH", str(tmp_path / "real.json"))
            get_settings.cache_clear()
            assert not auth.demo_allowed("TEST")
        get_settings.cache_clear()
        assert auth.demo_allowed("TEST")

    def test_prod_refuses_a_custom_file_with_demo_entries(self, tmp_path, monkeypatch, pmo_hash):
        _user_file(tmp_path, monkeypatch, [{"user_id": "PMO-1", "role": ROLE_PMO, "demo": True, "password_hash": pmo_hash}])
        with pytest.raises(auth.AuthConfigError, match="demo accounts"):
            auth.load_users("PROD")

    def test_prod_accepts_a_real_user_file(self, tmp_path, monkeypatch, pmo_hash):
        _user_file(tmp_path, monkeypatch, [{"user_id": "PMO-1", "role": ROLE_PMO, "password_hash": pmo_hash}])
        assert auth.load_users("PROD")["pmo-1"].role == ROLE_PMO

    @pytest.mark.parametrize("users, match", [
        ([{"user_id": "X", "role": "Superuser", "password_hash": "h"}], "unknown role"),
        ([{"user_id": "X", "role": ROLE_PMO}], "needs user_id, role and password_hash"),
        ([], "no users"),
    ])
    def test_invalid_user_files_are_refused(self, tmp_path, monkeypatch, users, match):
        _user_file(tmp_path, monkeypatch, users)
        with pytest.raises(auth.AuthConfigError, match=match):
            auth.load_users("DEV")

    def test_missing_file_is_a_config_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTH_USERS_FILE", str(tmp_path / "nope.json"))
        with pytest.raises(auth.AuthConfigError, match="not found"):
            auth.load_users("DEV")

    def test_demo_password_is_shown_only_for_the_demo_file_in_dev_and_test(self, tmp_path, monkeypatch, pmo_hash):
        assert auth.demo_password() == DEMO_PASSWORD
        _user_file(tmp_path, monkeypatch, [{"user_id": "PMO-1", "role": ROLE_PMO, "password_hash": pmo_hash}])
        assert auth.demo_password() is None

    def test_extra_users_add_a_real_admin_to_the_demo(self, tmp_path, monkeypatch, pmo_hash):
        admin = {"user_id": "OWNER", "role": auth.ROLE_ADMIN, "password_hash": pmo_hash}
        monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [admin]}))
        users = auth.load_users("DEV")
        assert users["owner"].role == auth.ROLE_ADMIN and "pmo-admin" in users
        assert auth.demo_password() == DEMO_PASSWORD  # still the public demo
        path = tmp_path / "extra.json"
        path.write_text(json.dumps({"users": [admin]}), encoding="utf-8")
        monkeypatch.setenv("AUTH_EXTRA_USERS", str(path))
        assert "owner" in auth.load_users("DEV")

    def test_admin_password_is_the_simple_admin_access(self, monkeypatch):
        monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "a-long-admin-secret")
        users = auth.load_users("DEV")
        assert users["admin"].role == auth.ROLE_ADMIN and not users["admin"].demo and "pmo-admin" in users
        assert "a-long-admin-secret" not in users["admin"].password_hash
        assert auth.sign_in("ADMIN", "a-long-admin-secret").user.role == auth.ROLE_ADMIN
        assert not auth.sign_in("ADMIN", "wrong-password-123").ok
        monkeypatch.setenv("AUTH_ADMIN_USER", "OWNER")
        assert "owner" in auth.load_users("DEV") and "admin" not in auth.load_users("DEV")
        monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "short")
        with pytest.raises(auth.AuthConfigError, match="at least 12"):
            auth.load_users("DEV")

    def test_extra_users_must_not_be_demo_accounts(self, monkeypatch, pmo_hash):
        monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [
            {"user_id": "X", "role": auth.ROLE_ADMIN, "password_hash": pmo_hash, "demo": True}]}))
        with pytest.raises(auth.AuthConfigError, match="no demo entries"):
            auth.load_users("DEV")

    def test_auth_settings_are_validated(self):
        from app.core.env_config import EnvFileModel

        with pytest.raises(ValueError, match="max_failed_attempts"):  # pydantic's ValidationError is a ValueError
            EnvFileModel.model_validate({"auth": {"max_failed_attempts": 0}})
        assert EnvFileModel.model_validate({"auth": {"lockout_minutes": 10}})


# =========================================================================== UI
@pytest.fixture
def ui(tmp_path, monkeypatch):
    """App with a copy of the demo data (same isolation as the Admin Console UI tests). Yields its own MonkeyPatch for
    environment variables, undone before the shared teardown rebuilds the runtime config (no PROD leaks)."""
    import shutil

    from app.core import logs
    from app.core.config import get_settings
    from app.core.runtime_config import reset_runtime_config

    shutil.copy(PROJECT_ROOT / "data" / "DerKurator_Demo_Portfolio.json", tmp_path / "portfolio.json")
    shutil.copy(PROJECT_ROOT / "data" / "supported_models.json", tmp_path / "supported_models.json")
    env = pytest.MonkeyPatch()
    for key, value in {"JSON_PATH": tmp_path / "portfolio.json", "DATA_SOURCE": "json", "ENVIRONMENT": "DEV"}.items():
        env.setenv(key, str(value))
    monkeypatch.setattr(logs, "DATA_DIR", tmp_path)
    get_settings.cache_clear()
    yield env
    env.undo()
    get_settings.cache_clear()
    reset_runtime_config()


def _switch_to(env: pytest.MonkeyPatch, environment: str) -> None:
    from app.core.config import get_settings
    from app.core.runtime_config import reset_runtime_config

    env.setenv("ENVIRONMENT", environment)
    get_settings.cache_clear()
    reset_runtime_config()


def _fresh() -> AppTest:
    import streamlit as st

    st.cache_data.clear()
    at = AppTest.from_file(str(MAIN), default_timeout=90)
    at.run()
    assert not at.exception, at.exception
    return at


def _submit(at: AppTest, user: str, password: str) -> AppTest:
    at.text_input(key="login_user").input(user)
    at.text_input(key="login_password").input(password)
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert not at.exception, at.exception
    return at


class TestSignInUI:
    def test_without_sign_in_only_the_sign_in_page_exists(self, ui):
        at = _fresh()
        assert [t.value for t in at.title] == ["Sign in"]
        assert "auth_user" not in at.session_state and not any(b.key == "sign_out" for b in at.button)
        assert any("Demo environment – demo accounts" in m.value for m in at.markdown)

    def test_quick_button_signs_in_with_one_click(self, ui):
        at = _fresh()
        at.button(key="demo_PMO-DEPUTY").click().run()
        assert at.session_state["auth_user"]["user_id"] == "PMO-DEPUTY"
        assert at.session_state["login_password"] == ""  # never kept in the session

    def test_wrong_password_stays_on_the_sign_in_page(self, ui):
        at = _submit(_fresh(), "PMO-ADMIN", "wrong")
        assert "auth_user" not in at.session_state
        assert any("Unknown user or wrong password" in w.value for w in at.warning)
        assert at.text_input(key="login_password").value == ""  # the password is never kept

    def test_role_comes_from_the_user_file_not_from_the_session(self, ui):
        at = _fresh()
        at.session_state["role"] = ROLE_PMO  # a forged role in the session must not count
        at = _submit(at, "Priya Raman", DEMO_PASSWORD)
        assert at.session_state["role"] == ROLE_PM and at.session_state["user_id"] == "Priya Raman"

    def test_sign_out_returns_to_the_sign_in_page(self, ui):
        at = _submit(_fresh(), "PMO-ADMIN", DEMO_PASSWORD)
        at.button(key="sign_out").click().run()
        assert "auth_user" not in at.session_state and [t.value for t in at.title] == ["Sign in"]

    def test_inactive_session_is_signed_out(self, ui):
        at = _submit(_fresh(), "PMO-ADMIN", DEMO_PASSWORD)
        at.session_state["auth_last_activity"] = time.time() - 31 * 60
        at.run()
        assert "auth_user" not in at.session_state
        assert any("session expired" in w.value for w in at.warning)

    def test_prod_with_a_real_user_file_signs_in_without_demo_box(self, ui, tmp_path, monkeypatch, pmo_hash):
        _user_file(tmp_path, monkeypatch, [{"user_id": "PMO-1", "role": ROLE_PMO, "password_hash": pmo_hash}])
        _switch_to(ui, "PROD")
        at = _fresh()
        assert not any("demo accounts" in m.value for m in at.markdown)
        at = _submit(at, "PMO-1", "s3cret-pmo")
        assert at.session_state["auth_user"]["role"] == ROLE_PMO
        assert any("LIVE PRODUCTION ENVIRONMENT" in e.value for e in at.error)

    def test_prod_with_demo_accounts_does_not_offer_a_sign_in(self, ui):
        _switch_to(ui, "PROD")
        at = _fresh()
        assert any("Sign-in is not configured" in e.value for e in at.error)
        assert not at.text_input and not any("demo accounts" in m.value for m in at.markdown)
