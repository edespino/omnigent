"""
Shared local direct-tmux attach for the native CLIs.

When the omnigent CLI runs on the same machine as the runner, the native
front-ends (``omnigent claude`` / ``codex`` / ``cursor`` / ``pi`` / …) attach the
user's real terminal straight to the runner-owned tmux pane over its private
Unix socket. That is lower-latency than the WebSocket + PTY relay used for the
browser and for non-local runners, because there is no server round-trip — the
local TTY drives tmux directly.

Since #540 the managed tmux server runs with ``remain-on-exit on`` /
``exit-empty off`` so the pane (and therefore the session and server) outlive
the inner CLI's exit — the dead pane stays capturable for diagnostics and the
private socket stays usable for control commands. That persistence breaks a
naive foreground attach: a plain ``tmux attach`` does **not** return when the
inner CLI quits. The client stays glued to the frozen ``Pane is dead`` pane, and
because Omnigent disables the tmux prefix (no detach key) the user's terminal
hard-hangs on ``/exit``.

:func:`attach_direct_tmux` fixes that: it runs the foreground attach while a
sidecar watcher polls ``#{pane_dead}``; the instant the pane dies the watcher
detaches the local client so the attach returns. The exit-vs-detach verdict is
then read from the pane-dead flag (not bare session existence, which
``remain-on-exit`` keeps true past exit), matching the WebSocket path's
4404-vs-4405 semantics in :mod:`omnigent.terminals.ws_bridge`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from enum import Enum
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# How often the sidecar watcher probes the pane-dead flag while the attach is
# live. 200ms keeps the post-``/exit`` detach feeling instant without forking
# tmux more than ~5x/sec.
_PANE_DEAD_POLL_INTERVAL_S = 0.2

# After issuing a clean ``detach-client``, how long to let the attach child exit
# on its own before force-terminating it. A reachable server detaches its client
# in well under this; the grace only matters as a backstop.
_DETACH_GRACE_S = 0.5

# Bound for each one-shot tmux probe/control subprocess so a wedged tmux can't
# hang the watcher (or the final verdict) indefinitely.
_TMUX_PROBE_TIMEOUT_S = 5.0


class AttachOutcome(Enum):
    """
    How a direct tmux attach ended.

    :cvar EXITED: The inner CLI exited (or the session/server is gone). The
        caller owns teardown of the runner-side terminal resource.
    :cvar DETACHED: The attach ended while the inner CLI is still running (the
        user or web client detached). The caller keeps the terminal alive.
    """

    EXITED = "exited"
    DETACHED = "detached"


async def _probe_pane_state(socket_path: Path, tmux_target: str) -> str:
    """
    Probe the target pane and classify it as ``"alive"`` / ``"dead"`` / ``"gone"``.

    ``list-panes`` errors on an unknown/dead session (unlike ``display-message``,
    which silently falls back to another pane), so a non-zero exit means the
    server/session is gone. A ``"1"`` line means the inner process exited but
    ``remain-on-exit`` kept the pane. Anything else is a live pane.

    Fails conservative: any spawn/timeout error is reported as ``"alive"`` so the
    watcher never tears down a healthy attach on a transient tmux hiccup.

    :param socket_path: Runner tmux server socket path.
    :param tmux_target: tmux ``-t`` target, e.g. ``"main"``.
    :returns: One of ``"alive"``, ``"dead"``, ``"gone"``.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            str(socket_path),
            "list-panes",
            "-t",
            tmux_target,
            "-F",
            "#{pane_dead}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        _LOGGER.debug("direct-attach: pane-state probe spawn failed", exc_info=True)
        return "alive"
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TMUX_PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, OSError):
        _LOGGER.debug("direct-attach: pane-state probe timed out", exc_info=True)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return "alive"
    if proc.returncode != 0:
        return "gone"
    panes = stdout.decode().split()
    return "dead" if "1" in panes else "alive"


async def _detach_local_client(socket_path: Path, tmux_target: str) -> None:
    """
    Detach every client attached to *tmux_target* (best effort).

    Sent once the pane is dead so the foreground ``tmux attach`` child returns.
    ``detach-client`` (not ``kill-session``) is deliberate: it frees the local
    terminal without destroying the runner-owned session, leaving server-side
    teardown to the caller / runner watcher.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            str(socket_path),
            "detach-client",
            "-s",
            tmux_target,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        _LOGGER.debug("direct-attach: detach-client spawn failed", exc_info=True)
        return
    with contextlib.suppress(asyncio.TimeoutError, OSError):
        await asyncio.wait_for(proc.wait(), timeout=_TMUX_PROBE_TIMEOUT_S)


async def _end_attach_on_inner_exit(
    socket_path: Path,
    tmux_target: str,
    process: asyncio.subprocess.Process,
) -> None:
    """
    Poll the pane and make the foreground attach return once the inner CLI exits.

    Runs as a sidecar task for the lifetime of the attach and is cancelled by
    :func:`attach_direct_tmux` the instant the attach child exits on its own (a
    user/web detach of a still-live pane).

    Two ways the inner CLI can be gone:

    - ``"dead"`` — ``remain-on-exit`` kept the pane after the CLI exited, server
      still reachable by path. Issue ``detach-client`` for a clean exit that
      restores the terminal.
    - ``"gone"`` — the path-based probe can't reach the server. This is the
      ``/exit`` race: the runner's teardown unlinked the socket directory while
      the ``tmux attach`` client is still wired to the server over the in-kernel
      socket, so no path-based ``tmux`` command (including ``detach-client``) can
      reach it. A clean detach is impossible.

    In both cases, after a short grace for any clean detach to land, the attach
    child is force-terminated if it hasn't exited — guaranteeing the user's
    terminal is freed regardless of socket reachability. ``SIGTERM`` to
    ``tmux attach`` makes it exit and restore terminal state.
    """
    while True:
        state = await _probe_pane_state(socket_path, tmux_target)
        if state in ("dead", "gone"):
            if state == "dead":
                # Clean, terminal-restoring exit when the server is reachable.
                await _detach_local_client(socket_path, tmux_target)
            try:
                # If the clean detach landed, the attach exits here and
                # :func:`attach_direct_tmux` cancels this task before the
                # terminate below ever runs.
                await asyncio.wait_for(process.wait(), timeout=_DETACH_GRACE_S)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            return
        await asyncio.sleep(_PANE_DEAD_POLL_INTERVAL_S)


async def attach_direct_tmux(
    socket_path: Path,
    tmux_target: str,
    *,
    stdio: int | None = None,
    env: dict[str, str] | None = None,
) -> AttachOutcome:
    """
    Attach the current terminal directly to the runner-owned tmux pane.

    ``TMUX`` is dropped from the child environment so a user who runs the native
    CLI from inside their own tmux can still attach to Omnigent's private server.
    A sidecar watcher makes the attach return the moment the inner CLI exits —
    cleanly via ``detach-client`` when the server is still reachable, or by
    terminating the attach child when the socket path has been unlinked out from
    under it (the ``/exit`` teardown race) — so ``/exit`` never hangs on the
    ``remain-on-exit`` dead pane.

    :param socket_path: Runner tmux server socket path.
    :param tmux_target: tmux ``-t`` target to attach, e.g. ``"main"``.
    :param stdio: Optional file descriptor to use for the child's
        stdin/stdout/stderr. ``None`` (production) inherits the real TTY; tests
        pass a PTY slave fd so the attach has a controlling terminal.
    :param env: Optional environment for the attach child, used verbatim. ``None``
        (default) inherits the current environment with ``TMUX`` dropped. Callers
        that restrict the attach env to an allowlist (e.g. kiro) pass it here.
    :returns: :attr:`AttachOutcome.DETACHED` when the session outlives the attach
        with a live pane (user/web detached), else :attr:`AttachOutcome.EXITED`.
    """
    if env is None:
        # ``os.environ.copy()`` (not ``dict(os.environ)``) avoids tripping the
        # exfil-scan wholesale-environ-dump shape; this only drops TMUX before
        # handing the env to the local tmux attach subprocess.
        env = os.environ.copy()
        env.pop("TMUX", None)
    process = await asyncio.create_subprocess_exec(
        "tmux",
        "-S",
        str(socket_path),
        "-f",
        os.devnull,
        "attach",
        "-t",
        tmux_target,
        env=env,
        stdin=stdio,
        stdout=stdio,
        stderr=stdio,
    )
    watcher = asyncio.create_task(
        _end_attach_on_inner_exit(socket_path, tmux_target, process)
    )
    try:
        await process.wait()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    # Classify on the pane-dead flag, not bare session existence: remain-on-exit
    # keeps the session alive past the inner CLI's exit, so only a live pane
    # means a genuine detach.
    state = await _probe_pane_state(socket_path, tmux_target)
    return AttachOutcome.DETACHED if state == "alive" else AttachOutcome.EXITED
