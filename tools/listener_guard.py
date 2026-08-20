#!/usr/bin/env python3
"""Exec a terminal command with TCP listener creation denied by seccomp.

The filter is installed in this short-lived wrapper rather than through
``preexec_fn`` in the multi-threaded gateway. Seccomp filters survive exec and
are inherited by descendants, so code hidden behind an innocuous command such
as ``python3 app.py`` cannot evade the boundary.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys


SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000


class ListenerGuardError(RuntimeError):
    """Raised when the requested kernel boundary cannot be installed."""


def install_listener_guard() -> None:
    """Deny ``listen(2)`` for this process and every future descendant."""
    if not sys.platform.startswith("linux"):
        raise ListenerGuardError("listener guard requires Linux seccomp")

    library = ctypes.util.find_library("seccomp")
    if not library:
        raise ListenerGuardError("listener guard requires libseccomp")

    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.restype = None

    context = seccomp.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise ListenerGuardError("seccomp_init failed")
    try:
        syscall = seccomp.seccomp_syscall_resolve_name(b"listen")
        if syscall < 0:
            raise ListenerGuardError("kernel does not expose listen(2)")
        action = SCMP_ACT_ERRNO | errno.EACCES
        result = seccomp.seccomp_rule_add(context, action, syscall, 0)
        if result != 0:
            raise ListenerGuardError(f"seccomp_rule_add failed ({result})")
        result = seccomp.seccomp_load(context)
        if result != 0:
            raise ListenerGuardError(f"seccomp_load failed ({result})")
    finally:
        seccomp.seccomp_release(context)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        print("listener_guard: missing command", file=sys.stderr)
        return 64
    try:
        install_listener_guard()
    except ListenerGuardError as exc:
        print(f"listener_guard: refusing unguarded execution: {exc}", file=sys.stderr)
        return 126
    try:
        os.execvpe(args[0], args, os.environ)
    except OSError as exc:
        print(f"listener_guard: exec failed: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
