"""Tests that MCP tool-level errors (isError) do not trip the per-server circuit breaker.

The circuit breaker in ``tools/mcp_tool.py`` is intended to short-circuit calls
to an MCP server that has failed ``_CIRCUIT_BREAKER_THRESHOLD`` times *at the
transport level* (session dropped, server unreachable, RPC exception). A
``isError`` result with a completed RPC round-trip is the tool's own response
— typically an agent passing bad parameters, validation failure, or a
domain-level denial — and the transport is demonstrably healthy at that point.

These tests lock in the fix from the 2026-08-18 audit: three tool-level
errors in a row must leave ``_server_error_counts[server] == 0`` and the 4th
call must not short-circuit; three transport failures must still open the
breaker.
"""
import json
from unittest.mock import MagicMock

import pytest


pytest.importorskip("mcp.client.auth.oauth2")


# ---------------------------------------------------------------------------
# Helpers (kept aligned with tests/tools/test_mcp_circuit_breaker.py)
# ---------------------------------------------------------------------------


def _install_stub_server(mcp_tool_module, name: str, call_tool_impl):
    """Install a fake MCP server in the module's registry."""
    import threading

    server = MagicMock()
    server.name = name
    session = MagicMock()
    session.call_tool = call_tool_impl
    server.session = session

    ready_flag = threading.Event()
    ready_flag.set()

    class _ReadyAdapter:
        def is_set(self):
            return ready_flag.is_set()

        def clear(self):
            ready_flag.clear()

        def set(self):
            ready_flag.set()

    server._reconnect_event = MagicMock()
    server._ready = _ReadyAdapter()
    server._is_recycled_stdio.return_value = False

    mcp_tool_module._servers[name] = server
    mcp_tool_module._server_error_counts.pop(name, None)
    if hasattr(mcp_tool_module, "_server_breaker_opened_at"):
        mcp_tool_module._server_breaker_opened_at.pop(name, None)
    return server


def _cleanup(mcp_tool_module, name: str) -> None:
    mcp_tool_module._servers.pop(name, None)
    mcp_tool_module._server_error_counts.pop(name, None)
    if hasattr(mcp_tool_module, "_server_breaker_opened_at"):
        mcp_tool_module._server_breaker_opened_at.pop(name, None)


def _make_is_error_result(text: str = "bad parameter"):
    """Build a stub CallToolResult with ``isError=True`` and a text block."""
    result = MagicMock()
    result.isError = True
    block = MagicMock()
    block.text = text
    block.type = "text"
    result.content = [block]
    result.structuredContent = None
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tool_level_errors_do_not_bump_breaker(monkeypatch, tmp_path):
    """Three tool-level isError results in a row must leave the breaker count at 0.

    The RPC round-trip completed every time, so the transport is healthy.
    Tool-level errors are the agent's own fault (bad parameters), not the
    server's. The breaker must stay closed.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    async def _call_tool_is_error(*a, **kw):
        return _make_is_error_result("bad parameter")

    _install_stub_server(mcp_tool, "srv", _call_tool_is_error)
    mcp_tool._ensure_mcp_loop()

    try:
        handler = _make_tool_handler("srv", "tool1", 10.0)

        for i in range(3):
            result = handler({})
            parsed = json.loads(result)
            assert "error" in parsed, f"call {i + 1}: expected tool error, got {parsed}"
            assert "bad parameter" in parsed["error"], f"call {i + 1}: {parsed}"

        assert mcp_tool._server_error_counts.get("srv", 0) == 0, (
            "tool-level errors must not bump the breaker"
        )
        # The breaker-open timestamp must not have been set either.
        assert mcp_tool._server_breaker_opened_at.get("srv") is None, (
            "tool-level errors must not stamp the breaker-open time"
        )
    finally:
        _cleanup(mcp_tool, "srv")


def test_tool_level_errors_do_not_short_circuit_subsequent_calls(monkeypatch, tmp_path):
    """After three tool-level errors, the 4th call must NOT short-circuit — it
    must actually hit the session and produce a fresh tool-level error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    call_count = {"n": 0}

    async def _call_tool_is_error(*a, **kw):
        call_count["n"] += 1
        return _make_is_error_result(f"tool error {call_count['n']}")

    _install_stub_server(mcp_tool, "srv", _call_tool_is_error)
    mcp_tool._ensure_mcp_loop()

    try:
        handler = _make_tool_handler("srv", "tool1", 10.0)

        for _ in range(3):
            handler({})
        assert call_count["n"] == 3

        # 4th call must reach the session — not the short-circuit gate.
        result = handler({})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "tool error 4" in parsed["error"]
        assert call_count["n"] == 4, (
            "4th call must invoke the session; the breaker should still be closed"
        )
    finally:
        _cleanup(mcp_tool, "srv")


def test_transport_failures_still_open_breaker(monkeypatch, tmp_path):
    """Sanity check: three transport-level failures (RPC exceptions) must STILL
    open the breaker. The fix must not regression the original transport-failure
    behavior."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    async def _call_tool_raises(*a, **kw):
        raise RuntimeError("transport down")

    _install_stub_server(mcp_tool, "srv", _call_tool_raises)
    mcp_tool._ensure_mcp_loop()

    try:
        handler = _make_tool_handler("srv", "tool1", 10.0)

        for _ in range(3):
            handler({})

        assert mcp_tool._server_error_counts.get("srv", 0) >= mcp_tool._CIRCUIT_BREAKER_THRESHOLD
        assert mcp_tool._server_breaker_opened_at.get("srv") is not None, (
            "transport failures must still stamp the breaker-open time"
        )

        # Next call must short-circuit with the cooldown message.
        result = handler({})
        parsed = json.loads(result)
        assert "unreachable" in parsed.get("error", "").lower(), parsed
    finally:
        _cleanup(mcp_tool, "srv")


def test_transport_failure_then_tool_error_resets_breaker(monkeypatch, tmp_path):
    """A confirmed transport failure followed by a successful round-trip (tool
    error or success) must fully reset the breaker — the unblock-count
    accumulator should not retain the transport strike."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    async def _call_tool_raises(*a, **kw):
        raise RuntimeError("transport down")

    async def _call_tool_is_error(*a, **kw):
        return _make_is_error_result("tool denied")

    # Start with raises, then switch to isError mid-test.
    impl = {"fn": _call_tool_raises}

    async def _call_tool_dispatch(*a, **kw):
        return await impl["fn"](*a, **kw)

    _install_stub_server(mcp_tool, "srv", _call_tool_dispatch)
    mcp_tool._ensure_mcp_loop()

    try:
        handler = _make_tool_handler("srv", "tool1", 10.0)

        # Two transport failures.
        handler({})
        handler({})
        assert mcp_tool._server_error_counts.get("srv", 0) == 2

        # Switch to tool-level errors; the breaker must reset.
        impl["fn"] = _call_tool_is_error
        result = handler({})
        parsed = json.loads(result)
        assert "tool denied" in parsed["error"]
        assert mcp_tool._server_error_counts.get("srv", 0) == 0, (
            "a successful RPC round-trip with isError must reset the breaker"
        )
    finally:
        _cleanup(mcp_tool, "srv")
