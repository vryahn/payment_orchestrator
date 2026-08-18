"""Vercel serverless entry point. Thin HTTP shell over ops.py -- no logic here,
so the web UI and the MCP agent can never diverge in what they report.

The whole runtime is fastapi + the standard library: the engine reads committed
JSON tables, and the LLM edge talks to its providers over urllib. Nothing in
this request path imports pandas, duckdb or pyarrow.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import ops  # noqa: E402

app = FastAPI(title="payment routing orchestrator", docs_url="/api/docs")


# Vercel's rewrite replaces the request path with the destination, so the
# function would only ever see "/api/index". vercel.json carries the real
# sub-path in `__p`; put it back before the router looks at it.
# ponytail: middleware over renaming this file to a [...path] dynamic route,
# which would scatter the rename through the READMEs for the same result.
@app.middleware("http")
async def _restore_path(request, call_next):
    sub = request.query_params.get("__p")
    if sub is not None:
        request.scope["path"] = "/api/" + sub
    return await call_next(request)


def _bad(e):
    return JSONResponse(status_code=400, content={"error": str(e)})


def _txn_args(body: dict) -> dict:
    return {
        "amount": body.get("amount"),
        "bin6": body.get("bin6"),
        "funding": body.get("funding"),
        "gateway": body.get("gateway"),
        "attempt_number": body.get("attempt_number", 1),
        "cost_bias": body.get("cost_bias", 0),
        "psps_down": body.get("psps_down") or [],
        "error_history": body.get("error_history") or [],
    }


@app.get("/api/cases")
def get_cases():
    return ops.cases()


@app.get("/api/meta")
def get_meta():
    return ops.meta()


@app.post("/api/decide")
def post_decide(body: dict):
    try:
        return ops.route_transaction(**_txn_args(body))
    except (ValueError, RuntimeError, TypeError) as e:
        return _bad(e)


@app.post("/api/simulate")
def post_simulate(body: dict):
    try:
        return ops.simulate(**_txn_args(body))
    except (ValueError, RuntimeError, TypeError) as e:
        return _bad(e)


@app.post("/api/evidence")
def post_evidence(body: dict):
    try:
        return ops.segment_evidence(amount=body.get("amount"), bin6=body.get("bin6"),
                                    funding=body.get("funding"), gateway=body.get("gateway"))
    except (ValueError, RuntimeError, TypeError) as e:
        return _bad(e)


@app.post("/api/normalize")
def post_normalize(body: dict):
    try:
        out = ops.normalize_decline(psp=body.get("psp"), raw_code=body.get("raw_code"),
                                    raw_message=body.get("raw_message"))
    except (ValueError, TypeError) as e:
        return _bad(e)
    return {**out, "error_class_options": list(ops.ERROR_CLASSES)}


@app.get("/api/normalize/samples")
def get_normalize_samples():
    return ops.NORMALIZE_SAMPLES


@app.get("/api/backtest")
def get_backtest():
    return ops.cached_backtest()
