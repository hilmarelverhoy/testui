import json
import socket
import threading
import time
from unittest import mock

from testui.sender import Sender


def listener():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    got = []
    accepted = threading.Event()

    def serve():
        conn, _ = srv.accept()
        accepted.set()
        buf = b""
        while True:
            d = conn.recv(65536)
            if not d:
                break
            buf += d
        got.extend(json.loads(l) for l in buf.splitlines())

    t = threading.Thread(target=serve, daemon=True); t.start()
    t.accepted = accepted
    return srv.getsockname()[1], got, t


def test_delivers_over_tcp():
    port, got, t = listener()
    s = Sender("127.0.0.1", port)
    s.send({"type": "a"}); s.send({"type": "b"}, flush=True)
    s.close(); t.join(3)
    assert [e["type"] for e in got] == ["a", "b"]


def test_dead_server_never_blocks_or_raises():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    snd = Sender("127.0.0.1", port, queue_size=10)
    t0 = time.monotonic()
    for i in range(1000):  # overflows the bounded queue: must drop silently
        snd.send({"i": i})
    assert time.monotonic() - t0 < 1
    snd.close(1)


def test_survives_mocked_modules():
    port, got, t = listener()
    snd = Sender("127.0.0.1", port)
    snd.send({"type": "first"}, flush=True)
    assert t.accepted.wait(3)  # the test's own server must accept before sockets get patched
    with mock.patch("socket.socket", side_effect=RuntimeError), mock.patch("json.dumps", side_effect=RuntimeError), \
            mock.patch("time.monotonic", side_effect=RuntimeError), mock.patch("builtins.open", side_effect=RuntimeError), \
            mock.patch("socket.create_connection", side_effect=RuntimeError), mock.patch("socket.getaddrinfo", side_effect=RuntimeError):
        snd.send({"type": "second"}, flush=True)
    snd.close(); t.join(3)
    assert [e["type"] for e in got] == ["first", "second"]


def test_connect_works_while_socket_is_patched():
    port, got, t = listener()
    snd = Sender("127.0.0.1", port)
    with mock.patch("socket.create_connection", side_effect=RuntimeError), mock.patch("socket.getaddrinfo", side_effect=RuntimeError):
        snd.send({"type": "x"}, flush=True)
    snd.close(); t.join(3)
    assert got and got[0]["type"] == "x"


def test_unserialisable_event_is_dropped_not_fatal():
    port, got, t = listener()
    snd = Sender("127.0.0.1", port)
    snd.send({"bad": object()}); snd.send({"type": "ok"}, flush=True)
    snd.close(); t.join(3)
    assert got[-1]["type"] == "ok"
