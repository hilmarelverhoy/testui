"""Textual front-end: app > class > test tree, vim-style navigation and folds."""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Input, Static, Tree

from . import nvim
from .model import BAD, STATUS_ICON, Store, TestResult, label_for, split_id
from .server import serve_tcp, tail_jsonl

MAX_TB_LINES = 200


def _dfs(node):
    """All descendants in tree order (including collapsed ones)."""
    for child in node.children:
        yield child
        yield from _dfs(child)


def default_run_cmd():
    env = os.environ.get("TESTUI_RUN_CMD")
    if env:
        return shlex.split(env)
    dt = Path(__file__).resolve().parents[3] / "bin" / "dt"
    return [str(dt)] if dt.exists() else None


class TestuiApp(App):
    TITLE = "testui"
    ENABLE_COMMAND_PALETTE = False  # frees ctrl+p
    CSS = """
    Tree { height: 1fr; padding: 0 1; }
    #search { dock: bottom; height: 3; margin-bottom: 1; }
    #status { height: 1; dock: bottom; padding: 0 1; background: $panel; }
    """
    BINDINGS = [
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
        Binding("h", "left", "fold/parent", show=False),
        Binding("l", "right", "unfold/child", show=False),
        Binding("G", "bottom", "bottom", show=False),
        Binding("ctrl+d", "half_down", "½ page down", show=False),
        Binding("ctrl+u", "half_up", "½ page up", show=False),
        Binding("n", "next_match", "next match"),
        Binding("N", "prev_match", "prev match"),
        Binding("ctrl+n", "next_failure", "next failure"),
        Binding("ctrl+p", "prev_failure", "prev failure"),
        Binding("r", "rerun", "re-run"),
        Binding("}", "next_group", "next group", show=False),
        Binding("{", "prev_group", "prev group", show=False),
        Binding("slash", "search", "search"),
        Binding("escape", "clear_search", "clear search", show=False),
        Binding("o", "open", "open in nvim"),
        Binding("y", "yank", "yank id"),
        Binding("f", "toggle_filter", "failures only"),
        Binding("x", "toggle_expand", "expand failures"),
        Binding("c", "clear", "clear"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, host="127.0.0.1", port=8765, jsonl=None, local_root=None,
                 maps=(), nvim_socket=None, tmux_target=None, run_cmd=None):
        super().__init__()
        self.host, self.port, self.jsonl = host, port, jsonl
        self.local_root = os.path.expanduser(local_root) if local_root else None
        self.maps = [(r.rstrip("/"), os.path.expanduser(l).rstrip("/")) for r, l in maps]
        self.nvim_socket = nvim_socket or os.environ.get("TESTUI_NVIM_SOCKET", nvim.DEFAULT_SOCKET)
        self.tmux_target = tmux_target or os.environ.get("TESTUI_TMUX_TARGET")
        self.run_cmd = run_cmd if run_cmd is not None else default_run_cmd()
        self._rerun = None      # asyncio subprocess while a re-run is in flight
        self._merge_next = False
        self._rerun_started = False
        self.store = Store()
        self.only_bad = False
        self.query_text = ""
        self.connections = 0
        self.listen_error = ""
        self._test_nodes = {}   # test id -> node
        self._groups = {}       # ("app", a) / ("cls", a, c) -> node
        self._counts = {}       # group key -> {outcome: n}
        self._outcome = {}      # test id -> last outcome
        self._expanded = False
        self._pending = ""      # first key of a g/z chord

    # ---- layout -------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        tree = Tree("tests", id="results")
        tree.show_root = False
        yield tree
        search = Input(placeholder="filter tests (words are ANDed, Enter keeps, Esc clears)", id="search")
        search.display = False
        yield search
        yield Static(id="status")
        yield Footer()

    async def on_mount(self):
        self.query_one(Tree).focus()
        if self.jsonl:
            self.run_worker(tail_jsonl(self.jsonl, self.handle_event), exclusive=False)
        else:
            self.run_worker(self._listen(), exclusive=False)
        self.set_interval(0.5, self._refresh_status)
        self._refresh_status()

    async def _listen(self):
        try:
            server = await serve_tcp(self.host, self.port, self.handle_event, self._on_conn)
        except OSError as exc:
            self.listen_error = "cannot listen on %s:%s (%s)" % (self.host, self.port, exc)
            return
        async with server:
            await server.serve_forever()

    def _on_conn(self, delta):
        self.connections += delta

    # ---- events -> tree -----------------------------------------------
    def handle_event(self, ev):
        try:
            keep = self._merge_next and ev.get("type") == "session_start"
            if keep:
                self._merge_next = False
                self._rerun_started = True
            changed = self.store.apply(ev, keep=keep)
            if changed == "reset":
                self._rebuild()
            elif changed == "result":
                res = self.store.run.results[ev["id"]]
                self._count(res)
                if self._shown(res):
                    self._upsert(res)
                self._relabel_groups(res.id)
            self._refresh_status()
        except Exception as exc:  # a bad event must never kill the UI
            self.log("event error: %r" % (exc,))

    def _shown(self, res):
        if self.only_bad and not res.bad:
            return False
        words = self.query_text.lower().split()
        if words:
            hay = res.id.lower()
            return all(w in hay for w in words)
        return True

    @staticmethod
    def _keys(tid):
        app, cls, _, _ = split_id(tid)
        return ("app", app), ("cls", app, cls)

    def _count(self, res):
        old = self._outcome.get(res.id)
        for key in self._keys(res.id):
            c = self._counts.setdefault(key, {})
            if old:
                c[old] = c.get(old, 1) - 1
            c[res.outcome] = c.get(res.outcome, 0) + 1
        self._outcome[res.id] = res.outcome

    def _rebuild(self):
        tree = self.query_one(Tree)
        tree.clear()
        self._test_nodes, self._groups, self._counts, self._outcome = {}, {}, {}, {}
        for tid in self.store.run.order:
            self._count(self.store.run.results[tid])
        for tid in self.store.run.order:
            res = self.store.run.results[tid]
            if self._shown(res):
                self._upsert(res)
        for key in list(self._groups):
            node = self._groups[key]
            node.set_label(self._group_label(key))
            if self.query_text:  # searching: reveal every match
                node.expand()

    def _group_node(self, key):
        node = self._groups.get(key)
        if node is None:
            tree = self.query_one(Tree)
            parent = tree.root if key[0] == "app" else self._group_node(("app", key[1]))
            node = parent.add(self._group_label(key), data={"kind": "group", "key": key},
                              expand=False)
            self._groups[key] = node
        return node

    def _group_label(self, key):
        c = self._counts.get(key, {})
        bad = sum(c.get(k, 0) for k in BAD)
        total = sum(c.values())
        t = Text()
        t.append("✗ " if bad else "✓ ", style="bold red" if bad else "green")
        if key[0] == "app":
            t.append(key[1], style="bold")
        else:
            mod, _, cls = key[2].rpartition(".")
            if mod:
                t.append(mod + ".", style="dim")
            t.append(cls or key[2], style="bold")
        t.append("  %d" % total, style="dim")
        if bad:
            t.append("  %d failed" % bad, style="red")
        if c.get("skipped"):
            t.append("  %d skipped" % c["skipped"], style="yellow")
        return t

    def _relabel_groups(self, tid):
        for key in self._keys(tid):
            node = self._groups.get(key)
            if node is not None:
                node.set_label(self._group_label(key))

    def _upsert(self, res: TestResult):
        node = self._test_nodes.get(res.id)
        if node is not None:
            node.remove_children()
            node.set_label(self._label(res))
        else:
            app_key, cls_key = self._keys(res.id)
            self._group_node(app_key)
            parent = self._group_node(cls_key)
            node = parent.add(self._label(res), data={"kind": "test", "id": res.id},
                              allow_expand=False)
            self._test_nodes[res.id] = node
            if res.bad:  # reveal failures, once, when they first appear
                for key in (app_key, cls_key):
                    c = self._counts.get(key, {})
                    if sum(c.get(k, 0) for k in BAD) <= 1:
                        self._groups[key].expand()
        if res.frames or res.traceback:
            node.allow_expand = True
            for i, fr in enumerate(res.frames):
                node.add_leaf(self._frame_label(fr, i == res.primary),
                              data={"kind": "frame", "id": res.id, "file": fr["file"],
                                    "line": fr["line"]})
            if res.traceback:
                lines = res.traceback.splitlines()
                tb = node.add("traceback (%d lines)" % len(lines),
                              data={"kind": "tb", "id": res.id}, expand=False)
                for ln in lines[:MAX_TB_LINES]:
                    tb.add_leaf(Text(ln, style="dim"), data={"kind": "tb", "id": res.id})
                if len(lines) > MAX_TB_LINES:
                    tb.add_leaf(Text("... %d more lines" % (len(lines) - MAX_TB_LINES), style="dim"))

    # ---- labels -------------------------------------------------------
    def _label(self, res: TestResult):
        icon, style = STATUS_ICON.get(res.outcome, ("?", ""))
        _, _, name, sub = split_id(res.id)
        t = Text()
        t.append(icon + " ", style=style)
        t.append(name, style="bold" if res.bad else "")
        if sub:
            t.append(" " + sub, style="italic")
        if res.duration is not None and res.duration >= 0.05:
            t.append("  %.2fs" % res.duration, style="cyan")
        if res.message:
            t.append("  " + res.message.splitlines()[0][:160], style="red" if res.bad else "dim")
        return t

    def _rel(self, path):
        root = self.store.run.root
        if root and path.startswith(root.rstrip("/") + "/"):
            return path[len(root.rstrip("/")) + 1:]
        return path

    def _frame_label(self, fr, primary):
        t = Text()
        t.append("★ " if primary else "  ", style="yellow")
        t.append("%s:%d" % (self._rel(fr["file"]), fr["line"]),
                 style="bold blue" if fr.get("project") else "blue")
        t.append(" in %s" % fr["func"], style="dim")
        if fr.get("code"):
            t.append("   " + fr["code"][:120])
        return t

    # ---- status -------------------------------------------------------
    def _refresh_status(self):
        r = self.store.run
        c = r.counts()
        done = len(r.results)
        parts = []
        if self.listen_error:
            parts.append("[red]%s[/]" % self.listen_error)
        elif r.run_id is None:
            where = self.jsonl or "%s:%s" % (self.host, self.port)
            parts.append("waiting for a test run on %s ..." % where)
        else:
            prog = "%d/%s" % (done, r.total) if r.total and not r.merged else str(done)
            parts.append(prog)
            parts.append("[green]%d passed[/]" % c.get("passed", 0))
            bad = sum(c.get(k, 0) for k in BAD)
            parts.append("[red]%d failed[/]" % bad if bad else "0 failed")
            if c.get("skipped"):
                parts.append("[yellow]%d skipped[/]" % c["skipped"])
            if r.aborted:
                parts.append("[bold red]ABORTED: %s[/]" % (r.error or "runner raised"))
            elif r.finished:
                parts.append("[bold]finished[/]")
            elif self.store.stalled():
                parts.append("[bold red]no heartbeat - run died? last test: %s[/]" % (r.current or "?"))
            elif r.current:
                parts.append("[dim]running %s[/]" % r.current.split(" ")[0].split(".", 2)[-1])
        if self.only_bad:
            parts.append("[magenta](failures only)[/]")
        if self._rerun is not None:
            parts.append("[cyan]re-running...[/]")
        if self.query_text:
            parts.append("[magenta]/%s[/]" % self.query_text.replace("[", "\\["))
        if self._pending:
            parts.append("[bold]%s...[/]" % self._pending)
        try:
            self.query_one("#status", Static).update("  ".join(parts))
        except NoMatches:  # timer fired before mount / during shutdown
            pass

    # ---- navigation ---------------------------------------------------
    @property
    def _tree(self):
        return self.query_one(Tree)

    def action_down(self):
        self._tree.action_cursor_down()

    def action_up(self):
        self._tree.action_cursor_up()

    def action_bottom(self):
        t = self._tree
        t.cursor_line = t.last_line

    def action_top(self):
        self._tree.cursor_line = 0

    def _half(self):
        return max(1, self._tree.size.height // 2)

    def action_half_down(self):
        t = self._tree
        t.cursor_line = min(t.last_line, max(t.cursor_line, 0) + self._half())

    def action_half_up(self):
        t = self._tree
        t.cursor_line = max(0, t.cursor_line - self._half())

    def action_left(self):
        t = self._tree
        node = t.cursor_node
        if node is None:
            return
        if node.is_expanded and node.allow_expand:
            node.collapse()
        elif node.parent is not None and node.parent is not t.root:
            self._goto(node.parent)

    def action_right(self):
        node = self._tree.cursor_node
        if node is None or not node.allow_expand:
            return
        if not node.is_expanded:
            node.expand()
        elif node.children:
            self._tree.action_cursor_down()

    def _visible_nodes(self):
        t = self._tree
        return [t.get_node_at_line(i) for i in range(t.last_line + 1)]

    def _goto(self, node):
        """Synchronous move_cursor: line numbers are looked up fresh, so it is
        correct right after expand/collapse (TreeNode._line can be stale)."""
        try:
            self._tree.cursor_line = self._visible_nodes().index(node)
        except ValueError:
            pass

    def _jump_group(self, step):
        t = self._tree
        nodes = self._visible_nodes()
        i = t.cursor_line + step
        while 0 <= i < len(nodes):
            n = nodes[i]
            if n is not None and (n.data or {}).get("kind") == "group":
                t.cursor_line = i
                return
            i += step

    def action_next_group(self):
        self._jump_group(+1)

    def action_prev_group(self):
        self._jump_group(-1)

    def _owner_test_node(self, node):
        while node is not None and (node.data or {}).get("kind") in ("frame", "tb"):
            node = node.parent
        return node

    def _jump_failure(self, step):
        t = self._tree
        flat = list(_dfs(t.root))
        cur = self._owner_test_node(t.cursor_node)
        try:
            start = flat.index(cur)
        except ValueError:
            start = -1 if step > 0 else len(flat)
        i = start + step
        while 0 <= i < len(flat):
            n = flat[i]
            data = n.data or {}
            res = self.store.run.results.get(data.get("id")) if data.get("kind") == "test" else None
            if res is not None and res.bad:
                p = n.parent
                while p is not None and p is not t.root:
                    p.expand()
                    p = p.parent
                self._goto(n)
                return
            i += step
        self.notify("no more failures")

    def action_next_failure(self):
        self._jump_failure(+1)

    def action_prev_failure(self):
        self._jump_failure(-1)

    def _test_nodes_in_order(self):
        return [n for n in _dfs(self._tree.root) if (n.data or {}).get("kind") == "test"]

    def _jump_match(self, step, start=None):
        """Cycle through the tests left by the / filter, wrapping around."""
        if not self.query_text:
            self.notify("no active search (press /)", severity="warning")
            return
        tests = self._test_nodes_in_order()
        if not tests:
            self.notify("no matches")
            return
        cur = self._owner_test_node(self._tree.cursor_node)
        if start is not None:
            idx = start
        else:
            idx = tests.index(cur) if cur in tests else (-1 if step > 0 else len(tests))
        node = tests[(idx + step) % len(tests)]
        p = node.parent
        while p is not None and p is not self._tree.root:
            p.expand()
            p = p.parent
        self._goto(node)

    def action_next_match(self):
        self._jump_match(+1)

    def action_prev_match(self):
        self._jump_match(-1)

    # ---- re-run -------------------------------------------------------
    def _rerun_label(self):
        node = self._owner_test_node(self._tree.cursor_node)
        data = (node.data or {}) if node is not None else {}
        if data.get("kind") == "test":
            return label_for(data["id"])
        if data.get("kind") == "group":
            key = data["key"]
            return key[1] if key[0] == "app" else "%s.%s" % (key[1], key[2])
        return None

    async def action_rerun(self):
        if not self.run_cmd:
            self.notify("no run command: use --run-cmd or $TESTUI_RUN_CMD", severity="error")
            return
        if self._rerun is not None:
            self.notify("a re-run is already in progress", severity="warning")
            return
        label = self._rerun_label()
        if not label:
            self.notify("nothing to re-run here", severity="warning")
            return
        self.notify("re-running " + label)
        self._merge_next, self._rerun_started = True, False
        try:
            self._rerun = await asyncio.create_subprocess_exec(
                *self.run_cmd, label, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            self._merge_next = False
            self.notify("cannot start %s: %s" % (self.run_cmd[0], exc), severity="error")
            return
        self.run_worker(self._await_rerun(self._rerun), exclusive=False)
        self._refresh_status()

    async def _await_rerun(self, proc):
        out, _ = await proc.communicate()
        # events can still be in flight (TCP / file tail) after the process exits
        for _ in range(20):
            if self._rerun_started:
                break
            await asyncio.sleep(0.1)
        self._merge_next = False
        self._rerun = None
        if not self._rerun_started:  # runner never reported: startup/import/docker error
            tail = "\n".join(out.decode(errors="replace").strip().splitlines()[-6:])
            self.notify("re-run failed to start (exit %s)\n%s" % (proc.returncode, tail),
                        severity="error", timeout=15)
        self._refresh_status()

    # ---- g / z chords -------------------------------------------------
    async def on_key(self, event):
        if isinstance(self.focused, Input):
            return
        key = event.key
        if self._pending:
            chord, self._pending = self._pending + key, ""
            event.stop()
            event.prevent_default()
            self._refresh_status()
            self._run_chord(chord)
            return
        if key in ("g", "z"):
            self._pending = key
            event.stop()
            event.prevent_default()
            self._refresh_status()

    def _fold_target(self):
        node = self._tree.cursor_node
        while node is not None and (node.data or {}).get("kind") in ("frame",) \
                and node.parent is not None:
            node = node.parent
        return node

    def _run_chord(self, chord):
        t = self._tree
        node = self._fold_target()
        if chord == "gg":
            self.action_top()
        elif chord == "zo" and node is not None:
            node.expand()
        elif chord == "zc" and node is not None:
            node.collapse()
        elif chord == "za" and node is not None:
            node.toggle()
        elif chord == "zR":
            for n in _dfs(t.root):
                if (n.data or {}).get("kind") in ("group", "test") and n.allow_expand:
                    n.expand()
        elif chord == "zM":
            for n in _dfs(t.root):
                n.collapse()
            t.cursor_line = 0

    # ---- search -------------------------------------------------------
    def action_search(self):
        box = self.query_one("#search", Input)
        box.display = True
        box.focus()

    def on_input_changed(self, event):
        if event.input.id == "search":
            self.query_text = event.value.strip()
            self._rebuild()
            self._refresh_status()

    def on_input_submitted(self, event):
        if event.input.id == "search":
            event.input.display = False
            self._tree.focus()
            if self.query_text:
                self._jump_match(+1, start=-1)

    def action_clear_search(self):
        box = self.query_one("#search", Input)
        had = bool(self.query_text) or box.display
        box.value = ""
        box.display = False
        self._tree.focus()
        if had:
            self.query_text = ""
            self._rebuild()
            self._refresh_status()

    # ---- actions ------------------------------------------------------
    def action_toggle_filter(self):
        self.only_bad = not self.only_bad
        self._rebuild()
        self._refresh_status()

    def action_clear(self):
        self.store.run.results.clear()
        self.store.run.order.clear()
        self._rebuild()
        self._refresh_status()

    def action_toggle_expand(self):
        self._expanded = not self._expanded
        for tid, node in self._test_nodes.items():
            res = self.store.run.results.get(tid)
            if res and res.bad:
                if self._expanded:
                    p = node.parent
                    while p is not None and p is not self._tree.root:
                        p.expand()
                        p = p.parent
                    node.expand()
                else:
                    node.collapse()

    def action_yank(self):
        node = self._owner_test_node(self._tree.cursor_node)
        data = (node.data or {}) if node is not None else {}
        if data.get("kind") == "test":
            tid = data["id"].split(" ")[0]
        elif data.get("kind") == "group":
            key = data["key"]
            tid = ".".join(key[1:]) if key[0] == "app" else key[1] + "." + key[2]
        else:
            return
        self.copy_to_clipboard(tid)
        self.notify("yanked " + tid)

    def map_path(self, path):
        for remote, local in self.maps:
            if path == remote or path.startswith(remote + "/"):
                return local + path[len(remote):]
        root = self.store.run.root
        if self.local_root and root and path.startswith(root.rstrip("/") + "/"):
            return os.path.join(self.local_root, path[len(root.rstrip("/")) + 1:])
        return path

    def _location(self, node):
        data = node.data or {}
        if data.get("kind") == "frame":
            return data["file"], data["line"]
        res = self.store.run.results.get(data.get("id"))
        if res and res.primary is not None and res.frames:
            fr = res.frames[res.primary]
            return fr["file"], fr["line"]
        return None

    async def action_open(self):
        node = self._tree.cursor_node
        loc = self._location(node) if node is not None else None
        if not loc:
            self.notify("nothing to open here", severity="warning")
            return
        path, line = self.map_path(loc[0]), loc[1]
        if not os.path.exists(path):
            self.notify("%s not found on this machine - set --local-root or --map" % path,
                        severity="error", timeout=8)
            return
        ok, msg = await nvim.open_in_running_nvim(path, line, self.nvim_socket, self.tmux_target)
        if ok:
            self.notify(msg)
            return
        self.notify(msg + " - launching new nvim", severity="warning")
        with self.suspend():
            subprocess.call(["nvim", "+%d" % line, path])
