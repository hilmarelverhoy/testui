import json
import os
import socket
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).parent / "djproj"


def run_django(tmp_path, *labels, env_extra=None):
    out = tmp_path / "events.jsonl"
    # a port nothing listens on: TCP path must fail silently
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="settings", PYTHONPATH=str(PROJ) + os.pathsep + os.pathsep.join(sys.path),
               TESTUI_JSONL=str(out), TESTUI_HOST="127.0.0.1", TESTUI_PORT=str(port))
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, "-m", "django", "test", "--testrunner=testui.runner.TestuiRunner", *labels],
                          cwd=PROJ, env=env, capture_output=True, text=True, timeout=120)
    events = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
    return proc, events


def results(events):
    return {e["id"]: e for e in events if e["type"] == "test_result"}


def test_outcomes_and_locations(tmp_path):
    proc, events = run_django(tmp_path, "sample")
    assert proc.returncode == 1  # real failures still fail the run
    types = [e["type"] for e in events]
    assert types[0] == "session_start" and types[-1] == "session_finish"
    assert any(e["type"] == "collected" for e in events)
    r = results(events)
    p = "sample.tests."
    assert r[p + "Basic.test_pass"]["outcome"] == "passed"
    assert r[p + "Basic.test_skip"]["outcome"] == "skipped"
    assert r[p + "Basic.test_error"]["outcome"] == "error"
    fail = r[p + "Basic.test_fail"]
    fr = fail["frames"][fail["primary"]]
    assert fr["file"].endswith("sample/tests.py") and fr["code"] == "self.assertEqual(a, 2)"
    helper = r[p + "Basic.test_assert_in_helper"]
    assert helper["frames"][helper["primary"]]["func"] == "helper"  # deepest project frame, not the test
    assert any("subTest" in k or "i=" in k for k in r)  # subtest failures reported


def test_hostile_tests_do_not_break_reporting(tmp_path):
    proc, events = run_django(tmp_path, "sample.tests.Hostile")
    r = results(events)
    assert r["sample.tests.Hostile.test_prints_junk"]["outcome"] == "passed"
    assert r["sample.tests.Hostile.test_mocks_everything"]["outcome"] == "passed"
    # chdir('/') must not break path classification
    chdir = r["sample.tests.Hostile.test_chdir"]
    assert chdir["frames"][chdir["primary"]]["project"] is True
    huge = r["sample.tests.Hostile.test_huge_message"]
    assert len(json.dumps(huge)) < 200_000  # truncated
    assert events[-1]["type"] == "session_finish"


def test_no_tui_running_is_harmless(tmp_path):
    proc, events = run_django(tmp_path, "sample.tests.Basic.test_pass", env_extra={"TESTUI_JSONL": ""})
    assert proc.returncode == 0 and "OK" in proc.stderr


def test_hard_exit_leaves_unfinished_stream(tmp_path):
    proc, events = run_django(tmp_path, "crash_tests")
    assert proc.returncode == 3
    types = [e["type"] for e in events]
    assert "session_finish" not in types
    assert [e for e in events if e["type"] == "test_start"][-1]["id"].endswith("test_hard_exit")


def test_parallel(tmp_path):
    proc, events = run_django(tmp_path, "sample.tests.Basic", "--parallel", "2")
    r = results(events)
    assert r["sample.tests.Basic.test_pass"]["outcome"] == "passed", proc.stderr[-2000:]
    assert r["sample.tests.Basic.test_fail"]["outcome"] == "failed"
    assert events[-1]["type"] == "session_finish"
