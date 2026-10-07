"""testui: runner-side (stdlib only) and TUI-side (needs textual) code.

Only `testui.runner`, `testui.protocol` and `testui.sender` run inside the
test process, and they must stay stdlib-only and fail-silent.
"""
