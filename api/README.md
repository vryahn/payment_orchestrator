# HTTP API

FastAPI on Vercel's Python runtime. `vercel.json` rewrites `/api/(.*)` to
`/api/index`; static files in `public/` are served by Vercel directly, so the UI
and the API share an origin and no CORS setup is needed.

`api/index.py` holds no logic — every endpoint is one call into `ops.py`, the
same module `mcp_server.py` uses. The runtime is **fastapi + the standard
library**: the engine reads committed JSON tables (`routing_tables.json`,
`routing_meta.json`), and the decline normalizer talks to its LLM providers over
`urllib`. Nothing in a request path imports pandas, duckdb or pyarrow — those
are offline-only and live in `requirements-dev.txt`.

Deploy note: the tables, `demo_transactions.json` and `backtest_summary.json`
are committed on purpose. The build step never runs the offline pipeline, so if
a deploy ever 500s on import, the cause is those files not being bundled with
the function, not a missing dependency.

Run locally:

```bash
.venv/bin/uvicorn api.index:app --reload --port 8000
```

## Endpoints

| method | path | body | returns |
|---|---|---|---|
| GET | `/api/cases` | — | the 8 demo transactions verbatim: `[{txn, cost_bias, why_interesting}]` |
| GET | `/api/meta` | — | `{psps, fees_pct, funding, gateways, error_classes, sample_bins, amount_bands, amount_band_edges}` |
| POST | `/api/decide` | txn body | the engine's decision dict as-is |
| POST | `/api/simulate` | txn body | `{cost_bias_sweep, psps_down_scenarios}` |
| POST | `/api/evidence` | `{amount, bin6, funding, gateway}` | `{psp: {level, segment_used, n, approvals, p_hat, wilson_lb}}` |
| POST | `/api/normalize` | `{psp, raw_code?, raw_message?}` | `Normalized` + `error_class_options` |
| GET | `/api/normalize/samples` | — | ~12 curated raw declines for a dropdown |
| GET | `/api/backtest` | — | the cached backtest headline (never replayed at request time) |

Transaction body:

```json
{
  "amount": 250,
  "bin6": "596546",
  "funding": "debit",
  "gateway": "checkout",
  "attempt_number": 1,
  "cost_bias": 0,
  "psps_down": [],
  "error_history": [{"psp": "psp-a", "error_class": "insufficient_funds"}]
}
```

`amount` and `gateway` are required; the rest default. Validation failures
return `400 {"error": "..."}` — including an unroutable channel, an unknown PSP
in `psps_down`, or a non-positive amount.

## curl

```bash
BASE=http://localhost:8000
TXN='{"amount":250,"bin6":"596546","funding":"debit","gateway":"checkout","cost_bias":0}'

curl -s $BASE/api/cases | jq '.[0].why_interesting'
curl -s $BASE/api/meta | jq '{psps, error_classes}'
curl -s $BASE/api/backtest | jq '{headline_lift_pp, n_test_txns}'
curl -s $BASE/api/normalize/samples | jq '.[0]'

curl -s -XPOST $BASE/api/decide   -H 'content-type: application/json' -d "$TXN" | jq '{route_psp, static_default}'
curl -s -XPOST $BASE/api/simulate -H 'content-type: application/json' -d "$TXN" | jq '.cost_bias_sweep'
curl -s -XPOST $BASE/api/evidence -H 'content-type: application/json' \
  -d '{"amount":250,"bin6":"596546","funding":"debit","gateway":"checkout"}' | jq '."psp-a"'
curl -s -XPOST $BASE/api/normalize -H 'content-type: application/json' \
  -d '{"psp":"psp-c","raw_code":"Not enough balance"}' | jq '{error_class, source}'

# retry plan after a decline, with the PSP that declined marked down
curl -s -XPOST $BASE/api/decide -H 'content-type: application/json' -d '{
  "amount":320,"bin6":"528772","funding":"debit","gateway":"recurring","attempt_number":2,
  "error_history":[{"psp":"psp-c","error_class":"insufficient_funds"}]
}' | jq '.retry_policy'
```

## LLM keys

`/api/normalize` answers from the deterministic table with no key configured;
table misses fall back to `generic_decline` and say so in `reasoning`. To enable
the LLM route, set `GEMINI_KEY` (or `MISTRAL_KEY`) as a Vercel environment
variable — `VITE_`-prefixed names are also honoured.
