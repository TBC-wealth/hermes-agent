"""Capability-separated client access for AgentSmith's local UI auth service."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from agentsmith_ui_auth.client import AuthClient


_lock = threading.Lock()
_issuer: AuthClient | None = None
_dashboard: AuthClient | None = None
_internal_issuer: AuthClient | None = None


def _configured(name: str) -> bool:
    value = os.environ.get(name, "").strip()
    return bool(value and Path(value).is_socket())


def issuer_configured() -> bool:
    return _configured("AGENTSMITH_UI_AUTH_ISSUER_SOCKET")


def dashboard_configured() -> bool:
    return _configured("AGENTSMITH_UI_AUTH_DASHBOARD_SOCKET")


def internal_issuer_configured() -> bool:
    return _configured("AGENTSMITH_INTERNAL_AUTH_ISSUER_SOCKET")


def issuer_client() -> AuthClient:
    global _issuer
    path = os.environ.get("AGENTSMITH_UI_AUTH_ISSUER_SOCKET", "").strip()
    if not path:
        raise RuntimeError("UI auth issuer capability is not configured")
    with _lock:
        if _issuer is None:
            _issuer = AuthClient(Path(path))
        return _issuer


def dashboard_client() -> AuthClient:
    global _dashboard
    path = os.environ.get("AGENTSMITH_UI_AUTH_DASHBOARD_SOCKET", "").strip()
    if not path:
        raise RuntimeError("UI auth dashboard capability is not configured")
    with _lock:
        if _dashboard is None:
            _dashboard = AuthClient(Path(path))
        return _dashboard


def internal_issuer_client() -> AuthClient:
    global _internal_issuer
    path = os.environ.get("AGENTSMITH_INTERNAL_AUTH_ISSUER_SOCKET", "").strip()
    if not path:
        raise RuntimeError("internal auth issuer capability is not configured")
    with _lock:
        if _internal_issuer is None:
            _internal_issuer = AuthClient(Path(path))
        return _internal_issuer
