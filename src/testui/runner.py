"""Django test runner that streams structured events to the testui TUI.

    python manage.py test --testrunner=testui.runner.TestuiRunner

Everything that touches reporting is wrapped so it can never change the
outcome of the test run.
"""
from __future__ import annotations

import inspect
import os
import time
import unittest
import uuid

from django.test.runner import DiscoverRunner

from . import protocol
from .sender import get_sender

_monotonic = time.monotonic
_time = time.time
_getcwd = os.getcwd
_getpid = os.getpid

ROOT = _getcwd()  # captured now so tests that chdir can't break path logic
RUN_ID = uuid.uuid4().hex[:8]


def _emit(event, flush=False):
    try:
        sender = get_sender()
        if sender is not None:
            event["v"] = protocol.SCHEMA
            event["run"] = RUN_ID
            event["ts"] = _time()
            sender.send(event, flush=flush)
    except Exception:
        pass


def _test_location(test):
    """Fallback file/line (the test definition) when there is no traceback."""
    try:
        fn = getattr(type(test), test._testMethodName)
        return inspect.getsourcefile(fn), inspect.getsourcelines(fn)[1]
    except Exception:
        return None, None


def _make_result_class(base):
    class TestuiResult(base):
        _testui_starts = {}

        def _testui_report(self, test, outcome, err=None, reason=None, subtest=None):
            try:
                test_id = test.id()
                if subtest is not None:
                    test_id = "%s %s" % (test_id, subtest.id().split(" ", 1)[-1])
                started = self._testui_starts.get(test.id())
                duration = round(_monotonic() - started, 4) if started is not None else None
                event = {
                    "type": "test_result",
                    "id": test_id,
                    "outcome": outcome,
                    "duration": duration,
                }
                if err is not None:
                    try:
                        text = self._exc_info_to_string(err, test)
                    except Exception:
                        text = "%s: %s" % (getattr(err[0], "__name__", err[0]), err[1])
                    frames = protocol.parse_frames(text, ROOT)
                    idx = protocol.primary_frame_index(frames)
                    if idx is None:
                        file, line = _test_location(test)
                        if file:
                            frames = [{"file": file, "line": line, "func": test_id,
                                       "code": "", "project": True}]
                            idx = 0
                    event.update(
                        message=protocol.short_message(text),
                        traceback=protocol.truncate(text),
                        frames=frames[:60],
                        primary=idx,
                    )
                elif reason:
                    event["message"] = protocol.truncate(str(reason), 300)
                _emit(event)
            except Exception:
                pass

        def startTest(self, test):
            super().startTest(test)
            try:
                self._testui_starts[test.id()] = _monotonic()
                _emit({"type": "test_start", "id": test.id()}, flush=True)
            except Exception:
                pass

        def addSuccess(self, test):
            super().addSuccess(test)
            self._testui_report(test, "passed")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._testui_report(test, "failed", err)

        def addError(self, test, err):
            super().addError(test, err)
            self._testui_report(test, "error", err)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._testui_report(test, "skipped", reason=reason)

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self._testui_report(test, "xfail")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._testui_report(test, "xpass")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self._testui_report(test, "failed", err, subtest=subtest)

    return TestuiResult


class TestuiRunner(DiscoverRunner):
    def get_test_runner_kwargs(self):
        kwargs = super().get_test_runner_kwargs()
        base = kwargs.get("resultclass") or unittest.TextTestResult
        kwargs["resultclass"] = _make_result_class(base)
        return kwargs

    def build_suite(self, *args, **kwargs):
        suite = super().build_suite(*args, **kwargs)
        try:
            _emit({"type": "collected", "total": suite.countTestCases()})
        except Exception:
            pass
        return suite

    def run_tests(self, test_labels, **kwargs):
        _emit({"type": "session_start", "root": ROOT, "pid": _getpid(),
               "labels": list(test_labels or [])})
        try:
            failures = super().run_tests(test_labels, **kwargs)
        except BaseException as exc:
            _emit({"type": "session_finish", "aborted": True,
                   "error": protocol.truncate("%s: %s" % (type(exc).__name__, exc), 500)})
            self._close()
            raise
        _emit({"type": "session_finish", "failures": failures})
        self._close()
        return failures

    @staticmethod
    def _close():
        try:
            sender = get_sender()
            if sender is not None:
                sender.close()
        except Exception:
            pass
