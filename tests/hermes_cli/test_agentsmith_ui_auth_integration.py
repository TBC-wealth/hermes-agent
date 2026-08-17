from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentsmith_ui_auth.client import AuthClientError
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.routes import router
from plugins.dashboard_auth.teams import TeamsDashboardAuthProvider


AAD = "284adbc3-a4cd-409e-9fab-4150ef066770"
TENANT = "11111111-1111-1111-1111-111111111111"


def _app(client):
    clear_providers()
    register_provider(TeamsDashboardAuthProvider(client))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, base_url="https://agentsmith.example")


def test_login_page_uses_nonce_and_clears_fragment_before_body_redeem():
    client = _app(MagicMock())
    response = client.get("/auth/teams/login")
    assert response.status_code == 200
    assert "history.replaceState" in response.text
    assert "/auth/teams/redeem" in response.text
    assert "/auth/teams/pending" in response.text
    assert "pending:body.pending" not in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'nonce-" in csp
    assert "script-src 'unsafe-inline'" not in csp
    assert response.headers["x-frame-options"] == "DENY"
    clear_providers()


def test_redeem_sets_http_only_pending_cookie_and_collection_mints_native_cookie():
    service = MagicMock()
    service.redeem.return_value = {
        "pending": "pending-secret", "code": "23456789ABCDE",
        "expires_at": 1_900_000_000,
    }
    service.collect.side_effect = [
        AuthClientError("not_confirmed"),
        {
            "access_token": "access-secret",
            "session_key": "a" * 64,
            "tenant_id": TENANT,
            "aad_object_id": AAD,
            "role": "hermes_ui_operator",
            "expires_at": 1_900_000_000,
        },
    ]
    client = _app(service)
    redeemed = client.post("/auth/teams/redeem", json={"grant": "grant-secret"})
    assert redeemed.status_code == 200
    assert redeemed.json() == {"code": "23456789ABCDE", "expires_in": 120}
    cookie = redeemed.headers["set-cookie"]
    assert "__Host-agentsmith_pending=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "pending-secret" not in redeemed.text

    waiting = client.post("/auth/teams/pending", json={})
    assert waiting.status_code == 202
    collected = client.post("/auth/teams/pending", json={})
    assert collected.status_code == 200
    assert collected.json() == {"ok": True, "next": "/"}
    cookies = collected.headers.get_list("set-cookie")
    assert any("__Host-hermes_session_at=access-secret" in value for value in cookies)
    assert not any("hermes_session_rt=access-secret" in value for value in cookies)
    service.collect.assert_called_with(pending="pending-secret")
    clear_providers()


def test_pending_value_is_never_accepted_from_javascript_body():
    service = MagicMock()
    service.collect.side_effect = AuthClientError("invalid_pending")
    client = _app(service)
    response = client.post("/auth/teams/pending", json={"pending": "attacker-value"})
    assert response.status_code == 202
    service.collect.assert_called_once_with(pending="")
    clear_providers()


def test_teams_logout_revokes_access_session_without_refresh_token():
    service = MagicMock()
    service.logout_session.return_value = {"revoked": True, "sequence": 1}
    client = _app(service)
    client.cookies.set("__Host-hermes_session_at", "access-secret")
    response = client.post("/auth/teams/logout")
    assert response.status_code == 200
    service.logout_session.assert_called_once_with(access_token="access-secret")
    clear_providers()


def test_teams_logout_does_not_claim_success_when_revocation_is_unavailable():
    service = MagicMock()
    service.logout_session.side_effect = AuthClientError("unavailable")
    client = _app(service)
    client.cookies.set("__Host-hermes_session_at", "access-secret")
    response = client.post("/auth/teams/logout")
    assert response.status_code == 503
    assert not any("Max-Age=0" in value for value in response.headers.get_list("set-cookie"))
    clear_providers()
