"""Open a file:line in a running neovim (via --listen socket) or a new one."""
from __future__ import annotations

import asyncio
import os
import stat

DEFAULT_SOCKET = "/tmp/nvim.sock"
_FNAMEESCAPE = " \t\n*?[{`$\\%#'\"|!<"


def is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def edit_keys(path, line):
    """Keys for --remote-send: leave any mode, :edit +LINE FILE, centre."""
    escaped = "".join("\\" + c if c in _FNAMEESCAPE else c for c in path)
    escaped = escaped.replace("<", "<lt>")
    return "<C-\\><C-n>:edit +%d %s<CR>zz" % (line, escaped)


async def _run(*argv, capture=False):
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,  # don't share the tty: nvim sets it O_NONBLOCK
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 5)
    except asyncio.TimeoutError:
        proc.kill()
        return (1, "timed out", "") if capture else (1, "timed out")
    err = (err or b"").decode(errors="replace").strip()
    if capture:
        return proc.returncode, err, (out or b"").decode(errors="replace").strip()
    return proc.returncode, err


async def open_in_running_nvim(path, line, socket_path, tmux_target=None):
    """Returns (ok, message). ok=False means no usable nvim server."""
    if not is_socket(socket_path):
        return False, "no nvim socket at %s (start nvim with --listen %s)" % (socket_path, socket_path)
    code, err = await _run("nvim", "--server", socket_path, "--remote-send", edit_keys(path, line))
    if code != 0:
        return False, "nvim --remote-send failed: %s" % (err or code)
    if not tmux_target:
        # ask nvim which tmux pane it lives in
        _, _, out = await _run("nvim", "--server", socket_path, "--remote-expr",
                               "getenv('TMUX_PANE')", capture=True)
        tmux_target = out if out.startswith("%") else None
    if tmux_target:
        await _run("tmux", "switch-client", "-t", tmux_target)
        await _run("tmux", "select-window", "-t", tmux_target)
        await _run("tmux", "select-pane", "-t", tmux_target)
    return True, "opened %s:%d" % (path, line)
