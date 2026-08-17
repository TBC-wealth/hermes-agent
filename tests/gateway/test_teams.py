"""Tests for the Microsoft Teams platform adapter plugin."""

import asyncio
import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from gateway.config import Platform, PlatformConfig, HomeChannel
from plugins.teams_pipeline.models import TeamsMeetingRef, TeamsMeetingSummaryPayload
from tests.gateway._plugin_adapter_loader import load_plugin_adapter


@pytest.fixture(autouse=True)
def _agent_file_upload_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMS_FILE_UPLOAD_ROOTS", str(tmp_path))


# ---------------------------------------------------------------------------
# SDK Mock — install in sys.modules before importing the adapter
# ---------------------------------------------------------------------------

def _ensure_teams_mock():
    """Install a teams SDK mock in sys.modules if the real package isn't present."""
    if "microsoft_teams" in sys.modules and hasattr(sys.modules["microsoft_teams"], "__file__"):
        return

    # Build the module hierarchy
    microsoft_teams = types.ModuleType("microsoft_teams")
    microsoft_teams_apps = types.ModuleType("microsoft_teams.apps")
    microsoft_teams_api = types.ModuleType("microsoft_teams.api")
    microsoft_teams_api_activities = types.ModuleType("microsoft_teams.api.activities")
    microsoft_teams_api_activities_typing = types.ModuleType("microsoft_teams.api.activities.typing")
    microsoft_teams_api_activities_invoke = types.ModuleType("microsoft_teams.api.activities.invoke")
    microsoft_teams_api_activities_invoke_adaptive_card = types.ModuleType(
        "microsoft_teams.api.activities.invoke.adaptive_card"
    )
    microsoft_teams_api_activities_invoke_file_consent = types.ModuleType(
        "microsoft_teams.api.activities.invoke.file_consent"
    )
    microsoft_teams_common = types.ModuleType("microsoft_teams.common")
    microsoft_teams_common_http = types.ModuleType("microsoft_teams.common.http")
    microsoft_teams_common_http_client = types.ModuleType("microsoft_teams.common.http.client")
    microsoft_teams_api_models = types.ModuleType("microsoft_teams.api.models")
    microsoft_teams_api_models_adaptive_card = types.ModuleType("microsoft_teams.api.models.adaptive_card")
    microsoft_teams_api_models_invoke_response = types.ModuleType("microsoft_teams.api.models.invoke_response")
    microsoft_teams_api_models_file = types.ModuleType("microsoft_teams.api.models.file")
    microsoft_teams_api_models_file_consent = types.ModuleType(
        "microsoft_teams.api.models.file.file_consent_card"
    )
    microsoft_teams_api_models_file_info = types.ModuleType(
        "microsoft_teams.api.models.file.file_info_card"
    )
    microsoft_teams_cards = types.ModuleType("microsoft_teams.cards")
    microsoft_teams_apps_http = types.ModuleType("microsoft_teams.apps.http")
    microsoft_teams_apps_http_adapter = types.ModuleType("microsoft_teams.apps.http.adapter")

    # App class mock
    class MockApp:
        def __init__(self, **kwargs):
            self._client_id = kwargs.get("client_id")
            self.server = MagicMock()
            self.server.handle_request = AsyncMock(return_value={"status": 200, "body": None})
            self.credentials = MagicMock()
            self.credentials.client_id = self._client_id

        @property
        def id(self):
            return self._client_id

        def on_message(self, func):
            self._message_handler = func
            return func

        def on_card_action(self, func):
            self._card_action_handler = func
            return func

        def on_file_consent(self, func):
            self._file_consent_handler = func
            return func

        async def initialize(self):
            pass

        async def send(self, conversation_id, activity):
            result = MagicMock()
            result.id = "sent-activity-id"
            return result

        async def start(self, port=3978):
            pass

        async def stop(self):
            pass

    microsoft_teams_apps.App = MockApp
    microsoft_teams_apps.ActivityContext = MagicMock
    microsoft_teams_common_http_client.ClientOptions = MagicMock

    # MessageActivity mock
    microsoft_teams_api.MessageActivity = MagicMock
    microsoft_teams_api.ConversationReference = MagicMock
    microsoft_teams_api.MessageActivityInput = MagicMock
    microsoft_teams_api.Attachment = MagicMock

    # TypingActivityInput mock
    class MockTypingActivityInput:
        pass

    microsoft_teams_api_activities_typing.TypingActivityInput = MockTypingActivityInput

    # Adaptive card invoke activity mock
    microsoft_teams_api_activities_invoke_adaptive_card.AdaptiveCardInvokeActivity = MagicMock
    microsoft_teams_api_activities_invoke_file_consent.FileConsentInvokeActivity = MagicMock

    # Adaptive card response mocks
    microsoft_teams_api_models_adaptive_card.AdaptiveCardActionCardResponse = MagicMock
    microsoft_teams_api_models_adaptive_card.AdaptiveCardActionMessageResponse = MagicMock

    # Invoke response mocks
    class MockInvokeResponse:
        def __init__(self, status=200, body=None):
            self.status = status
            self.body = body

    microsoft_teams_api_models_invoke_response.InvokeResponse = MockInvokeResponse
    microsoft_teams_api_models_invoke_response.AdaptiveCardInvokeResponse = MagicMock
    microsoft_teams_api_models_file_consent.FileConsentCard = MagicMock
    microsoft_teams_api_models_file_info.FileInfoCard = MagicMock

    # Cards mocks
    class MockAdaptiveCard:
        def with_version(self, v):
            return self

        def with_body(self, body):
            return self

        def with_actions(self, actions):
            return self

    microsoft_teams_cards.AdaptiveCard = MockAdaptiveCard
    microsoft_teams_cards.ExecuteAction = MagicMock
    microsoft_teams_cards.TextBlock = MagicMock

    # HttpRequest TypedDict mock
    def HttpRequest(body=None, headers=None):
        return {"body": body, "headers": headers}

    # HttpResponse TypedDict mock
    HttpResponse = dict
    HttpMethod = str
    from typing import Callable
    HttpRouteHandler = Callable

    microsoft_teams_apps_http_adapter.HttpRequest = HttpRequest
    microsoft_teams_apps_http_adapter.HttpResponse = HttpResponse
    microsoft_teams_apps_http_adapter.HttpMethod = HttpMethod
    microsoft_teams_apps_http_adapter.HttpRouteHandler = HttpRouteHandler

    # Wire the hierarchy
    for name, mod in {
        "microsoft_teams": microsoft_teams,
        "microsoft_teams.apps": microsoft_teams_apps,
        "microsoft_teams.api": microsoft_teams_api,
        "microsoft_teams.api.activities": microsoft_teams_api_activities,
        "microsoft_teams.api.activities.typing": microsoft_teams_api_activities_typing,
        "microsoft_teams.api.activities.invoke": microsoft_teams_api_activities_invoke,
        "microsoft_teams.api.activities.invoke.adaptive_card": microsoft_teams_api_activities_invoke_adaptive_card,
        "microsoft_teams.api.activities.invoke.file_consent": microsoft_teams_api_activities_invoke_file_consent,
        "microsoft_teams.common": microsoft_teams_common,
        "microsoft_teams.common.http": microsoft_teams_common_http,
        "microsoft_teams.common.http.client": microsoft_teams_common_http_client,
        "microsoft_teams.api.models": microsoft_teams_api_models,
        "microsoft_teams.api.models.adaptive_card": microsoft_teams_api_models_adaptive_card,
        "microsoft_teams.api.models.invoke_response": microsoft_teams_api_models_invoke_response,
        "microsoft_teams.api.models.file": microsoft_teams_api_models_file,
        "microsoft_teams.api.models.file.file_consent_card": microsoft_teams_api_models_file_consent,
        "microsoft_teams.api.models.file.file_info_card": microsoft_teams_api_models_file_info,
        "microsoft_teams.cards": microsoft_teams_cards,
        "microsoft_teams.apps.http": microsoft_teams_apps_http,
        "microsoft_teams.apps.http.adapter": microsoft_teams_apps_http_adapter,
    }.items():
        sys.modules.setdefault(name, mod)


_ensure_teams_mock()

# Load plugins/platforms/teams/adapter.py under a unique module name
# (plugin_adapter_teams) so it cannot collide with sibling plugin adapters.
_teams_mod = load_plugin_adapter("teams")

_teams_mod.AIOHTTP_AVAILABLE = True
# SDK import is deferred (#62935); bind mocked symbols the same way connect() does.
assert _teams_mod.check_teams_requirements() is True
_teams_mod.TEAMS_SDK_AVAILABLE = True

# Ensure SDK symbols that were None (import failed on Python <3.12) are
# replaced with the mocked versions so runtime calls don't silently no-op.
import sys as _sys
_mt = _sys.modules.get("microsoft_teams.api.activities.typing")
if _mt and _teams_mod.TypingActivityInput is None:
    _teams_mod.TypingActivityInput = _mt.TypingActivityInput

TeamsAdapter = _teams_mod.TeamsAdapter
TeamsSummaryWriter = _teams_mod.TeamsSummaryWriter
check_requirements = _teams_mod.check_requirements
check_teams_requirements = _teams_mod.check_teams_requirements
validate_config = _teams_mod.validate_config
register = _teams_mod.register


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**extra):
    return PlatformConfig(enabled=True, extra=extra)


# ---------------------------------------------------------------------------
# Tests: Requirements
# ---------------------------------------------------------------------------

class TestTeamsAiohttpBridge:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_400_without_calling_sdk(self):
        app = MagicMock()
        registered = {}
        app.router.add_route.side_effect = (
            lambda method, path, handler: registered.update(handler=handler)
        )
        sdk_handler = AsyncMock()
        bridge = _teams_mod._AiohttpBridgeAdapter(app)
        bridge.register_route("POST", "/api/messages", sdk_handler)

        request = MagicMock()
        request.json = AsyncMock(
            side_effect=json.JSONDecodeError("invalid", "", 0),
        )
        response = await registered["handler"](request)

        assert response.status == 400
        sdk_handler.assert_not_awaited()


class TestTeamsRequirements:


    def test_returns_true_when_deps_available(self, monkeypatch):
        monkeypatch.setattr(_teams_mod, "TEAMS_SDK_AVAILABLE", True)
        monkeypatch.setattr(_teams_mod, "AIOHTTP_AVAILABLE", True)
        assert check_requirements() is True

    def test_check_teams_requirements_shortcircuits_when_present(self, monkeypatch):
        # When SDK symbols are already bound and aiohttp is available, the
        # active lazy-installer returns True immediately without re-importing.
        monkeypatch.setattr(_teams_mod, "App", object())
        monkeypatch.setattr(_teams_mod, "AIOHTTP_AVAILABLE", True)
        called = {"ensure_and_bind": 0}

        def _fake_ensure_and_bind(*_args, **_kwargs):
            called["ensure_and_bind"] += 1
            return True

        monkeypatch.setattr(
            "tools.lazy_deps.ensure_and_bind", _fake_ensure_and_bind
        )
        assert check_teams_requirements() is True
        assert called["ensure_and_bind"] == 0

    def test_check_teams_requirements_lazy_installs_when_missing(self, monkeypatch):
        # When deps are missing, the active installer delegates to
        # ensure_and_bind("platform.teams", ...) — parity with Slack/Discord.
        monkeypatch.setattr(_teams_mod, "App", None)
        monkeypatch.setattr(_teams_mod, "TEAMS_SDK_AVAILABLE", False)
        monkeypatch.setattr(_teams_mod, "AIOHTTP_AVAILABLE", False)
        seen = {}

        def _fake_ensure_and_bind(feature, importer, target_globals, **kwargs):
            seen["feature"] = feature
            return True

        monkeypatch.setattr(
            "tools.lazy_deps.ensure_and_bind", _fake_ensure_and_bind
        )
        assert check_teams_requirements() is True
        assert seen["feature"] == "platform.teams"

    def test_validate_config_with_env(self, monkeypatch):
        monkeypatch.setenv("TEAMS_CLIENT_ID", "test-id")
        monkeypatch.setenv("TEAMS_CLIENT_SECRET", "test-secret")
        monkeypatch.setenv("TEAMS_TENANT_ID", "test-tenant")
        assert validate_config(_make_config()) is True

    def test_validate_config_from_extra(self, monkeypatch):
        monkeypatch.delenv("TEAMS_CLIENT_ID", raising=False)
        monkeypatch.delenv("TEAMS_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("TEAMS_TENANT_ID", raising=False)
        cfg = _make_config(client_id="id", client_secret="secret", tenant_id="tenant")
        assert validate_config(cfg) is True


# ---------------------------------------------------------------------------
# Tests: Adapter Init
# ---------------------------------------------------------------------------

class TestTeamsAdapterInit:
    def test_reads_config_from_extra(self):
        config = _make_config(
            client_id="cfg-id",
            client_secret="cfg-secret",
            tenant_id="cfg-tenant",
        )
        adapter = TeamsAdapter(config)
        assert adapter._client_id == "cfg-id"
        assert adapter._client_secret == "cfg-secret"
        assert adapter._tenant_id == "cfg-tenant"


    def test_custom_port_from_env(self, monkeypatch):
        monkeypatch.setenv("TEAMS_PORT", "5000")
        adapter = TeamsAdapter(_make_config(client_id="id", client_secret="secret", tenant_id="tenant"))
        assert adapter._port == 5000

    def test_invalid_port_from_extra_falls_back_to_default(self):
        adapter = TeamsAdapter(
            _make_config(client_id="id", client_secret="secret", tenant_id="tenant", port="abc")
        )
        assert adapter._port == 3978


# ---------------------------------------------------------------------------
# Tests: Plugin registration
# ---------------------------------------------------------------------------

class TestTeamsPluginRegistration:


    def test_register_name(self):
        ctx = MagicMock()
        register(ctx)
        kwargs = ctx.register_platform.call_args[1]
        assert kwargs["name"] == "teams"

    def test_register_splits_passive_probe_from_active_installer(self):
        # check_fn is the PASSIVE probe (status displays call it freely);
        # the ACTIVE lazy-installer rides on ensure_deps_fn, which
        # create_adapter() invokes when the passive probe fails (#79812).
        ctx = MagicMock()
        register(ctx)
        kwargs = ctx.register_platform.call_args[1]
        assert kwargs["check_fn"] is check_requirements
        assert kwargs["ensure_deps_fn"] is check_teams_requirements

    def test_register_auth_env_vars(self):
        ctx = MagicMock()
        register(ctx)
        kwargs = ctx.register_platform.call_args[1]
        assert kwargs["allowed_users_env"] == "TEAMS_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "TEAMS_ALLOW_ALL_USERS"


# ---------------------------------------------------------------------------
# Tests: Interactive setup (import fix regression — #18325 / #19173)
# ---------------------------------------------------------------------------

class TestTeamsInteractiveSetup:
    def test_interactive_setup_persists_credentials(self, tmp_path, monkeypatch):
        """Regression for #19173: interactive_setup must import prompt helpers
        from hermes_cli.cli_output (not hermes_cli.config) and persist
        credentials to .env without crashing.
        """
        hermes_home = tmp_path / "hermes"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        import hermes_cli.cli_output as cli_output_mod

        answers = iter(["client-id", "client-secret", "tenant-id", "aad-1, aad-2"])
        monkeypatch.setattr(cli_output_mod, "prompt", lambda *_a, **_kw: next(answers))
        monkeypatch.setattr(cli_output_mod, "prompt_yes_no", lambda *_a, **_kw: True)
        monkeypatch.setattr(cli_output_mod, "print_info", lambda *_a, **_kw: None)
        monkeypatch.setattr(cli_output_mod, "print_success", lambda *_a, **_kw: None)
        monkeypatch.setattr(cli_output_mod, "print_warning", lambda *_a, **_kw: None)

        _teams_mod.interactive_setup()

        env_text = (hermes_home / ".env").read_text(encoding="utf-8")
        assert "TEAMS_CLIENT_ID=client-id" in env_text
        assert "TEAMS_TENANT_ID=tenant-id" in env_text

class TestTeamsConnect:
    @pytest.mark.anyio
    async def test_connect_fails_without_sdk(self, monkeypatch):
        monkeypatch.setattr(_teams_mod, "TEAMS_SDK_AVAILABLE", False)
        # Simulate the SDK being unavailable AND not installable (offline /
        # locked-down env): the lazy-installer can't rebind the globals, so
        # TEAMS_SDK_AVAILABLE stays False and connect() must fail.
        monkeypatch.setattr(
            "tools.lazy_deps.ensure_and_bind",
            lambda *_a, **_k: False,
        )
        adapter = TeamsAdapter(_make_config(
            client_id="id", client_secret="secret", tenant_id="tenant",
        ))
        result = await adapter.connect()
        assert result is False


# ---------------------------------------------------------------------------
# Tests: Send
# ---------------------------------------------------------------------------

class TestTeamsSend:

    @pytest.mark.anyio
    async def test_send_calls_app_send(self):
        adapter = TeamsAdapter(_make_config(
            client_id="id", client_secret="secret", tenant_id="tenant",
        ))
        mock_result = MagicMock()
        mock_result.id = "msg-123"
        mock_app = MagicMock()
        mock_app.send = AsyncMock(return_value=mock_result)
        adapter._app = mock_app

        result = await adapter.send("conv-id", "Hello")
        assert result.success is True
        assert result.message_id == "msg-123"
        mock_app.send.assert_awaited_once_with("conv-id", "Hello")

    @pytest.mark.anyio
    async def test_send_resolves_stable_aad_target(self, tmp_path, monkeypatch):
        store = tmp_path / "conversations.json"
        store.write_text(json.dumps({
            "aad-456": {
                "chat_id": "19:abc@thread.v2",
                "service_url": "https://smba.trafficmanager.net/teams/",
            }
        }))
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(store))
        adapter = TeamsAdapter(_make_config(client_id="id", client_secret="secret", tenant_id="tenant"))
        adapter._app = MagicMock()
        adapter._app.send = AsyncMock(return_value=SimpleNamespace(id="msg-1"))
        result = await adapter.send("user:aad-456", "Hello")
        assert result.success is True
        adapter._app.send.assert_awaited_once_with("19:abc@thread.v2", "Hello")

    @pytest.mark.anyio
    async def test_send_fails_closed_for_unknown_aad_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(tmp_path / "missing.json"))
        adapter = TeamsAdapter(_make_config(client_id="id", client_secret="secret", tenant_id="tenant"))
        adapter._app = MagicMock()
        result = await adapter.send("user:unknown", "Hello")
        assert result.success is False
        adapter._app.send.assert_not_called()

    def test_concurrent_conversation_updates_do_not_lose_routes(self, tmp_path, monkeypatch):
        store = tmp_path / "conversations.json"
        users = [f"aad-{index}" for index in range(20)]
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(store))
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", ",".join(users))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda user: _teams_mod._remember_conversation(
                    user,
                    f"19:{user}@thread.v2",
                    "https://smba.trafficmanager.net/teams/",
                ),
                users,
            ))
        routes = json.loads(store.read_text())
        assert set(routes) == set(users)
        assert store.stat().st_mode & 0o777 == 0o600
        assert Path(f"{store}.lock").stat().st_mode & 0o777 == 0o600


def _make_summary_payload():
    return TeamsMeetingSummaryPayload(
        meeting_ref=TeamsMeetingRef(meeting_id="meeting-123"),
        title="Weekly Sync",
        summary="Discussed launch readiness.",
        key_decisions=["Proceed with staged rollout."],
        action_items=["Send launch checklist."],
        risks=["QA sign-off still pending."],
    )


class TestTeamsSummaryWriter:

    @pytest.mark.anyio
    async def test_graph_delivery_posts_to_channel(self):
        graph_client = SimpleNamespace(
            post_json=AsyncMock(return_value={"id": "msg-123", "webUrl": "https://teams.example/messages/123"})
        )
        writer = TeamsSummaryWriter(graph_client=graph_client)
        payload = _make_summary_payload()

        result = await writer.write_summary(
            payload,
            {
                "delivery_mode": "graph",
                "team_id": "team-1",
                "channel_id": "channel-1",
            },
        )

        assert result["target_type"] == "channel"
        assert result["message_id"] == "msg-123"
        graph_client.post_json.assert_awaited_once()
        path = graph_client.post_json.await_args.args[0]
        body = graph_client.post_json.await_args.kwargs["json_body"]
        assert path == "/teams/team-1/channels/channel-1/messages"
        assert body["body"]["contentType"] == "html"
        assert "Weekly Sync" in body["body"]["content"]


# ---------------------------------------------------------------------------
# Tests: Message Handling
# ---------------------------------------------------------------------------

class TestTeamsMessageHandling:
    def _make_activity(
        self,
        *,
        text="Hello",
        from_id="user-123",
        from_aad_id="aad-456",
        from_name="Test User",
        conversation_id="19:abc@thread.v2",
        conversation_type="personal",
        tenant_id="tenant-789",
        activity_id="activity-001",
        attachments=None,
    ):
        activity = MagicMock()
        activity.text = text
        activity.id = activity_id
        activity.from_ = MagicMock()
        activity.from_.id = from_id
        activity.from_.aad_object_id = from_aad_id
        activity.from_.name = from_name
        activity.conversation = MagicMock()
        activity.conversation.id = conversation_id
        activity.conversation.conversation_type = conversation_type
        activity.conversation.name = "Test Chat"
        activity.conversation.tenant_id = tenant_id
        activity.attachments = attachments or []
        return activity

    def _make_ctx(self, activity):
        ctx = MagicMock()
        ctx.activity = activity
        return ctx

    @pytest.mark.anyio
    async def test_personal_message_creates_dm_event(self):
        adapter = TeamsAdapter(_make_config(
            client_id="bot-id", client_secret="secret", tenant_id="tenant",
        ))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter.handle_message = AsyncMock()

        activity = self._make_activity(conversation_type="personal")
        await adapter._on_message(self._make_ctx(activity))

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_type == "dm"

    @pytest.mark.anyio
    async def test_group_message_creates_group_event(self):
        adapter = TeamsAdapter(_make_config(
            client_id="bot-id", client_secret="secret", tenant_id="tenant",
        ))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter.handle_message = AsyncMock()

        activity = self._make_activity(conversation_type="groupChat")
        await adapter._on_message(self._make_ctx(activity))

        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_type == "group"

    @pytest.mark.anyio
    async def test_allowed_sender_route_is_persisted_atomically(self, tmp_path, monkeypatch):
        store = tmp_path / "private" / "conversations.json"
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(store))
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "aad-456")
        adapter = TeamsAdapter(_make_config(client_id="bot-id", client_secret="secret", tenant_id="tenant"))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter.handle_message = AsyncMock()
        activity = self._make_activity()
        activity.service_url = "https://smba.trafficmanager.net/teams/"
        await adapter._on_message(self._make_ctx(activity))
        route = json.loads(store.read_text())["aad-456"]
        assert route["chat_id"] == "19:abc@thread.v2"
        assert route["service_url"] == "https://smba.trafficmanager.net/teams/"
        assert store.stat().st_mode & 0o777 == 0o600
        assert "user:aad-456" in adapter._conv_refs

    @pytest.mark.anyio
    async def test_unlisted_sender_route_is_not_persisted(self, tmp_path, monkeypatch):
        store = tmp_path / "conversations.json"
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(store))
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "someone-else")
        adapter = TeamsAdapter(_make_config(client_id="bot-id", client_secret="secret", tenant_id="tenant"))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter.handle_message = AsyncMock()
        activity = self._make_activity()
        activity.service_url = "https://smba.trafficmanager.net/teams/"
        await adapter._on_message(self._make_ctx(activity))
        assert not store.exists()

    @pytest.mark.anyio
    async def test_group_message_does_not_replace_personal_route(self, tmp_path, monkeypatch):
        store = tmp_path / "conversations.json"
        store.write_text(json.dumps({
            "aad-456": {
                "chat_id": "19:personal@thread.v2",
                "service_url": "https://smba.trafficmanager.net/teams/",
            }
        }))
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(store))
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "aad-456")
        adapter = TeamsAdapter(_make_config(
            client_id="bot-id", client_secret="secret", tenant_id="tenant",
        ))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter.handle_message = AsyncMock()
        activity = self._make_activity(
            conversation_id="19:group@thread.v2",
            conversation_type="groupChat",
        )
        activity.service_url = "https://smba.trafficmanager.net/teams/"

        await adapter._on_message(self._make_ctx(activity))

        assert json.loads(store.read_text())["aad-456"]["chat_id"] == "19:personal@thread.v2"
        assert "user:aad-456" not in adapter._conv_refs


class TestTeamsUIAuthCommands:
    def _adapter_and_context(self, text, *, conversation_type="personal"):
        adapter = TeamsAdapter(_make_config(
            client_id="bot-id", client_secret="secret", tenant_id="tenant-789",
        ))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter.handle_message = AsyncMock()
        activity = MagicMock()
        activity.text = text
        activity.id = f"activity-{text}"
        activity.from_ = MagicMock(id="user", aad_object_id="aad-456", name="Alberto")
        activity.conversation = MagicMock(
            id="19:personal", conversation_type=conversation_type,
            name="Chat", tenant_id="tenant-789",
        )
        activity.attachments = []
        ctx = MagicMock(activity=activity)
        ctx.send = AsyncMock()
        return adapter, ctx

    @pytest.mark.anyio
    async def test_ui_login_and_confirm_bypass_llm(self, monkeypatch):
        client = MagicMock()
        client.issue.return_value = {"grant": "grant-secret"}
        client.confirm.return_value = {"confirmed": True}
        monkeypatch.setattr(
            "hermes_cli.agentsmith_ui_auth_client.issuer_configured", lambda: True,
        )
        monkeypatch.setattr(
            "hermes_cli.agentsmith_ui_auth_client.issuer_client", lambda: client,
        )
        monkeypatch.setenv("HERMES_DASHBOARD_PUBLIC_URL", "https://agentsmith.example")
        adapter, login_ctx = self._adapter_and_context("  UI   login ")
        await adapter._on_message(login_ctx)
        adapter.handle_message.assert_not_awaited()
        login_reply = login_ctx.send.await_args.args[0]
        assert "https://agentsmith.example/auth/teams/login#grant=grant-secret" in login_reply

        code = "23456789ABCDE"
        adapter2, confirm_ctx = self._adapter_and_context(f"UI confirm {code}")
        await adapter2._on_message(confirm_ctx)
        adapter2.handle_message.assert_not_awaited()
        assert "confirmed" in confirm_ctx.send.await_args.args[0].lower()
        client.issue.assert_called_once_with(
            aad_object_id="aad-456", tenant_id="tenant-789",
            conversation_id="19:personal",
        )
        client.confirm.assert_called_once_with(
            aad_object_id="aad-456", tenant_id="tenant-789",
            conversation_id="19:personal", code=code,
        )

    @pytest.mark.anyio
    async def test_ui_command_in_group_fails_closed(self, monkeypatch):
        adapter, ctx = self._adapter_and_context("UI login", conversation_type="groupChat")
        await adapter._on_message(ctx)
        adapter.handle_message.assert_not_awaited()
        assert "private chat" in ctx.send.await_args.args[0]

    @pytest.mark.anyio
    async def test_similar_prose_is_not_treated_as_command(self, monkeypatch):
        adapter, ctx = self._adapter_and_context("Can you explain UI login please?")
        await adapter._on_message(ctx)
        adapter.handle_message.assert_awaited_once()
        ctx.send.assert_not_awaited()

    @pytest.mark.anyio
    async def test_malformed_confirmation_is_intercepted_before_llm(self):
        adapter, ctx = self._adapter_and_context("UI confirm 123456")
        await adapter._on_message(ctx)
        adapter.handle_message.assert_not_awaited()
        assert "could not be completed" in ctx.send.await_args.args[0]

    @pytest.mark.anyio
    @pytest.mark.parametrize("missing", ["aad", "tenant"])
    async def test_ui_auth_never_falls_back_to_generic_sender_or_configured_tenant(
        self, monkeypatch, missing,
    ):
        client = MagicMock()
        monkeypatch.setattr(
            "hermes_cli.agentsmith_ui_auth_client.issuer_configured", lambda: True,
        )
        monkeypatch.setattr(
            "hermes_cli.agentsmith_ui_auth_client.issuer_client", lambda: client,
        )
        adapter, ctx = self._adapter_and_context("UI login")
        if missing == "aad":
            ctx.activity.from_.aad_object_id = None
            ctx.activity.from_.id = "generic-sender-must-not-work"
        else:
            ctx.activity.conversation.tenant_id = None
        await adapter._on_message(ctx)
        adapter.handle_message.assert_not_awaited()
        assert "could not be completed" in ctx.send.await_args.args[0]
        client.issue.assert_not_called()


class TestTeamsAttachmentClassification:
    """Document attachments must set MessageType.DOCUMENT so run.py's
    document-context injection surfaces the cached file to the agent
    (same bug class as Signal/Email/SimpleX, PR #44695)."""

    def _make_adapter(self):
        adapter = TeamsAdapter(_make_config(
            client_id="bot-id", client_secret="secret", tenant_id="tenant",
        ))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter.handle_message = AsyncMock()
        return adapter

    def _make_activity(self, attachments, text="see attached"):
        activity = MagicMock()
        activity.text = text
        activity.id = "activity-att-001"
        activity.from_ = MagicMock()
        activity.from_.id = "user-123"
        activity.from_.aad_object_id = "aad-456"
        activity.from_.name = "Test User"
        activity.conversation = MagicMock()
        activity.conversation.id = "19:abc@thread.v2"
        activity.conversation.conversation_type = "personal"
        activity.conversation.name = "Test Chat"
        activity.conversation.tenant_id = "tenant-789"
        activity.attachments = attachments
        return activity

    def _make_ctx(self, activity):
        ctx = MagicMock()
        ctx.activity = activity
        return ctx

    def _file_download_attachment(self, name="report.pdf", file_type="pdf"):
        att = MagicMock()
        att.content_type = "application/vnd.microsoft.teams.file.download.info"
        att.content_url = None
        att.name = name
        att.content = {
            "downloadUrl": "https://contoso.sharepoint.com/download/x",
            "fileType": file_type,
        }
        return att

    def _image_attachment(self):
        att = MagicMock()
        att.content_type = "image/png"
        att.content_url = "https://smba.example.com/img.png"
        att.name = "img.png"
        return att

    def _html_body_attachment(self):
        # Teams mirrors the message body as a text/html attachment
        att = MagicMock()
        att.content_type = "text/html"
        att.content_url = None
        att.name = ""
        return att

    @pytest.mark.anyio
    async def test_file_download_info_sets_document_type(self):
        from gateway.platforms.base import MessageType

        adapter = self._make_adapter()
        adapter._fetch_attachment_bytes = AsyncMock(return_value=b"%PDF-1.4 fake")

        activity = self._make_activity([self._file_download_attachment()])
        await adapter._on_message(self._make_ctx(activity))

        event = adapter.handle_message.call_args[0][0]
        assert event.message_type == MessageType.DOCUMENT, (
            f"Expected DOCUMENT, got {event.message_type}. "
            "Documents must be classified as DOCUMENT so run.py injects file context."
        )
        assert len(event.media_urls) == 1
        assert event.media_types == ["application/pdf"]

    @pytest.mark.anyio
    async def test_mixed_image_and_document_prefers_document(self):
        from gateway.platforms.base import MessageType

        adapter = self._make_adapter()
        adapter._fetch_attachment_bytes = AsyncMock(return_value=b"%PDF-1.4 fake")

        async def fake_cache_image(url, *a, **kw):
            return "/tmp/img.png"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_teams_mod, "cache_image_from_url", fake_cache_image)
            activity = self._make_activity([
                self._image_attachment(),
                self._file_download_attachment(),
            ])
            await adapter._on_message(self._make_ctx(activity))

        event = adapter.handle_message.call_args[0][0]
        assert event.message_type == MessageType.DOCUMENT
        assert len(event.media_urls) == 2


# ── _standalone_send (out-of-process cron delivery) ──────────────────────


class _FakeAiohttpResponse:
    def __init__(self, status: int, payload, text_body: str = ""):
        self.status = status
        self._payload = payload
        self._text = text_body or (str(payload) if payload is not None else "")

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeAiohttpSession:
    """Scripted aiohttp.ClientSession with a queue of responses so tests
    can assert calls in order."""

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self._scripts:
            raise AssertionError(f"No scripted response for POST {url}")
        return self._scripts.pop(0)


def _install_fake_aiohttp(monkeypatch, session):
    """Replace ``aiohttp`` in ``sys.modules`` so ``import aiohttp as _aiohttp``
    inside ``_standalone_send`` picks up our fake."""
    fake_aiohttp = types.SimpleNamespace(
        ClientSession=lambda timeout=None, **kwargs: session,
        ClientTimeout=lambda total=None: None,
    )
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)


class TestTeamsStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_acquires_token_and_posts_activity(self, monkeypatch):
        monkeypatch.setenv("TEAMS_CLIENT_ID", "client-id")
        monkeypatch.setenv("TEAMS_CLIENT_SECRET", "secret")
        monkeypatch.setenv("TEAMS_TENANT_ID", "tenant")
        monkeypatch.delenv("TEAMS_SERVICE_URL", raising=False)

        token_resp = _FakeAiohttpResponse(200, {"access_token": "the-token"})
        activity_resp = _FakeAiohttpResponse(200, {"id": "msg-99"})
        session = _FakeAiohttpSession([token_resp, activity_resp])
        _install_fake_aiohttp(monkeypatch, session)

        result = await _teams_mod._standalone_send(
            PlatformConfig(enabled=True, extra={}),
            "19:abc@thread.skype",
            "hello cron",
        )

        assert result == {"success": True, "message_id": "msg-99"}
        assert len(session.calls) == 2

        token_url, token_kwargs = session.calls[0]
        assert "login.microsoftonline.com/tenant/oauth2/v2.0/token" in token_url
        assert token_kwargs["data"]["client_id"] == "client-id"
        assert token_kwargs["data"]["client_secret"] == "secret"
        assert token_kwargs["data"]["scope"] == "https://api.botframework.com/.default"

        activity_url, activity_kwargs = session.calls[1]
        # Default service URL when TEAMS_SERVICE_URL is unset
        assert "smba.trafficmanager.net" in activity_url
        assert "/v3/conversations/19:abc@thread.skype/activities" in activity_url
        assert activity_kwargs["headers"]["Authorization"] == "Bearer the-token"
        assert activity_kwargs["json"]["text"] == "hello cron"
        assert activity_kwargs["json"]["type"] == "message"


    @pytest.mark.asyncio
    async def test_standalone_send_propagates_token_failure(self, monkeypatch):
        monkeypatch.setenv("TEAMS_CLIENT_ID", "client-id")
        monkeypatch.setenv("TEAMS_CLIENT_SECRET", "secret")
        monkeypatch.setenv("TEAMS_TENANT_ID", "tenant")

        token_resp = _FakeAiohttpResponse(
            401,
            {"error": "unauthorized_client"},
            text_body='{"error":"unauthorized_client"}',
        )
        session = _FakeAiohttpSession([token_resp])
        _install_fake_aiohttp(monkeypatch, session)

        result = await _teams_mod._standalone_send(
            PlatformConfig(enabled=True, extra={}),
            "19:abc@thread.skype",
            "hi",
        )

        assert "error" in result
        assert "401" in result["error"]
        assert "token" in result["error"].lower()


class TestTeamsMediaAttachments:
    """send_video / send_voice / send_document route through the same
    Attachment mechanism as send_image so the gateway's media dispatch
    (run.py) delivers native attachments instead of the base-class text
    fallback (file path sent as plain text)."""

    def _make_adapter(self):
        adapter = TeamsAdapter(_make_config(
            client_id="bot-id", client_secret="secret", tenant_id="tenant",
        ))
        adapter._app = MagicMock()
        adapter._app.id = "bot-id"
        adapter._app.send = AsyncMock(return_value=MagicMock(id="msg-001"))
        return adapter


    @pytest.mark.asyncio
    async def test_send_voice_local_file_base64(self, tmp_path):
        adapter = self._make_adapter()
        audio = tmp_path / "reply.mp3"
        audio.write_bytes(b"ID3fakeaudio")
        result = await adapter.send_voice("19:abc@thread.v2", str(audio), caption="here you go")
        assert result.success
        adapter._app.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_document_uses_sender_bound_file_consent(self, tmp_path, monkeypatch):
        route_store = tmp_path / "conversations.json"
        route_store.write_text(json.dumps({
            "aad-456": {
                "chat_id": "19:abc@thread.v2",
                "service_url": "https://smba.trafficmanager.net/teams/",
            }
        }))
        upload_store = tmp_path / "pending-uploads.json"
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(route_store))
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STORE", str(upload_store))
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STATUS_STORE", str(tmp_path / "status.json"))
        adapter = self._make_adapter()
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        result = await adapter.send_document("user:aad-456", str(doc))
        assert result.success
        assert result.raw_response["deliveryStatus"] == "file_consent_requested"
        assert result.raw_response["terminal"] is False
        adapter._app.send.assert_awaited_once()
        pending = json.loads(upload_store.read_text())
        assert len(pending) == 1
        entry = next(iter(pending.values()))
        assert entry["user_id"] == "aad-456"
        assert entry["chat_id"] == "19:abc@thread.v2"
        assert entry["path"] == str(doc.resolve())
        assert upload_store.stat().st_mode & 0o777 == 0o600

    @pytest.mark.asyncio
    async def test_send_document_refuses_unknown_conversation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(tmp_path / "missing.json"))
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STORE", str(tmp_path / "pending.json"))
        adapter = self._make_adapter()
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        result = await adapter.send_document("19:unknown@thread.v2", str(doc))
        assert not result.success
        assert "captured personal conversation" in result.error

    @pytest.mark.asyncio
    async def test_send_document_refuses_file_outside_approved_roots(self, tmp_path, monkeypatch):
        route_store = tmp_path / "conversations.json"
        route_store.write_text(json.dumps({
            "aad-456": {
                "chat_id": "19:abc@thread.v2",
                "service_url": "https://smba.trafficmanager.net/teams/",
            }
        }))
        monkeypatch.setenv("TEAMS_CONVERSATION_STORE", str(route_store))
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_ROOTS", str(tmp_path / "approved"))
        adapter = self._make_adapter()
        document = tmp_path / "outside.pdf"
        document.write_bytes(b"%PDF outside")
        result = await adapter.send_document("user:aad-456", str(document))
        assert not result.success
        assert "roots" in result.error

    def test_concurrent_upload_and_status_updates_do_not_lose_entries(self, tmp_path, monkeypatch):
        upload_store = tmp_path / "pending.json"
        status_store = tmp_path / "status.json"
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STORE", str(upload_store))
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STATUS_STORE", str(status_store))
        documents = []
        for index in range(20):
            document = tmp_path / f"report-{index}.pdf"
            document.write_bytes(f"%PDF {index}".encode())
            documents.append(document)
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(
                lambda document: _teams_mod._queue_file_upload(
                    str(document), "aad-456", "19:abc@thread.v2", document.name
                ),
                documents,
            ))
        assert set(json.loads(upload_store.read_text())) == set(tokens)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda token: _teams_mod._record_file_upload_status(
                    token, "file_consent_requested"
                ),
                tokens,
            ))
        assert set(json.loads(status_store.read_text())) == set(tokens)


class _FakeUploadClient:
    def __init__(self):
        self.put = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class TestTeamsFileConsentCallback:
    @pytest.mark.asyncio
    async def test_accept_uploads_exact_file_and_consumes_token(self, tmp_path, monkeypatch):
        upload_store = tmp_path / "pending.json"
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STORE", str(upload_store))
        status_store = tmp_path / "status.json"
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STATUS_STORE", str(status_store))
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "aad-456")
        document = tmp_path / "report.pdf"
        document.write_bytes(b"%PDF test")
        token = _teams_mod._queue_file_upload(
            str(document), "aad-456", "19:abc@thread.v2", "report.pdf"
        )

        client = _FakeUploadClient()
        monkeypatch.setattr("tools.url_safety.is_safe_url", lambda _url: True)
        monkeypatch.setattr(
            "tools.url_safety.create_ssrf_safe_async_client",
            lambda **_kwargs: client,
        )
        adapter = TeamsAdapter(_make_config(client_id="bot-id", client_secret="secret", tenant_id="tenant"))
        response = SimpleNamespace(
            action="accept",
            context={"token": token},
            upload_info=SimpleNamespace(
                upload_url="https://tenant.sharepoint.com/upload",
                content_url="https://tenant.sharepoint.com/report.pdf",
                unique_id="drive-item-1",
                name="report.pdf",
                file_type="pdf",
            ),
        )
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=response,
                from_=SimpleNamespace(aad_object_id="aad-456", id="channel-id"),
                conversation=SimpleNamespace(id="19:abc@thread.v2"),
            ),
            send=AsyncMock(),
        )

        result = await adapter._on_file_consent(ctx)
        assert result.status == 200
        client.put.assert_awaited_once()
        assert client.put.await_args.kwargs["content"] == b"%PDF test"
        assert client.put.await_args.kwargs["headers"] == {
            "Content-Type": "application/octet-stream",
            "Content-Length": "9",
            "Content-Range": "bytes 0-8/9",
        }
        assert json.loads(upload_store.read_text()) == {}
        assert json.loads(status_store.read_text())[token]["status"] == "upload_complete"
        ctx.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_accept_callback_uploads_only_once(self, tmp_path, monkeypatch):
        upload_store = tmp_path / "pending.json"
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STORE", str(upload_store))
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STATUS_STORE", str(tmp_path / "status.json"))
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "aad-456")
        document = tmp_path / "report.pdf"
        document.write_bytes(b"%PDF test")
        token = _teams_mod._queue_file_upload(
            str(document), "aad-456", "19:abc@thread.v2", "report.pdf"
        )
        client = _FakeUploadClient()
        monkeypatch.setattr("tools.url_safety.is_safe_url", lambda _url: True)
        monkeypatch.setattr(
            "tools.url_safety.create_ssrf_safe_async_client",
            lambda **_kwargs: client,
        )
        response = SimpleNamespace(
            action="accept",
            context={"token": token},
            upload_info=SimpleNamespace(
                upload_url="https://tenant.sharepoint.com/upload",
                content_url="https://tenant.sharepoint.com/report.pdf",
                unique_id="drive-item-1",
                name="report.pdf",
                file_type="pdf",
            ),
        )
        contexts = [
            SimpleNamespace(
                activity=SimpleNamespace(
                    value=response,
                    from_=SimpleNamespace(aad_object_id="aad-456", id="channel-id"),
                    conversation=SimpleNamespace(id="19:abc@thread.v2"),
                ),
                send=AsyncMock(),
            )
            for _ in range(2)
        ]
        adapter = TeamsAdapter(_make_config(
            client_id="bot-id", client_secret="secret", tenant_id="tenant",
        ))

        results = await asyncio.gather(*(adapter._on_file_consent(ctx) for ctx in contexts))

        assert [result.status for result in results] == [200, 200]
        assert client.put.await_count == 1
        assert json.loads(upload_store.read_text()) == {}
        assert sum(ctx.send.await_count for ctx in contexts) == 2

    @pytest.mark.asyncio
    async def test_accept_rejects_token_from_different_sender(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEAMS_FILE_UPLOAD_STORE", str(tmp_path / "pending.json"))
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "aad-456,aad-evil")
        document = tmp_path / "report.pdf"
        document.write_bytes(b"%PDF test")
        token = _teams_mod._queue_file_upload(
            str(document), "aad-456", "19:abc@thread.v2", "report.pdf"
        )
        ctx = SimpleNamespace(
            activity=SimpleNamespace(
                value=SimpleNamespace(action="accept", context={"token": token}),
                from_=SimpleNamespace(aad_object_id="aad-evil", id="channel-id"),
                conversation=SimpleNamespace(id="19:abc@thread.v2"),
            ),
            send=AsyncMock(),
        )
        adapter = TeamsAdapter(_make_config(client_id="bot-id", client_secret="secret", tenant_id="tenant"))
        result = await adapter._on_file_consent(ctx)
        assert result.status == 200
        ctx.send.assert_not_awaited()
        assert token in json.loads((tmp_path / "pending.json").read_text())


class TestTeamsApprovalBinding:
    def test_approval_is_sender_dm_bound_and_one_shot(self):
        token = "approval-token"
        _teams_mod._record_pending_approval(
            token,
            session_key="agent:main:teams:dm:chat-a:user-a",
            user_id="user-a",
            profile="alberto",
            chat_id="chat-a",
            command="rm fixture",
            description="fixture",
        )
        assert _teams_mod._claim_pending_approval(
            token, user_id="user-b", chat_id="chat-a"
        ) is None
        assert _teams_mod._claim_pending_approval(
            token, user_id="user-a", chat_id="chat-b"
        ) is None
        claimed = _teams_mod._claim_pending_approval(
            token, user_id="user-a", chat_id="chat-a"
        )
        assert claimed is not None
        assert claimed["profile"] == "alberto"
        assert _teams_mod._claim_pending_approval(
            token, user_id="user-a", chat_id="chat-a"
        ) is None
