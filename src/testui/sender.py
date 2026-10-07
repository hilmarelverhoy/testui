"""Fail-silent, non-blocking event sender. Stdlib only.

Guarantees: never raises into the caller, never blocks the test process
(bounded queue, background thread, short socket timeouts), and keeps
references to the functions it needs captured at import time so tests that
mock `socket`/`time`/`json`/`open` cannot break it.
"""
from __future__ import annotations

import atexit
import json as _json
import os as _os
import queue as _queue
import socket as _socket
import threading as _threading
import time as _time

_dumps = _json.dumps
_getpid = _os.getpid
_Queue = _queue.Queue
_Empty = _queue.Empty
_Full = _queue.Full
_Thread = _threading.Thread
_Event = _threading.Event
_socket_cls = _socket.socket
_getaddrinfo = _socket.getaddrinfo
_SOCK_STREAM = _socket.SOCK_STREAM
_monotonic = _time.monotonic
_open = open

HEARTBEAT = 2.0
RETRY_AFTER = 1.0
_STOP = object()


class Sender:
    def __init__(self, host=None, port=None, jsonl=None, queue_size=10000):
        self._pid = _getpid()
        self._host, self._port, self._jsonl = host, port, jsonl
        self._q = _Queue(maxsize=queue_size)
        self._sock = None
        self._file = None
        self._next_try = 0.0
        self._thread = _Thread(target=self._run, name="testui-sender", daemon=True)
        self._closed = False
        try:
            self._thread.start()
        except Exception:
            self._closed = True

    # ---- public -------------------------------------------------------
    def send(self, event, flush=False):
        """Queue an event. With flush=True, wait (<=0.25s) until it was written,
        so the last test_start survives an os._exit()/segfault in the next test."""
        try:
            if self._closed or _getpid() != self._pid:
                return  # forked child: never write on the parent's socket
            if flush:
                done = _Event()
                self._q.put_nowait((event, done))
                done.wait(0.25)
            else:
                self._q.put_nowait(event)
        except Exception:  # queue full or anything else: drop
            pass

    def close(self, timeout=2.0):
        try:
            if self._closed or _getpid() != self._pid:
                return
            self._closed = True
            try:
                self._q.put(_STOP, timeout=0.2)
            except Exception:
                pass
            self._thread.join(timeout)
        except Exception:
            pass

    # ---- worker thread ------------------------------------------------
    def _run(self):
        while True:
            try:
                item = self._q.get(timeout=HEARTBEAT)
            except _Empty:
                item = {"type": "heartbeat"}
            except Exception:
                return
            if item is _STOP:
                self._teardown()
                return
            done = None
            if isinstance(item, tuple):
                item, done = item
            try:
                line = (_dumps(item, default=str) + "\n").encode("utf-8", "replace")
                self._write(line)
            except Exception:
                pass
            if done is not None:
                done.set()

    def _write(self, line):
        if self._jsonl:
            try:
                if self._file is None:
                    self._file = _open(self._jsonl, "ab", buffering=0)
                self._file.write(line)
            except Exception:
                self._file = None
        if self._host and self._port:
            now = _monotonic()
            if self._sock is None and now >= self._next_try:
                try:
                    self._sock = self._connect()
                except Exception:
                    self._sock = None
                    self._next_try = now + RETRY_AFTER
            if self._sock is not None:
                try:
                    self._sock.sendall(line)
                except Exception:
                    self._drop_socket()

    def _connect(self):
        # not socket.create_connection: it looks up `socket.socket` at call
        # time, which a test may have patched.
        last = None
        for af, st, proto, _, addr in _getaddrinfo(self._host, self._port, 0, _SOCK_STREAM):
            sock = None
            try:
                sock = _socket_cls(af, st, proto)
                sock.settimeout(1.0)
                sock.connect(addr)
                sock.settimeout(2.0)
                return sock
            except Exception as exc:
                last = exc
                try:
                    sock.close()
                except Exception:
                    pass
        raise last or OSError("no address")

    def _drop_socket(self):
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._next_try = _monotonic() + RETRY_AFTER

    def _teardown(self):
        if self._sock is not None:
            self._drop_socket()
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass


_sender = None


def get_sender():
    """Process-wide sender configured from TESTUI_* env vars (None if unset)."""
    global _sender
    if _sender is not None:
        return _sender
    try:
        host = _os.environ.get("TESTUI_HOST", "host.docker.internal")
        port = int(_os.environ.get("TESTUI_PORT", "8765"))
        jsonl = _os.environ.get("TESTUI_JSONL") or None
        if _os.environ.get("TESTUI_DISABLE"):
            return None
        _sender = Sender(host, port, jsonl)
        atexit.register(_sender.close)
    except Exception:
        _sender = None
    return _sender
