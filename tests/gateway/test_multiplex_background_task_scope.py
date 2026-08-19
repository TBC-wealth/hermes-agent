"""Regression: background tasks respect profile secret scope when multiplexing.

Issue #60726: /background spawns _run_background_task as a fire-and-forget
asyncio task with no profile scope, so _resolve_session_agent_runtime()'s
credential reads raise UnscopedSecretError when multiplex_profiles is on.
The fix wraps the task body in _profile_runtime_scope, mirroring _run_agent.
"""
import asyncio
from pathlib import Path
from unittest import mock

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner


def _make_runner(multiplex: bool) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    return runner


class TestBackgroundTaskProfileScope:
    """_run_background_task installs _profile_runtime_scope when multiplexing is active."""

    def test_wraps_in_profile_scope_when_multiplex_active(self):
        runner = _make_runner(multiplex=True)
        inner = mock.AsyncMock(return_value=None)
        runner._run_background_task_inner = inner

        source = mock.MagicMock()
        source.profile = "test_profile"

        with mock.patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            return_value=Path("/fake/profile"),
        ), mock.patch("gateway.run._profile_runtime_scope") as scope:
            scope.return_value.__enter__ = mock.MagicMock()
            scope.return_value.__exit__ = mock.MagicMock(return_value=False)
            asyncio.run(
                runner._run_background_task(
                    prompt="test", source=source, task_id="bg_test"
                )
            )

        scope.assert_called_once_with(Path("/fake/profile"))
        inner.assert_awaited_once()


class TestBackgroundTaskFailClosed:
    """A missing/unresolvable routed profile refuses the background task
    outright instead of falling back to the root identity (agentsmith #94
    item 2) — this is fire-and-forget, so there is no caller to hand a
    reply to; the fix notifies the chat directly and never runs the task.
    """

    def test_profile_resolution_error_skips_inner_and_notifies_chat(self):
        from gateway.run import ProfileResolutionError

        runner = _make_runner(multiplex=True)
        inner = mock.AsyncMock(return_value=None)
        runner._run_background_task_inner = inner

        fake_adapter = mock.AsyncMock()
        runner._adapter_for_source = mock.MagicMock(return_value=fake_adapter)
        runner._thread_metadata_for_source = mock.MagicMock(return_value={})

        source = mock.MagicMock()
        source.profile = "missing"
        source.platform.value = "discord"
        source.chat_id = "123"

        with mock.patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            side_effect=ProfileResolutionError("routed profile 'missing' does not exist"),
        ):
            asyncio.run(
                runner._run_background_task(
                    prompt="test", source=source, task_id="bg_test"
                )
            )

        # No agent turn: the inner implementation is never awaited.
        inner.assert_not_awaited()
        # The chat gets told directly (fire-and-forget has no other caller).
        fake_adapter.send.assert_awaited_once()
        sent_text = fake_adapter.send.call_args.args[1]
        assert "bg_test" in sent_text
        assert "administrator" in sent_text or "not configured" in sent_text.lower() or "isn't configured" in sent_text


