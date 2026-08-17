"""Hermes native sessions backed by AgentSmith's local Teams auth service."""
from __future__ import annotations

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)
from hermes_cli.agentsmith_ui_auth_client import dashboard_client, dashboard_configured
from agentsmith_ui_auth.client import AuthClientError


class TeamsDashboardAuthProvider(DashboardAuthProvider):
    name = "teams"
    display_name = "Microsoft Teams"

    def __init__(self, client=None) -> None:
        self.client = client or dashboard_client()

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        _ = redirect_uri
        return LoginStart(redirect_url="/auth/teams/login", cookie_payload={})

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        _ = (code, state, code_verifier, redirect_uri)
        raise NotImplementedError("Teams login completes through /auth/teams/collect")

    @staticmethod
    def _session(value: dict, *, access_token: str = "") -> Session:
        return Session(
            user_id=value["aad_object_id"],
            email="",
            display_name=value["aad_object_id"],
            org_id=value["tenant_id"],
            provider="teams",
            expires_at=int(value["expires_at"]),
            access_token=value.get("access_token", access_token),
            refresh_token="",
            session_key=value["session_key"],
        )

    def verify_session(self, *, access_token: str) -> Session | None:
        try:
            return self._session(
                self.client.verify(access_token=access_token),
                access_token=access_token,
            )
        except AuthClientError as exc:
            if exc.code == "invalid_session":
                return None
            raise ProviderError("UI authentication service unavailable") from exc

    def refresh_session(self, *, refresh_token: str) -> Session:
        _ = refresh_token
        raise RefreshExpiredError("Teams UI sessions are never refreshed")

    def revoke_session(self, *, refresh_token: str) -> None:
        _ = refresh_token

    def logout_access(self, access_token: str) -> dict:
        return self.client.logout_session(access_token=access_token)


def register(ctx) -> None:
    if dashboard_configured():
        ctx.register_dashboard_auth_provider(TeamsDashboardAuthProvider())
