"""
Tests for the shared local direct-tmux attach helper.

The native CLIs (``omnigent claude`` / ``codex`` / ``cursor`` / ``pi`` …) attach
the user's real terminal straight to the runner-owned tmux pane over its private
socket — lower latency than the WebSocket PTY relay. Since #540 the managed
server runs with ``remain-on-exit on`` so the pane (and session) outlive the
inner CLI's exit. A plain ``tmux attach`` then never returns when the inner CLI
quits: the client stays glued to the frozen dead pane, and because Omnigent
disables the tmux prefix there is no detach key — a hard hang on ``/exit``.

:func:`attach_direct_tmux` fixes this by watching ``#{pane_dead}`` while the
attach runs and detaching the local client the moment the pane dies, so the
foreground attach returns. These tests pin that behavior against a real tmux
server.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.terminals.direct_attach import AttachOutcome, attach_direct_tmux

_HAS_TMUX = shutil.which("tmux") is not None


@contextlib.contextmanager
def _short_socket_dir() -> Iterator[Path]:
    """
    Yield a short-lived directory for a tmux socket.

    The macOS Unix-socket path limit (~104 bytes) is shorter than a typical
    pytest ``tmp_path`` under ``/private/var/folders/...``, so socket paths must
    live somewhere short. ``/tmp`` is short on both macOS and Linux.
    """
    path = Path(tempfile.mkdtemp(prefix="og", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _tmux_base(socket: Path) -> list[str]:
    return ["tmux", "-S", str(socket), "-f", os.devnull]


def _set_persistence(socket: Path) -> None:
    """Apply the #540 ``remain-on-exit`` / ``exit-empty`` options."""
    subprocess.run(
        [*_tmux_base(socket), "set-option", "-gq", "remain-on-exit", "on"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [*_tmux_base(socket), "set-option", "-sq", "exit-empty", "off"],
        check=True,
        capture_output=True,
    )


def _wait_pane_dead(socket: Path, target: str = "main") -> None:
    for _ in range(250):
        out = subprocess.run(
            [*_tmux_base(socket), "list-panes", "-t", target, "-F", "#{pane_dead}"],
            capture_output=True,
            text=True,
        )
        if "1" in out.stdout.split():
            return
        time.sleep(0.02)
    raise AssertionError("inner pane never reported dead")


def _start_dead_pane_session(socket: Path, target: str = "main") -> None:
    """
    Start a session whose pane is dead but *kept* by ``remain-on-exit``.

    The inner command blocks on stdin so the server stays alive while persistence
    is applied; only then is the pane released to exit. A command that exits
    instantly (``sh -c 'exit 0'``) races the default ``exit-empty``, which tears
    the server down before ``remain-on-exit`` is set and leaves ``set-option``
    talking to a dead socket.
    """
    subprocess.run(
        [
            *_tmux_base(socket),
            "new-session",
            "-d",
            "-s",
            target,
            "-x",
            "80",
            "-y",
            "24",
            "sh -c 'read _ignored; exit 0'",
        ],
        check=True,
        capture_output=True,
    )
    _set_persistence(socket)
    # Release the blocked ``read`` so the pane exits *after* persistence is set;
    # remain-on-exit then keeps the dead pane instead of dropping the server.
    subprocess.run(
        [*_tmux_base(socket), "send-keys", "-t", target, "Enter"],
        check=True,
        capture_output=True,
    )
    _wait_pane_dead(socket, target)


@pytest.mark.skipif(not _HAS_TMUX, reason="requires a real tmux binary")
@pytest.mark.asyncio
async def test_attach_returns_exited_when_pane_is_dead(tmp_path: Path) -> None:
    """
    The attach returns ``EXITED`` (not hang) once the inner pane is dead.

    Reproduces the ``/exit`` hang: with ``remain-on-exit on`` a dead-pane session
    keeps any ``tmux attach`` client glued to the frozen pane forever. The helper
    must detect the dead pane, detach the client, and report ``EXITED``.
    """
    with _short_socket_dir() as sock_dir:
        socket = sock_dir / "tmux.sock"
        _start_dead_pane_session(socket)

        primary, secondary = pty.openpty()
        try:
            outcome = await asyncio.wait_for(
                attach_direct_tmux(socket, "main", stdio=secondary),
                timeout=10,
            )
        finally:
            os.close(primary)
            os.close(secondary)
            subprocess.run([*_tmux_base(socket), "kill-server"], capture_output=True)

        assert outcome is AttachOutcome.EXITED


@pytest.mark.skipif(not _HAS_TMUX, reason="requires a real tmux binary")
@pytest.mark.asyncio
async def test_attach_returns_when_socket_path_removed_while_attached() -> None:
    """
    The attach returns even if the socket path is unlinked out from under it.

    Reproduces the real ``/exit`` race: claude exits (dead pane kept by
    ``remain-on-exit``) and the runner's teardown removes the terminal's socket
    *directory* at about the same moment. The tmux server stays alive in-kernel
    with the client still connected, but no path-based ``tmux`` command can reach
    it anymore — so a path-based detach is impossible. The helper must still get
    the foreground attach to return (by terminating the attach child) rather than
    leaving the user's terminal glued to a connection nothing can detach.
    """
    with _short_socket_dir() as sock_dir:
        socket = sock_dir / "tmux.sock"
        _start_dead_pane_session(socket)
        server_pid = int(
            subprocess.run(
                [*_tmux_base(socket), "display-message", "-p", "#{pid}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )

        primary, secondary = pty.openpty()

        async def _yank_socket_after_attached() -> None:
            # Once the attach has registered as a client, unlink the socket path
            # while leaving the server alive in-kernel — exactly what the runner's
            # tempdir cleanup does to a live local attach.
            for _ in range(250):
                clients = subprocess.run(
                    [*_tmux_base(socket), "list-clients", "-t", "main"],
                    capture_output=True,
                    text=True,
                )
                if clients.stdout.strip():
                    break
                await asyncio.sleep(0.02)
            with contextlib.suppress(FileNotFoundError):
                socket.unlink()

        try:
            yanker = asyncio.create_task(_yank_socket_after_attached())
            # Tight bound: the bug was an indefinite hang (and, when the path was
            # still reachable, tmux's own ~8s client timeout). The watcher must
            # terminate the attach child promptly regardless.
            outcome = await asyncio.wait_for(
                attach_direct_tmux(socket, "main", stdio=secondary),
                timeout=4,
            )
            await yanker
        finally:
            os.close(primary)
            os.close(secondary)
            # Path-based kill-server can't reach the orphaned server; kill by pid.
            with contextlib.suppress(ProcessLookupError):
                os.kill(server_pid, 9)

        assert outcome is AttachOutcome.EXITED


@pytest.mark.skipif(not _HAS_TMUX, reason="requires a real tmux binary")
@pytest.mark.asyncio
async def test_attach_returns_detached_when_session_outlives_attach(tmp_path: Path) -> None:
    """
    An external detach (live pane) is reported as ``DETACHED``, not ``EXITED``.

    When the inner CLI is still running and the client is detached out-of-band,
    the helper must keep the session-as-alive verdict so the caller leaves the
    runner-owned terminal up.
    """
    with _short_socket_dir() as sock_dir:
        socket = sock_dir / "tmux.sock"
        subprocess.run(
            [
                *_tmux_base(socket),
                "new-session",
                "-d",
                "-s",
                "main",
                "-x",
                "80",
                "-y",
                "24",
                "sleep 100000",
            ],
            check=True,
            capture_output=True,
        )
        _set_persistence(socket)

        primary, secondary = pty.openpty()

        async def _detach_after_attached() -> None:
            # Give the attach a beat to register as a client, then detach it
            # out-of-band (the pane stays alive — simulating a user/web detach).
            for _ in range(250):
                clients = subprocess.run(
                    [*_tmux_base(socket), "list-clients", "-t", "main"],
                    capture_output=True,
                    text=True,
                )
                if clients.stdout.strip():
                    break
                await asyncio.sleep(0.02)
            subprocess.run(
                [*_tmux_base(socket), "detach-client", "-s", "main"],
                capture_output=True,
            )

        try:
            detacher = asyncio.create_task(_detach_after_attached())
            outcome = await asyncio.wait_for(
                attach_direct_tmux(socket, "main", stdio=secondary),
                timeout=10,
            )
            await detacher
        finally:
            os.close(primary)
            os.close(secondary)
            subprocess.run([*_tmux_base(socket), "kill-server"], capture_output=True)

        assert outcome is AttachOutcome.DETACHED
