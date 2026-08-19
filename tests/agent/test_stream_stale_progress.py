"""Tests for progress-aware stale-stream detection (#104).

The stale-stream detector used to reset its timer on ANY streaming chunk —
including empty SSE keep-alive pings — so a provider that kept trickling
*something* every few minutes could stall a turn for 1000+ seconds without
the detector ever firing (Alberto's session, call #146, 1,049s of silence
that the pre-existing 300s stale threshold never caught).

Covers:
  (a) a stream of continuous but PROGRESS-LESS chunks still trips the
      stale-stream detector — the timer no longer resets on empty /
      keep-alive chunks, only on content/reasoning/tool-call deltas or a
      finish_reason.
  (b) a stream of continuous progress (content deltas) does NOT trip the
      detector, even with per-chunk gaps that would look stale on their
      own — progress chunks still reset the timer.
  (c) HERMES_STREAM_MAX_SECONDS enforces a wall-clock ceiling per attempt,
      independent of progress — it fires even while content keeps
      arriving (the case the stale detector alone can never catch) — and,
      combined with the retry-after-partial flag the agentsmith unit sets
      alongside it, the turn still completes via a fresh retry.
  (d) HERMES_STREAM_RETRY_AFTER_PARTIAL=1 retries a transient error after
      partial delivery (discarding the partial text) instead of giving
      up; unset, the pre-existing give-up-with-stub behaviour is
      preserved.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from hermes_constants import PARTIAL_STREAM_STUB_ID


def _make_agent(**overrides):
    from run_agent import AIAgent

    defaults = dict(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(overrides)
    agent = AIAgent(**defaults)
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


def _delta_chunk(content=None, finish_reason=None):
    delta = SimpleNamespace(
        content=content, tool_calls=None, reasoning_content=None, reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=None, usage=None)


def _empty_chunk():
    """A chunk with a delta but no content/reasoning/tool_calls/finish_reason
    — the shape of an SSE keep-alive ping."""
    return _delta_chunk(content=None, finish_reason=None)


class _CapturedDiagLog:
    """Stand-in for ``agent._log_stream_retry`` that records every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

    def kinds(self):
        return [c.get("kind") for c in self.calls]

    def diag_for(self, kind):
        for c in reversed(self.calls):
            if c.get("kind") == kind:
                return c.get("diag") or {}
        return {}


class TestProgressAwareStaleDetection:
    """(a) + (b): the reset gate distinguishes progress from keep-alives."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_continuous_empty_chunks_still_trip_stale_detector(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """(a) A stream that keeps yielding EMPTY chunks faster than the
        stale timeout must still trip the detector — before #104, any
        chunk (including empty ones) reset the timer, so a continuously
        (but emptily) chattering stream like this would never have been
        caught."""
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.15")
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")

        def _keepalive_stream():
            # 40 empty chunks, 0.05s apart — well inside the 0.15s stale
            # threshold on a per-chunk basis — continuous chunk arrival,
            # zero progress the whole way.
            for _ in range(40):
                time.sleep(0.05)
                yield _empty_chunk()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _keepalive_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        diag_log = _CapturedDiagLog()
        agent._log_stream_retry = diag_log

        with pytest.raises(Exception):
            agent._interruptible_streaming_api_call({})

        assert "stale_kill" in diag_log.kinds(), (
            "A stream of continuous but progress-less chunks must still "
            "trip the stale-stream detector (#104) — the timer must reset "
            "only on content/reasoning/tool-call/finish_reason chunks."
        )
        stale_diag = diag_log.diag_for("stale_kill")
        assert stale_diag.get("empty_chunks", 0) > 0
        assert stale_diag.get("progress_chunks", 0) == 0

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_continuous_content_deltas_never_trip_stale_detector(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """(b) A stream that keeps delivering real content, even with
        per-chunk gaps, must complete normally — progress chunks keep
        resetting the timer, so the (much larger) stale threshold is
        never actually approached."""
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "1.0")

        def _content_stream():
            for i in range(5):
                time.sleep(0.15)
                yield _delta_chunk(content=f"word{i} ")
            yield _delta_chunk(finish_reason="stop")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _content_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        diag_log = _CapturedDiagLog()
        agent._log_stream_retry = diag_log

        response = agent._interruptible_streaming_api_call({})

        assert "stale_kill" not in diag_log.kinds()
        assert response.id != PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == "stop"
        assert "word0" in response.choices[0].message.content


class TestStreamMaxSecondsCeiling:
    """(c) HERMES_STREAM_MAX_SECONDS — a wall-clock ceiling independent of
    progress, killed on the same path as a stale timeout, and (combined
    with the retry-after-partial flag the agentsmith unit sets alongside
    it) still lets the turn complete via a fresh retry."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_ceiling_kills_a_stream_that_never_goes_stale(
        self, _mock_close, mock_create, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "30")
        monkeypatch.setenv("HERMES_STREAM_MAX_SECONDS", "0.2")
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")
        monkeypatch.setenv("HERMES_STREAM_RETRY_AFTER_PARTIAL", "1")

        call_count = {"n": 0}

        def _factory(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                def _runaway_stream():
                    # Continuous real progress — this would NEVER trip the
                    # stale-stream detector — but keeps running well past
                    # the 0.2s wall-clock ceiling.
                    for i in range(30):
                        time.sleep(0.03)
                        yield _delta_chunk(content=f"chunk{i} ")
                return _runaway_stream()

            def _clean_finish():
                yield _delta_chunk(content="A fresh complete answer.")
                yield _delta_chunk(finish_reason="stop")
            return _clean_finish()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _factory
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        diag_log = _CapturedDiagLog()
        agent._log_stream_retry = diag_log

        response = agent._interruptible_streaming_api_call({})

        assert "ceiling_kill" in diag_log.kinds(), (
            "HERMES_STREAM_MAX_SECONDS must kill a stream that keeps "
            "producing real progress once it exceeds the wall-clock "
            "ceiling — the stale-stream detector alone would never catch "
            "a runaway-but-live stream."
        )
        assert mock_client.chat.completions.create.call_count == 2, (
            "The ceiling kill must go through the same retry path as a "
            "stale kill, so the turn recovers on a fresh attempt."
        )
        assert response.id != PARTIAL_STREAM_STUB_ID
        assert response.choices[0].message.content == "A fresh complete answer."

    def test_ceiling_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_STREAM_MAX_SECONDS", raising=False)
        from utils import env_float
        assert env_float("HERMES_STREAM_MAX_SECONDS", 0.0) == 0.0


class TestRetryAfterPartialFlag:
    """(d) HERMES_STREAM_RETRY_AFTER_PARTIAL=1 retries a transient
    partial-delivery drop (discarding the partial text) instead of giving
    up; unset, the old give-up-with-stub behaviour is preserved."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_flag_set_retries_transient_partial_drop(
        self, _mock_close, mock_create, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")
        monkeypatch.setenv("HERMES_STREAM_RETRY_AFTER_PARTIAL", "1")

        call_count = {"n": 0}

        def _factory(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                def _dying_stream():
                    yield _delta_chunk(content="Here's the start of my answer")
                    raise httpx.RemoteProtocolError("peer closed connection")
                return _dying_stream()

            def _clean_finish():
                yield _delta_chunk(content="A brand new complete answer.")
                yield _delta_chunk(finish_reason="stop")
            return _clean_finish()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _factory
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Here's the start of my answer"

        response = agent._interruptible_streaming_api_call({})

        assert mock_client.chat.completions.create.call_count == 2, (
            "HERMES_STREAM_RETRY_AFTER_PARTIAL=1 must retry a transient "
            "partial-delivery drop (discarding the partial text) instead "
            "of giving up after the first attempt."
        )
        assert response.id != PARTIAL_STREAM_STUB_ID
        assert response.choices[0].message.content == "A brand new complete answer."

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_flag_unset_preserves_old_give_up_behaviour(
        self, _mock_close, mock_create, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")
        monkeypatch.delenv("HERMES_STREAM_RETRY_AFTER_PARTIAL", raising=False)

        def _dying_stream():
            yield _delta_chunk(content="Here's the start of my answer")
            raise httpx.RemoteProtocolError("peer closed connection")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _dying_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Here's the start of my answer"

        response = agent._interruptible_streaming_api_call({})

        assert mock_client.chat.completions.create.call_count == 1, (
            "Without the flag, a text-only partial drop with no tool call "
            "in flight must NOT be silently retried — pre-existing "
            "behaviour."
        )
        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].message.content == "Here's the start of my answer"
