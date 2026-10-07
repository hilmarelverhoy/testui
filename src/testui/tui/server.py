"""Event sources: a TCP listener (container -> host) and a JSONL file tail."""
from __future__ import annotations

import asyncio
import json

MAX_LINE = 4 * 1024 * 1024


def parse_line(raw):
    try:
        ev = json.loads(raw)
    except Exception:
        return None  # malformed line: skip, never crash the TUI
    return ev if isinstance(ev, dict) else None


async def serve_tcp(host, port, on_event, on_connection=None):
    async def handle(reader, writer):
        if on_connection:
            on_connection(+1)
        try:
            while True:
                try:
                    raw = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # oversized line: drop the buffered data and resync on next newline
                    try:
                        await reader.readuntil(b"\n")
                    except Exception:
                        pass
                    continue
                if not raw:
                    break
                ev = parse_line(raw)
                if ev is not None:
                    on_event(ev)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            if on_connection:
                on_connection(-1)

    return await asyncio.start_server(handle, host, port, limit=MAX_LINE)


async def tail_jsonl(path, on_event, poll=0.2):
    """Follow a JSONL file from the start; survives truncation/rotation."""
    pos = 0
    buf = b""
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                if f.tell() < pos:
                    pos, buf = 0, b""
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except FileNotFoundError:
            chunk = b""
        if chunk:
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                ev = parse_line(raw)
                if ev is not None:
                    on_event(ev)
        await asyncio.sleep(poll)
