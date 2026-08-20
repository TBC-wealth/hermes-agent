from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools.environments.local import LocalEnvironment


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "tools/listener_guard.py"


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="seccomp is Linux-only",
)


def guarded(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--", *command],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_hidden_python_app_cannot_listen(tmp_path: Path):
    app = tmp_path / "app.py"
    app.write_text(textwrap.dedent("""
        import socket
        listener = socket.socket()
        listener.bind(("0.0.0.0", 0))
        listener.listen()
    """))

    result = guarded(sys.executable, str(app))

    assert result.returncode != 0
    assert "PermissionError" in result.stderr


def test_outbound_connection_still_works():
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(5)
    host, port = server.getsockname()
    code = (
        "import socket; "
        f"s=socket.create_connection(({host!r}, {port}), timeout=3); "
        "s.sendall(b'ok'); s.close()"
    )
    try:
        result = guarded(sys.executable, "-c", code)
        connection, _ = server.accept()
        with connection:
            assert connection.recv(2) == b"ok"
    finally:
        server.close()

    assert result.returncode == 0, result.stderr


def test_local_environment_wraps_even_innocuous_command(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(LocalEnvironment, "init_session", lambda self: None)
    environment = LocalEnvironment(cwd=str(tmp_path), listener_guard=True)
    process = environment._run_bash(
        f"{sys.executable} -c 'import socket; s=socket.socket(); "
        "s.bind((\"0.0.0.0\", 0)); s.listen()'"
    )
    output, _ = process.communicate(timeout=10)

    assert process.returncode != 0
    assert "PermissionError" in output
