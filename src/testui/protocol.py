"""Event schema helpers shared by the runner and the TUI. Stdlib only."""
from __future__ import annotations

import re

SCHEMA = 1
MAX_TEXT = 20000
MAX_LINES = 200

_FRAME_RE = re.compile(r'^\s*File "(?P<file>.+)", line (?P<line>\d+), in (?P<func>.+?)\s*$')
_CARET_RE = re.compile(r"^[\s~^]+$")


def truncate(text, limit=MAX_TEXT):
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n... [truncated %d chars] ...\n" % (len(text) - limit) + text[-half:]


def is_project_file(path, root):
    if not path or "site-packages" in path or "/lib/python" in path:
        return False
    if path.startswith("<"):
        return False
    return bool(root) and path.startswith(root.rstrip("/") + "/")


def parse_frames(tb_text, root):
    """Parse `File "x", line N, in f` blocks out of formatted traceback text.

    Text parsing (rather than walking tb objects) works identically for real
    tracebacks and the pickled ones Django's --parallel mode sends back.
    """
    frames = []
    lines = tb_text.splitlines()
    i = 0
    while i < len(lines):
        m = _FRAME_RE.match(lines[i])
        if m:
            code = ""
            j = i + 1
            while j < len(lines) and not _FRAME_RE.match(lines[j]):
                cand = lines[j]
                if cand.strip() and not _CARET_RE.match(cand) and cand.startswith("    "):
                    code = cand.strip()
                    break
                if cand.strip() and not cand.startswith(" "):
                    break
                j += 1
            frames.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "func": m.group("func"),
                    "code": code,
                    "project": is_project_file(m.group("file"), root),
                }
            )
        i += 1
    return frames


def primary_frame_index(frames):
    """Deepest frame in project code (the assert line); else the deepest frame."""
    for idx in range(len(frames) - 1, -1, -1):
        if frames[idx]["project"]:
            return idx
    return len(frames) - 1 if frames else None


def short_message(tb_text):
    """First line of the exception message, from the last non-frame block."""
    lines = [ln for ln in tb_text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not ln.startswith((" ", "\t")) and not ln.startswith("Traceback"):
            return truncate(ln, 300)
    return truncate(lines[-1], 300) if lines else ""
