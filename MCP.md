# MCP server — the agent edge

`mcp_server.py` exposes the routing engine over MCP (stdio) so an analyst can
operate it in English while the decision itself stays deterministic. The agent
can ask *what would route where*, *on what evidence*, and *what if the cost knob
moved* — it cannot change how any of those are computed. Every tool is a thin
wrapper over `ops.py`, the same module the HTTP API calls.

## Register it

The server needs the offline dependencies (`mcp` itself lives in
`requirements-dev.txt`), so point the config at this repo's virtualenv rather
than a system Python.

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "payment-routing": {
      "command": "/absolute/path/to/payment-routing-orchestrator/.venv/bin/python",
      "args": ["mcp_server.py"],
      "cwd": "/absolute/path/to/payment-routing-orchestrator"
    }
  }
}
```

**Claude Code** — same shape, in `.mcp.json` at the repo root, or:

```bash
claude mcp add payment-routing \
  /absolute/path/to/payment-routing-orchestrator/.venv/bin/python mcp_server.py
```

Setup, once:

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python synth_attempts.py && .venv/bin/python build_routing_tables.py
```

## Tools

| tool | when the agent should reach for it |
|---|---|
| `route_transaction` | where one authorization should go, and the retry plan if it declines |
| `explain_decision` | the same thing as prose: pick, per-PSP segment level and support, reasoning trail |
| `simulate` | sensitivity: route + expected net across `cost_bias` 0…1, and under each single-PSP outage |
| `segment_evidence` | the audit view — level, n, approvals, `p_hat`, Wilson LB per PSP |
| `normalize_decline` | a raw PSP decline code or bank message → the engine's `error_class` enum |
| `backtest_summary` | the out-of-sample headline lift, with caveats; `live=True` replays it (needs `attempts.parquet` + offline deps) |

## Three questions it answers well

1. *"psp-b just paged us — it's down. For a 250-unit debit checkout on BIN 596546,
   where does traffic land instead, and what does that cost us per transaction?"*
   → `simulate`, then `explain_decision` with `psps_down=["psp-b"]`.

2. *"Our recurring channel routes to psp-c. Is that a real signal or three lucky
   transactions?"* → `segment_evidence` for a recurring transaction: it returns n
   and approvals per PSP, so the answer is a count, not an opinion.

3. *"The gateway returned `refusalReason: Blocked Card` on a subscription renewal.
   Should we retry tonight?"* → `normalize_decline` first (it resolves to
   `invalid_card_info`), then `route_transaction` with that in `error_history` —
   the state machine stops the chain and says to ask the customer for a new card
   rather than burning scheduled retries.
