import asyncio
import os
import subprocess
import time

import pytest
from textual.widgets import Tree

from testui.tui import nvim
from testui.tui.app import TestuiApp
from testui.tui.model import Store

RUN = {"run": "r1", "v": 1}


def ev(kind, **kw):
    return {"type": kind, **RUN, **kw}


def failing(tmp_file):
    return ev("test_result", id="app.tests.Foo.test_bar", outcome="failed", duration=0.2,
              message="AssertionError: 1 != 2", traceback="Traceback...\nAssertionError: 1 != 2",
              frames=[{"file": "/usr/src/app/lib.py", "line": 3, "func": "f", "code": "x", "project": False},
                      {"file": "/usr/src/app/%s" % tmp_file.name, "line": 7, "func": "test_bar",
                       "code": "assert 1 == 2", "project": True}], primary=1)


def test_store_ignores_garbage_and_other_runs():
    s = Store()
    assert s.apply("nonsense") is None and s.apply({"type": "???"}) is None
    s.apply(ev("session_start", root="/r"))
    assert s.apply({"type": "test_result", "id": "a", "outcome": "passed", "run": "other"}) is None
    assert s.apply(ev("test_result", id=5)) is None
    assert s.apply(ev("test_result", id="a", outcome="passed")) == "result"


async def test_two_level_folding_filter_and_path_mapping(tmp_path):
    src = tmp_path / "mod.py"; src.write_text("\n" * 10)
    app = TestuiApp(port=0, local_root=str(tmp_path), jsonl=str(tmp_path / "none.jsonl"))
    async with app.run_test() as pilot:
        app.handle_event(ev("session_start", root="/usr/src/app"))
        app.handle_event(ev("collected", total=2))
        app.handle_event(ev("test_result", id="app.tests.Foo.test_ok", outcome="passed", duration=0.01))
        app.handle_event(failing(src))
        tree = app.query_one(Tree)
        # app > class > test
        (app_node,) = tree.root.children
        (cls_node,) = app_node.children
        assert (app_node.data["key"], cls_node.data["key"]) == (("app", "app"), ("cls", "app", "tests.Foo"))
        assert [n.data["id"] for n in cls_node.children] == ["app.tests.Foo.test_ok", "app.tests.Foo.test_bar"]
        assert "1 failed" in app_node.label.plain and "1 failed" in cls_node.label.plain
        assert app_node.is_expanded and cls_node.is_expanded  # a failure reveals its groups
        bad = app._test_nodes["app.tests.Foo.test_bar"]
        assert bad.allow_expand and len(bad.children) == 3  # 2 frames + traceback
        assert not app._test_nodes["app.tests.Foo.test_ok"].allow_expand
        await pilot.press("f")
        assert [n.data["id"] for n in app._groups[("cls", "app", "tests.Foo")].children] == ["app.tests.Foo.test_bar"]
        assert "2" in app._groups[("app", "app")].label.plain  # counts still cover filtered-out tests
        assert app.map_path("/usr/src/app/mod.py") == str(tmp_path / "mod.py")
        await pilot.pause()
        tree.move_cursor(app._test_nodes["app.tests.Foo.test_bar"])
        assert app._location(tree.cursor_node) == ("/usr/src/app/mod.py", 7)  # assert frame, not first


async def test_passing_groups_start_collapsed(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test():
        app.handle_event(ev("session_start", root="/r"))
        app.handle_event(ev("test_result", id="a.tests.C.t1", outcome="passed"))
        assert not app._groups[("app", "a")].is_expanded
        assert not app._groups[("cls", "a", "tests.C")].is_expanded


def test_split_id():
    from testui.tui.model import split_id
    assert split_id("accounts.tests.test_x.Foo.test_bar") == ("accounts", "tests.test_x.Foo", "test_bar", "")
    assert split_id("a.tests.Foo.test_bar (i=1)") == ("a", "tests.Foo", "test_bar", "(i=1)")
    assert split_id("setUpClass (a.tests.Foo)") == ("a", "tests.Foo", "setUpClass", "")
    assert split_id("weird")[2] == "weird"


async def test_open_uses_socket(tmp_path, monkeypatch):
    src = tmp_path / "mod.py"; src.write_text("\n" * 10)
    calls = []

    async def fake(path, line, sock, tmux=None):
        calls.append((path, line, sock)); return True, "ok"
    monkeypatch.setattr(nvim, "open_in_running_nvim", fake)
    app = TestuiApp(local_root=str(tmp_path), jsonl=str(tmp_path / "n"), nvim_socket="/tmp/x.sock")
    async with app.run_test() as pilot:
        app.handle_event(ev("session_start", root="/usr/src/app"))
        app.handle_event(failing(src))
        await pilot.pause()
        app.query_one(Tree).move_cursor(app._test_nodes["app.tests.Foo.test_bar"])
        await pilot.press("o")
        assert calls == [(str(src), 7, "/tmp/x.sock")]


async def test_stall_and_abort_are_visible(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test():
        app.handle_event(ev("session_start", root="/r"))
        app.handle_event(ev("test_start", id="a.B.c"))
        app.store.run.last_seen -= 60
        assert app.store.stalled()
        app._refresh_status()
        assert "no heartbeat" in str(app.query_one("#status").render())


def test_edit_keys_escape():
    assert nvim.edit_keys("/a b/c#d.py", 12) == "<C-\\><C-n>:edit +12 /a\\ b/c\\#d.py<CR>zz"
    assert "<lt>" in nvim.edit_keys("/a<b", 1)


@pytest.mark.skipif(not subprocess.run(["which", "nvim"], capture_output=True).stdout, reason="nvim missing")
async def test_real_nvim_jump(tmp_path):
    sock = "/tmp/testui-test-%d.sock" % os.getpid()
    f = tmp_path / "with space#.py"; f.write_text("".join("line %d\n" % i for i in range(1, 40)))
    proc = subprocess.Popen(["nvim", "--headless", "--clean", "--listen", sock])
    try:
        for _ in range(50):
            if nvim.is_socket(sock): break
            time.sleep(0.1)
        ok, msg = await nvim.open_in_running_nvim(str(f), 25, sock)
        assert ok, msg
        out = subprocess.run(["nvim", "--server", sock, "--remote-expr", "expand('%:p').':'.line('.')"],
                             capture_output=True, text=True).stdout.strip()
        assert out == "%s:25" % os.path.realpath(f) or out.endswith("with space#.py:25")
    finally:
        proc.terminate()
    ok, msg = await nvim.open_in_running_nvim(str(f), 1, "/tmp/definitely-missing.sock")
    assert not ok and "no nvim socket" in msg


def populate(app, n_classes=3, per=4, bad=("m0.tests.C1.t2", "m1.tests.C0.t1")):
    app.handle_event(ev("session_start", root="/r"))
    for m in range(2):
        for c in range(n_classes):
            for t in range(per):
                tid = "m%d.tests.C%d.t%d" % (m, c, t)
                app.handle_event(ev("test_result", id=tid, outcome="failed" if tid in bad else "passed",
                                    message="boom" if tid in bad else ""))


def cursor_id(app):
    d = app.query_one(Tree).cursor_node.data
    return d.get("id") or d["key"]


async def test_j_k_h_l_and_folds(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test() as pilot:
        populate(app)
        tree = app.query_one(Tree)
        await pilot.pause()
        await pilot.press("z", "M")  # collapse everything
        assert cursor_id(app) == ("app", "m0")  # zM leaves the cursor on the first row
        await pilot.press("j")
        assert cursor_id(app) == ("app", "m1")
        await pilot.press("k")
        await pilot.press("l")  # expand
        await pilot.press("l")  # step into first child
        assert cursor_id(app) == ("cls", "m0", "tests.C0")
        await pilot.press("h")  # collapsed already -> go to parent
        assert cursor_id(app) == ("app", "m0")
        await pilot.press("h")  # collapse
        assert not app._groups[("app", "m0")].is_expanded
        await pilot.press("z", "o"); assert app._groups[("app", "m0")].is_expanded
        await pilot.press("z", "c"); assert not app._groups[("app", "m0")].is_expanded
        await pilot.press("z", "a"); assert app._groups[("app", "m0")].is_expanded
        await pilot.press("z", "R")
        assert app._groups[("cls", "m1", "tests.C2")].is_expanded
        await pilot.press("z", "M")
        assert not app._groups[("app", "m0")].is_expanded


async def test_gg_G_half_pages_and_group_jumps(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test(size=(100, 20)) as pilot:
        populate(app)
        tree = app.query_one(Tree)
        await pilot.pause()
        await pilot.press("z", "R")
        await pilot.press("G")
        assert tree.cursor_line == tree.last_line
        await pilot.press("g", "g")
        assert tree.cursor_line == 0
        await pilot.press("ctrl+d")
        assert 0 < tree.cursor_line <= tree.size.height
        down = tree.cursor_line
        await pilot.press("ctrl+u")
        assert tree.cursor_line < down
        await pilot.press("}")
        assert tree.cursor_node.data["kind"] == "group"
        first = tree.cursor_line
        await pilot.press("}")
        assert tree.cursor_line > first
        await pilot.press("{")
        assert tree.cursor_line == first


async def test_next_prev_failure_expands_collapsed_groups(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test() as pilot:
        populate(app)
        await pilot.pause()
        await pilot.press("z", "M")
        await pilot.press("ctrl+n"); await pilot.pause()
        assert cursor_id(app) == "m0.tests.C1.t2"
        await pilot.press("ctrl+n"); await pilot.pause()
        assert cursor_id(app) == "m1.tests.C0.t1"
        await pilot.press("ctrl+p"); await pilot.pause()
        assert cursor_id(app) == "m0.tests.C1.t2"


async def test_slash_search_filters_and_escape_restores(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test() as pilot:
        populate(app)
        await pilot.pause()
        await pilot.press("slash")
        assert app.query_one("#search").display and app.focused.id == "search"
        await pilot.press(*"c1")          # typing letters must not trigger j/k/g/z/q bindings
        await pilot.press("space", "t", "2")
        await pilot.pause()
        assert set(app._test_nodes) == {"m0.tests.C1.t2", "m1.tests.C1.t2"}
        assert app._groups[("cls", "m0", "tests.C1")].is_expanded  # matches are revealed
        assert ("cls", "m0", "tests.C0") not in app._groups
        await pilot.press("enter")
        assert not app.query_one("#search").display and app.query_text == "c1 t2"
        assert cursor_id(app) == "m0.tests.C1.t2"  # Enter lands on the first match
        await pilot.press("n"); assert cursor_id(app) == "m1.tests.C1.t2"
        await pilot.press("n"); assert cursor_id(app) == "m0.tests.C1.t2"  # wraps
        await pilot.press("N"); assert cursor_id(app) == "m1.tests.C1.t2"
        await pilot.press("ctrl+p"); await pilot.pause()  # failure nav works on the filtered tree
        assert cursor_id(app) == "m0.tests.C1.t2"
        await pilot.press("escape")
        assert app.query_text == "" and len(app._test_nodes) == 24


async def test_search_applies_to_incoming_results(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test() as pilot:
        app.handle_event(ev("session_start", root="/r"))
        await pilot.press("slash"); await pilot.press(*"foo")
        app.handle_event(ev("test_result", id="a.tests.Foo.t1", outcome="passed"))
        app.handle_event(ev("test_result", id="a.tests.Bar.t1", outcome="passed"))
        assert set(app._test_nodes) == {"a.tests.Foo.t1"}


async def test_n_without_search_does_not_crash(tmp_path):
    app = TestuiApp(jsonl=str(tmp_path / "n"))
    async with app.run_test() as pilot:
        populate(app)
        await pilot.press("n", "N")
        assert app.query_text == ""


def test_label_for():
    from testui.tui.model import label_for
    assert label_for("a.tests.Foo.t (i=1)") == "a.tests.Foo.t"
    assert label_for("setUpClass (a.tests.Foo)") == "a.tests.Foo"


FAKE = """#!/bin/sh
# fake bin/dt: emits a session for the given label over JSONL
echo "$1" >> "$OUT.labels"
printf '{"type":"session_start","run":"rr","root":"/r","labels":["%s"]}\n' "$1" >> "$OUT"
printf '{"type":"test_result","run":"rr","id":"a.tests.Foo.t1","outcome":"passed"}\n' >> "$OUT"
printf '{"type":"session_finish","run":"rr","failures":0}\n' >> "$OUT"
exit 1
"""


async def test_r_reruns_label_and_merges_results(tmp_path):
    out = tmp_path / "events.jsonl"; out.write_text("")
    fake = tmp_path / "fake_dt"; fake.write_text(FAKE); fake.chmod(0o755)
    os.environ["OUT"] = str(out)
    app = TestuiApp(jsonl=str(out), run_cmd=[str(fake)])
    async with app.run_test() as pilot:
        app.handle_event(ev("session_start", root="/r"))
        app.handle_event(ev("test_result", id="a.tests.Foo.t1", outcome="failed", message="x"))
        app.handle_event(ev("test_result", id="a.tests.Bar.t9", outcome="passed"))
        await pilot.pause()
        app.query_one(Tree).move_cursor(app._test_nodes["a.tests.Foo.t1"])
        await pilot.press("r")
        for _ in range(50):
            await pilot.pause(0.1)
            if app.store.run.finished and app._rerun is None and app._rerun_started:
                break
        assert (tmp_path / "events.jsonl.labels").read_text().strip() == "a.tests.Foo.t1"
        assert app.store.run.results["a.tests.Foo.t1"].outcome == "passed"  # updated in place
        assert "a.tests.Bar.t9" in app.store.run.results                    # others kept
        assert app._test_nodes["a.tests.Foo.t1"].label.plain.startswith("✓")


async def test_r_reports_startup_failure(tmp_path):
    bad = tmp_path / "bad"; bad.write_text("#!/bin/sh\necho docker exploded\nexit 125\n"); bad.chmod(0o755)
    app = TestuiApp(jsonl=str(tmp_path / "n"), run_cmd=[str(bad)])
    async with app.run_test() as pilot:
        app.handle_event(ev("session_start", root="/r"))
        app.handle_event(ev("test_result", id="a.tests.Foo.t1", outcome="failed"))
        await pilot.pause()
        app.query_one(Tree).move_cursor(app._test_nodes["a.tests.Foo.t1"])
        await pilot.press("r")
        for _ in range(50):
            await pilot.pause(0.1)
            if app._rerun is None:
                break
        assert app._rerun is None and not app._merge_next and "a.tests.Foo.t1" in app.store.run.results
