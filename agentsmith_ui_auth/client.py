"""Persistent, non-inheritable JSON client for one auth-service capability socket."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path


class AuthClientError(RuntimeError):
    def __init__(self, code: str, message: str = "Authentication service request failed."):
        super().__init__(message)
        self.code = code


class AuthClient:
    def __init__(self, socket_path: Path, *, timeout: float = 5.0) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._reader = None
        self._pid = os.getpid()
        self._counter = 0
        self._lock = threading.Lock()
        os.register_at_fork(after_in_child=self._after_fork)

    def _after_fork(self) -> None:
        self._close_unlocked()
        self._pid = -1

    def _close_unlocked(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
        self._reader = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _connect(self) -> None:
        if os.getpid() != self._pid:
            raise AuthClientError("fork_denied")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.set_inheritable(False)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self.socket_path)
        except OSError as exc:
            connection.close()
            raise AuthClientError("unavailable") from exc
        self._socket = connection
        self._reader = connection.makefile("rb")

    def call(self, method: str, **params):
        with self._lock:
            if os.getpid() != self._pid:
                raise AuthClientError("fork_denied")
            if self._socket is None:
                self._connect()
            self._counter += 1
            request_id = self._counter
            body = json.dumps(
                {"id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            try:
                self._socket.sendall(body)
                raw = self._reader.readline(65537)
            except OSError as exc:
                self._close_unlocked()
                raise AuthClientError("unavailable") from exc
            if not raw or len(raw) > 65536:
                self._close_unlocked()
                raise AuthClientError("protocol_error")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._close_unlocked()
                raise AuthClientError("protocol_error") from exc
            if response.get("id") != request_id:
                self._close_unlocked()
                raise AuthClientError("protocol_error")
            if not response.get("ok"):
                raise AuthClientError(str(response.get("error") or "request_failed"))
            return response.get("result")

    def issue(self, **params):
        return self.call("issue", **params)

    def confirm(self, **params):
        return self.call("confirm", **params)

    def logout_principal(self, **params):
        return self.call("logout_principal", **params)

    def status(self, **params):
        return self.call("status", **params)

    def redeem(self, **params):
        return self.call("redeem", **params)

    def collect(self, **params):
        return self.call("collect", **params)

    def verify(self, **params):
        return self.call("verify", **params)

    def logout_session(self, **params):
        return self.call("logout_session", **params)

    def revocations(self, **params):
        return self.call("revocations", **params)

    def acknowledge(self, **params):
        return self.call("acknowledge", **params)

    def health(self, **params):
        return self.call("health", **params)
