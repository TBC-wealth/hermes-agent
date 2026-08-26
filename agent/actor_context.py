"""Actor context: carry a verified caller through a turn and into its processes.

Hermes establishes who is on the other end of a turn — the platform user, the
profile it routed to, the session and the turn. Anything a turn spawns inherits
that identity in practice but not in evidence: a subprocess can read its own
environment and claim to be anybody, so a host that needs to authorize what a
turn's tools do has nothing trustworthy to go on.

This module carries the identity Hermes already established, and lets a host bind
it to the actual process that runs a tool. It contains no policy: it does not
decide who anybody is, what they may do, or what a handle means. It knows that a
turn has an actor, that a process belongs to a turn, and that turns end.

    context = ActorContext(user_id=..., profile=..., session_id=..., turn_id=...)
    with actor_turn(context):
        ...                        # the turn runs
                                   # a spawned process is bound via the hooks below

A host installs two hooks. Neither is required and both default to no-ops, so an
unconfigured Hermes behaves exactly as before:

``prepare_process_env(context) -> Mapping[str, str]``
    Called immediately before a process is spawned. Whatever it returns is added
    to that process's environment. A child's environment is fixed at exec, so
    this necessarily runs before the child's PID exists.

``bind_process(context, pid, start_time) -> None``
    Called immediately after the process is spawned, with the PID the parent now
    knows and the start time that disambiguates it from a later reuse of the same
    number.

The split exists because those two facts become available at different moments
and neither can be moved. A host that needs both — an environment stamp and a
process binding — cannot get them in one call, and pretending otherwise would
push it toward binding the gateway's own PID, which would make every concurrent
turn in the process indistinguishable.

Concurrency is the point. Several turns run in one gateway process, so the
context is a :class:`contextvars.ContextVar`: each turn sees its own, and a
process spawned by one turn can never be handed another turn's environment.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActorContext:
    """Who a turn belongs to, as Hermes established it.

    ``user_id`` is the platform's verified identifier for the human — the value
    Hermes authenticated, not a display name and not anything the turn's own
    content supplied.
    """

    user_id: str
    profile: str
    session_id: str
    turn_id: str
    #: Free-form, for a host that needs to tell platforms apart.
    platform: str = ""

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "profile": self.profile,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "platform": self.platform,
        }


_current: contextvars.ContextVar[Optional[ActorContext]] = contextvars.ContextVar(
    "hermes_actor_context", default=None
)

#: Host hooks. Both default to doing nothing, so Hermes is unchanged unless a
#: host installs them.
_prepare_env: Optional[Callable[[ActorContext], Mapping[str, str]]] = None
_bind_process: Optional[Callable[[ActorContext, int, Optional[int]], None]] = None
_end_turn: Optional[Callable[[ActorContext, str], None]] = None


def install_hooks(
    *,
    prepare_process_env: Optional[Callable[[ActorContext], Mapping[str, str]]] = None,
    bind_process: Optional[Callable[[ActorContext, int, Optional[int]], None]] = None,
    end_turn: Optional[Callable[[ActorContext, str], None]] = None,
) -> None:
    """Install host hooks. Called once at startup, before any turn runs."""
    global _prepare_env, _bind_process, _end_turn
    if prepare_process_env is not None:
        _prepare_env = prepare_process_env
    if bind_process is not None:
        _bind_process = bind_process
    if end_turn is not None:
        _end_turn = end_turn


def clear_hooks() -> None:
    """Remove every hook. For tests, and for a host shutting down cleanly."""
    global _prepare_env, _bind_process, _end_turn
    _prepare_env = _bind_process = _end_turn = None


def current() -> Optional[ActorContext]:
    """The actor context for this turn, or None outside one."""
    return _current.get()


@contextlib.contextmanager
def actor_turn(context: ActorContext):
    """Run a turn under an actor context, ending it on EVERY exit path.

    Completion, cancellation, interruption, timeout and exception all pass
    through the finally clause. A turn whose context outlived it would leave a
    handle usable after the work stopped, which is the failure this exists to
    prevent.
    """
    token = _current.set(context)
    outcome = "completed"
    try:
        yield context
    except BaseException as exc:  # noqa: BLE001 - re-raised below
        # BaseException on purpose: asyncio.CancelledError and KeyboardInterrupt
        # are exit paths too, and they are the ones a narrower clause misses.
        outcome = type(exc).__name__
        raise
    finally:
        _current.reset(token)
        if _end_turn is not None:
            try:
                _end_turn(context, outcome)
            except Exception:
                # A host that fails to clean up must not take the turn's own
                # result down with it; the failure is logged and the host's own
                # expiry remains the backstop.
                logger.warning("actor end_turn hook failed", exc_info=True)


def process_environment(base: Optional[Mapping[str, str]] = None) -> dict:
    """Environment additions for a process this turn is about to spawn.

    Returns a plain dict, empty when there is no context or no hook, so a caller
    can always ``env.update(process_environment())`` unconditionally.
    """
    context = _current.get()
    if context is None or _prepare_env is None:
        return {}
    try:
        extra = _prepare_env(context)
    except Exception:
        logger.warning("actor prepare_process_env hook failed", exc_info=True)
        return {}
    if not extra:
        return {}
    return {str(key): str(value) for key, value in dict(extra).items()}


def host_start_time(pid: int) -> Optional[int]:
    """A process's start time, which makes a PID unambiguous.

    A PID alone is reusable. Pairing it with the start time is what stops a
    binding outliving its process and being inherited by whatever gets the
    number next. Linux only; returns None elsewhere, and a host that needs it
    decides what that means.
    """
    try:
        tail = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return int(tail[19])
    except (OSError, ValueError, IndexError):
        return None


def bind_spawned_process(pid: int, start_time: Optional[int] = None) -> None:
    """Tell the host which process this turn just spawned.

    Called immediately after the spawn, with the real child PID — never the
    gateway's. Binding the gateway's PID would make every concurrent turn in the
    process look identical, which defeats the point.
    """
    context = _current.get()
    if context is None or _bind_process is None:
        return
    if not isinstance(pid, int) or pid <= 0:
        return
    if start_time is None:
        start_time = host_start_time(pid)
    try:
        _bind_process(context, pid, start_time)
    except Exception:
        logger.warning("actor bind_process hook failed", exc_info=True)


def sanitize_inherited(env: dict) -> dict:
    """Strip actor variables a parent process may already carry.

    A turn's process must receive only what this turn's hook produced. Anything
    inherited from the gateway's own environment, or left over from another
    turn, is removed before the hook's additions are applied — otherwise a stale
    variable would silently make a process look like a turn it does not belong
    to.
    """
    return {
        key: value for key, value in env.items()
        if not str(key).startswith("AGENTSMITH_TURN")
        and not str(key).startswith("HERMES_ACTOR_")
    }
