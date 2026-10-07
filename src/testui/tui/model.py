"""Pure state for a test run (no Textual imports; easy to unit test)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

STATUS_ICON = {
    "passed": ("✓", "green"),
    "failed": ("✗", "bold red"),
    "error": ("!", "bold red"),
    "skipped": ("s", "yellow"),
    "xfail": ("x", "yellow"),
    "xpass": ("X", "magenta"),
}
BAD = {"failed", "error", "xpass"}


@dataclass
class TestResult:
    id: str
    outcome: str
    duration: float | None = None
    message: str = ""
    traceback: str = ""
    frames: list = field(default_factory=list)
    primary: int | None = None

    @property
    def bad(self):
        return self.outcome in BAD


@dataclass
class Run:
    run_id: str | None = None
    root: str | None = None
    labels: list = field(default_factory=list)
    total: int | None = None
    results: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    current: str | None = None
    finished: bool = False
    merged: bool = False
    aborted: bool = False
    error: str = ""
    last_seen: float = field(default_factory=time.monotonic)
    started_at: float = field(default_factory=time.monotonic)

    def counts(self):
        out = {}
        for r in self.results.values():
            out[r.outcome] = out.get(r.outcome, 0) + 1
        return out


class Store:
    """Applies protocol events. `apply` returns what changed for the UI."""

    def __init__(self):
        self.run = Run()

    def apply(self, ev, keep=False):
        """Returns one of: 'reset', 'result', 'status', None.

        keep=True makes a session_start merge into the current results (used
        for re-runs of a subset) instead of starting from scratch.
        """
        if not isinstance(ev, dict):
            return None
        kind = ev.get("type")
        run_id = ev.get("run")
        if kind == "session_start":
            old = self.run
            self.run = Run(run_id=run_id, root=ev.get("root"), labels=ev.get("labels") or [])
            if keep:
                self.run.results, self.run.order = old.results, old.order
                self.run.merged = True
                return "status"
            return "reset"
        if run_id and self.run.run_id and run_id != self.run.run_id:
            return None  # stale/other run
        if self.run.run_id is None:
            self.run.run_id = run_id
        self.run.last_seen = time.monotonic()
        if kind == "collected":
            self.run.total = ev.get("total")
        elif kind == "test_start":
            self.run.current = ev.get("id")
        elif kind == "test_result":
            tid = ev.get("id")
            if not isinstance(tid, str):
                return None
            res = TestResult(
                id=tid,
                outcome=ev.get("outcome", "error"),
                duration=ev.get("duration"),
                message=ev.get("message") or "",
                traceback=ev.get("traceback") or "",
                frames=ev.get("frames") or [],
                primary=ev.get("primary"),
            )
            if tid not in self.run.results:
                self.run.order.append(tid)
            self.run.results[tid] = res
            return "result"
        elif kind == "session_finish":
            self.run.finished = True
            self.run.aborted = bool(ev.get("aborted"))
            self.run.error = ev.get("error") or ""
            self.run.current = None
        elif kind != "heartbeat":
            return None
        return "status"

    def stalled(self, now=None, after=10.0):
        r = self.run
        return (
            r.run_id is not None
            and not r.finished
            and ((now or time.monotonic()) - r.last_seen) > after
        )


def split_id(tid):
    """'app.tests.mod.Class.test (sub)' -> (app, 'tests.mod.Class', 'test', 'sub').

    Also handles unittest's 'setUpClass (app.tests.Class)' style ids.
    """
    head, _, rest = tid.partition(" ")
    if "." in head:
        sub = rest
        parts = head.split(".")
    else:  # e.g. "setUpClass (app.tests.Class)"
        sub = ""
        dotted = rest.strip("()")
        parts = dotted.split(".") + [head] if dotted else [head]
    if len(parts) >= 3:
        return parts[0], ".".join(parts[1:-1]), parts[-1], sub
    if len(parts) == 2:
        return parts[0], "-", parts[1], sub
    return "?", "-", parts[0], sub


def label_for(tid):
    """Test id -> a label `manage.py test` accepts (drops subtest suffix)."""
    head, _, rest = tid.partition(" ")
    if "." in head:
        return head
    dotted = rest.strip("()")  # "setUpClass (a.tests.Foo)" -> class label
    return dotted or head
