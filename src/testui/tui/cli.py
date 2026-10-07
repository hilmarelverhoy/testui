from __future__ import annotations

import argparse
import shlex


def main(argv=None):
    p = argparse.ArgumentParser(prog="testui", description="Live TUI for Django test runs")
    p.add_argument("--host", default="127.0.0.1", help="listen address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--jsonl", help="tail this JSONL event file instead of listening on TCP")
    p.add_argument("--local-root", help="host path of the project root (maps the runner's cwd)")
    p.add_argument("--map", action="append", default=[], metavar="REMOTE=LOCAL",
                   help="extra path prefix mapping, repeatable")
    p.add_argument("--nvim-socket", help="nvim --listen socket (default $TESTUI_NVIM_SOCKET or /tmp/nvim.sock)")
    p.add_argument("--tmux-target", help="tmux target to focus after opening (e.g. editor:1.0)")
    p.add_argument("--run-cmd", help="command that runs tests for a label (default: bin/dt, or $TESTUI_RUN_CMD); `r` calls it as: CMD LABEL")
    args = p.parse_args(argv)
    maps = []
    for m in args.map:
        remote, sep, local = m.partition("=")
        if not sep:
            p.error("--map expects REMOTE=LOCAL, got %r" % m)
        maps.append((remote, local))
    from .app import TestuiApp

    TestuiApp(args.host, args.port, args.jsonl, args.local_root, maps,
              args.nvim_socket, args.tmux_target,
              shlex.split(args.run_cmd) if args.run_cmd else None).run()


if __name__ == "__main__":
    main()
