"""Phase 4: lifecycle guard + per-profile observability."""
import pytest


class TestServedProfilesStatus:
    def test_write_and_read_served_profiles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import importlib
        import gateway.status as status
        importlib.reload(status)
        try:
            status.write_runtime_status(
                gateway_state="running", served_profiles=["default", "coder"]
            )
            rec = status.read_runtime_status()
            assert rec.get("served_profiles") == ["default", "coder"]
        finally:
            importlib.reload(status)


class TestNamedProfileMultiplexerGuard:
    """_guard_named_profile_under_multiplexer is inert unless all conditions hold."""


    def test_force_bypasses(self, monkeypatch):
        from hermes_cli import gateway as gw
        # Even if it looks like a named profile, force returns immediately.
        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        gw._guard_named_profile_under_multiplexer(force=True)

    def test_inert_when_no_default_gateway_running(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw
        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: tmp_path
        )
        # No gateway.pid in tmp_path => no running default gateway => no raise.
        gw._guard_named_profile_under_multiplexer(force=False)

    def _fake_running_default_gateway(self, monkeypatch, tmp_path):
        """Make the guard believe a live default gateway exists at tmp_path."""
        from hermes_cli import gateway as gw
        import gateway.status as status

        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: tmp_path
        )
        (tmp_path / "gateway.pid").write_text("12345", encoding="utf-8")
        monkeypatch.setattr(status, "_read_pid_record", lambda p: {"pid": 12345})
        monkeypatch.setattr(status, "_pid_from_record", lambda rec: 12345)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)


class TestRunAgentFailClosedOnMissingProfile:
    """A routed turn whose profile is missing/unresolvable must never run the
    agent nor fall back to the root identity (agentsmith #94 item 2).

    ``GatewayRunner._run_agent`` is the dispatch site immediately above
    ``_resolve_profile_home_for_source``: when multiplexing is on, it resolves
    the profile home BEFORE entering ``_profile_runtime_scope`` /
    ``_run_agent_inner``, so a ``ProfileResolutionError`` there means the
    agent never runs at all — the strongest form of "no agent turn".
    """

    @staticmethod
    def _make_runner(multiplex: bool):
        from gateway.config import GatewayConfig
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=multiplex)
        return runner

    def test_missing_profile_raises_and_never_runs_inner(self, monkeypatch):
        import asyncio
        from unittest import mock

        from gateway.run import GatewayRunner, ProfileResolutionError

        runner = self._make_runner(multiplex=True)
        inner = mock.AsyncMock(return_value={"final_response": "should not run"})
        runner._run_agent_inner = inner

        source = mock.MagicMock()
        source.profile = "missing"
        source.platform.value = "discord"
        source.chat_id = "123"

        with mock.patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            side_effect=ProfileResolutionError("routed profile 'missing' does not exist"),
        ):
            with pytest.raises(ProfileResolutionError):
                asyncio.run(
                    runner._run_agent(
                        message="hi",
                        context_prompt="",
                        history=[],
                        source=source,
                        session_id="s1",
                    )
                )

        # The strongest assertion of "no agent turn": the inner runner that
        # would actually build/run the AIAgent is never awaited.
        inner.assert_not_awaited()

    def test_multiplex_off_reaches_inner_unaffected(self):
        """Sanity check: single-profile gateways are completely untouched —
        _run_agent never even calls _resolve_profile_home_for_source."""
        import asyncio
        from unittest import mock

        from gateway.run import GatewayRunner

        runner = self._make_runner(multiplex=False)
        inner = mock.AsyncMock(return_value={"final_response": "ok"})
        runner._run_agent_inner = inner

        source = mock.MagicMock()
        source.profile = None

        with mock.patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
        ) as mock_resolve:
            result = asyncio.run(
                runner._run_agent(
                    message="hi",
                    context_prompt="",
                    history=[],
                    source=source,
                    session_id="s1",
                )
            )

        mock_resolve.assert_not_called()
        inner.assert_awaited_once()
        assert result == {"final_response": "ok"}


