# testui

A terminal UI for Django test runs. Each test is one line (status, name, duration,
first line of the error). Expand a failure to see the traceback frames, and press
`o` to open the assert line in a running neovim.

```
tests ──(TCP, live)──► testui (Textual) ──► nvim --server /tmp/nvim.sock
```

The runner (`testui.runner.TestuiRunner`) is a Django `DiscoverRunner` that streams
JSON events; the TUI listens for them. They are separate processes, so a crashing or
hanging test can't take the TUI down.

## Quick start (api repo, runs in Docker)

1. **nvim**, in tmux: `nvim --listen /tmp/nvim.sock`
2. **TUI**, in another pane:
   ```
   ~/test_handler/.venv/bin/testui --local-root ~/api
   ```
3. **Tests**, in a third pane:
   ```
   ~/test_handler/bin/dt                 # all tests
   ~/test_handler/bin/dt common          # a label, like manage.py test
   ```

`bin/dt` runs `docker-compose run --rm web python manage.py test --testrunner=testui.runner.TestuiRunner`
with `docker-compose.testui.yml` as an overlay. The overlay mounts testui into the
container and points it at `host.docker.internal:8765`. Nothing is added to `~/api`.

`--local-root` maps the container's `/usr/src/app` to your checkout so nvim opens the
right files.

## Layout

Two fold levels: **app** (first part of the test id, e.g. `accounts`) > **test class**
(`tests.test_x.FooTest`) > test. Group lines show pass/fail counts. Groups start collapsed and
open themselves the first time a failure appears in them (your own folding sticks).

## Keys

| Key | Action |
|---|---|
| `j` / `k`, arrows | down / up |
| `gg` / `G` | top / bottom |
| `ctrl-d` / `ctrl-u` | half page down / up |
| `{` / `}` | previous / next app or class line |
| `ctrl-n` / `ctrl-p` | next / previous failure (unfolds what's needed) |
| `n` / `N` | next / previous `/` match (wraps around) |
| `r` | re-run the test, class or app under the cursor (see below) |
| `l` / `Enter` | unfold, or step into the first child |
| `h` | fold, or jump to the parent |
| `za` `zo` `zc` | toggle / open / close fold under the cursor |
| `zR` / `zM` | open / close all folds |
| `/` | filter tests (words are ANDed, case-insensitive, matches the full id); `Enter` keeps it and jumps to the first match, `Esc` clears |
| `f` | failures only (combines with `/`) |
| `x` | expand / collapse all failures |
| `o` | open in nvim: the frame under the cursor, or the assert frame if the cursor is on the test line |
| `y` | yank the test / class / app id to the clipboard (paste after `manage.py test`) |
| `c` | clear results |
| `q` | quit |

In an expanded test, `★` marks the assert frame (the deepest frame in your project code).
Frames in libraries are shown too and can be opened.

## Re-running (`r`)

`r` runs `bin/dt <label>` for the item under the cursor: a test method, a class, or a whole app.
The new results are merged into the current tree (a test that now passes flips to green, everything
else stays), so you keep the rest of the run. If the command fails before any events arrive (for
example Docker isn't up), the last lines of its output are shown in a notification.

## Options

| Flag | Meaning |
|---|---|
| `--port N` / `--host H` | where the TUI listens (default `127.0.0.1:8765`) |
| `--local-root DIR` | host path of the project root |
| `--map REMOTE=LOCAL` | extra path prefix mapping, repeatable |
| `--nvim-socket PATH` | default `$TESTUI_NVIM_SOCKET` or `/tmp/nvim.sock` |
| `--tmux-target T` | focus this tmux pane after opening (e.g. `editor:1.0`) |
| `--run-cmd CMD` | what `r` runs, called as `CMD LABEL` (default `bin/dt`, or `$TESTUI_RUN_CMD`) |
| `--jsonl FILE` | tail an event file instead of listening on TCP |

If no nvim socket exists, `o` falls back to launching a new nvim and returns to the TUI on exit.

Runner environment variables: `TESTUI_HOST` (default `host.docker.internal`),
`TESTUI_PORT` (`8765`), `TESTUI_JSONL` (also write events to a file), `TESTUI_DISABLE`.

Without Docker:
```
TESTUI_HOST=127.0.0.1 python manage.py test --testrunner=testui.runner.TestuiRunner
```
(the testui package must be importable, e.g. `PYTHONPATH=~/test_handler/src`).

## Robustness

- The runner side is stdlib only and fail-silent: every hook is wrapped, sending uses a bounded
  queue and a background thread, and a dead or slow TUI never slows or breaks tests.
- It captures `socket`, `json` and `time` at import and connects without
  `socket.create_connection`, so tests that mock them can't break reporting.
- Paths are classified using the directory captured at start, so tests that `chdir` are fine.
- `test_start` is flushed before each test, so after a hard crash (`os._exit`, segfault) the
  last started test is known. The TUI shows `no heartbeat - run died? last test: ...`.
- If the run itself raises, the TUI shows `ABORTED`.
- Event lines are truncated (large messages) and the TUI skips malformed lines and other runs' events.
- `--parallel` works (frames are parsed from the formatted traceback text).

## Development

```
cd ~/test_handler
.venv/bin/python -m pytest tests
```

Layout: `src/testui/{protocol,sender,runner}.py` run in the test process;
`src/testui/tui/` is the Textual app, the TCP/JSONL sources and the nvim jump.
# testui
