#!/usr/bin/env python
"""Guard on the MCP tool surface: all six tools, all four annotation hints,
every one an explicit boolean. Directories reject a tool where any of the four
is missing or non-boolean, and a wrong readOnlyHint is worse than none -- it
tells a host it is safe to call something that is not.

    python test_mcp_tools.py
"""
import asyncio

from mcp_server import mcp

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
# normalize_decline is the only tool that leaves the process (LLM fallback).
OPEN_WORLD = {"normalize_decline"}


def test_tool_annotations():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert len(tools) == 6, f"expected 6 tools, got {len(tools)}: {sorted(names)}"
    assert OPEN_WORLD <= names, f"missing tool: {OPEN_WORLD - names}"

    for tool in tools:
        ann = tool.annotations
        assert ann is not None, f"{tool.name}: no annotations declared"
        for hint in HINTS:
            value = getattr(ann, hint, None)
            assert isinstance(value, bool), f"{tool.name}.{hint} is {value!r}, not a bool"
        # Every tool wraps ops.py, which never writes. If that stops being true,
        # fix the hint here before a host trusts it.
        assert ann.readOnlyHint is True, f"{tool.name}: readOnlyHint must be True"
        assert ann.destructiveHint is False, f"{tool.name}: read-only tool cannot be destructive"
        assert ann.openWorldHint is (tool.name in OPEN_WORLD), \
            f"{tool.name}: openWorldHint={ann.openWorldHint} contradicts {OPEN_WORLD}"


if __name__ == "__main__":
    test_tool_annotations()
    print("ok: 6 tools, all four hints declared as booleans")
