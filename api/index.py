"""Vercel serverless entry point. Thin HTTP shell over ops.py -- no logic here,
so the web UI and the MCP agent can never diverge in what they report.

The whole runtime is fastapi + the standard library: the engine reads committed
JSON tables, and the LLM edge talks to its providers over urllib. Nothing in
this request path imports pandas, duckdb or pyarrow.
"""
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from starlette.routing import Route  # noqa: E402

import ops  # noqa: E402
from mcp_server import mcp  # noqa: E402

app = FastAPI(title="payment routing orchestrator", docs_url="/api/docs")

MCP_PATH = "/api/mcp"


def _mcp_denied(request):
    """None if this MCP request may proceed, otherwise the refusal to return.

    The demo API stays open; the agent edge does not, because an unauthenticated
    caller could run `backtest_summary(live=True)` all day on someone else's bill.
    """
    token = os.environ.get("MCP_TOKEN")
    if not token:
        return JSONResponse(status_code=503, content={"error": "MCP disabled"})
    header = request.headers.get("authorization", "")
    sent = header[7:] if header[:7].lower() == "bearer " else ""
    if not hmac.compare_digest(sent, token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return None


# Vercel's rewrite replaces the request path with the destination, so the
# function would only ever see "/api/index". vercel.json carries the real
# sub-path in `__p`; put it back before the router looks at it.
# ponytail: middleware over renaming this file to a [...path] dynamic route,
# which would scatter the rename through the READMEs for the same result.
# The MCP guard lives here too, so that it reads the restored path rather than
# the rewritten one -- a separate middleware would run in the wrong order.
@app.middleware("http")
async def _restore_path(request, call_next):
    sub = request.query_params.get("__p")
    if sub is not None:
        request.scope["path"] = "/api/" + sub
    if request.scope["path"].startswith(MCP_PATH):
        return _mcp_denied(request) or await call_next(request)
    return await call_next(request)


class _MCPEndpoint:
    """The same six tools as the stdio server, over Streamable HTTP.

    ponytail: a fresh session manager per request instead of one held open by an
    ASGI lifespan. Stateless mode builds a new transport per request anyway, so
    nothing is lost -- and nothing here depends on a long-lived event loop, which
    a serverless function does not promise between invocations.
    """

    async def __call__(self, scope, receive, send):
        manager = StreamableHTTPSessionManager(
            app=mcp._mcp_server, stateless=True, json_response=True)
        async with manager.run():
            await manager.handle_request(scope, receive, send)


# Appended rather than declared with @app.post: the transport speaks raw ASGI
# (it owns the response), and the route has to match /api/mcp exactly.
app.router.routes.append(Route(MCP_PATH, _MCPEndpoint(), methods=["GET", "POST", "DELETE"]))


def _bad(e):
    return JSONResponse(status_code=400, content={"error": str(e)})


# The handlers keep taking a bare dict so ops.py stays the only validator -- a
# pydantic model here would move "amount is required" into a second authority and
# turn it into a 422, contradicting this module's whole premise. The cost is that
# FastAPI infers an empty `{additionalProperties: true}` body schema, leaving the
# spec useless for generating a client, so the schema is declared by hand below.
# The drift that invites is guarded mechanically: a test in tests_ai.py asserts
# these keys are exactly the ones _txn_args reads out of the body.
_TXN_FIELDS = {
    "amount": {"type": "number", "description": "Minor-unit-agnostic amount. Required."},
    "bin6": {"type": "string", "description": "First 6 digits of the PAN; resolves the issuer."},
    "funding": {"type": "string", "description": "See /api/meta -> funding."},
    "gateway": {"type": "string", "description": "See /api/meta -> gateways. Required."},
    "attempt_number": {"type": "integer", "default": 1},
    "cost_bias": {"type": "number", "default": 0,
                  "description": "0 = route on approval alone; 1 = tolerate 10pp of approval to save fee."},
    "psps_down": {"type": "array", "items": {"type": "string"},
                  "description": "PSPs to exclude from this decision."},
    "error_history": {"type": "array", "description": "Prior failures on this transaction.",
                      "items": {"type": "object",
                                "properties": {"psp": {"type": "string"},
                                               "error_class": {"type": "string"}}}},
}

_ERROR_SCHEMA = {"type": "object", "properties": {"error": {"type": "string"}}}
_BAD_RESPONSE = {400: {"description": "Validation failed in ops.py",
                       "content": {"application/json": {"schema": _ERROR_SCHEMA}}}}


def _body_spec(fields: dict, required: list, nested_txn: bool) -> dict:
    """An explicit requestBody schema for a handler that takes a bare dict."""
    props = dict(fields)
    if nested_txn:
        props["txn"] = {"type": "object", "properties": dict(fields),
                        "description": "The nested form /api/cases returns; posted straight back, "
                                       "it is flattened. Top-level keys win on conflict."}
    schema = {"type": "object", "properties": props, "additionalProperties": True}
    if not nested_txn:
        schema["required"] = required
    return {"content": {"application/json": {"schema": schema}}, "required": True}


def _txn_args(body: dict) -> dict:
    # /api/cases hands back {txn: {...}, cost_bias, why_interesting}, which is what
    # a caller copies straight into /api/decide -- and until this flattened it, that
    # obvious first move answered "amount is required". The web UI reads the nested
    # shape (public/app.js), so /api/cases keeps it and the input side gives way.
    txn = body.get("txn")
    if isinstance(txn, dict):
        body = {**txn, **{k: v for k, v in body.items() if k != "txn"}}
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


@app.post("/api/decide", responses=_BAD_RESPONSE,
          openapi_extra={"requestBody": _body_spec(_TXN_FIELDS, ["amount", "gateway"], True)})
def post_decide(body: dict):
    try:
        return ops.route_transaction(**_txn_args(body))
    except (ValueError, RuntimeError, TypeError) as e:
        return _bad(e)


@app.post("/api/simulate", responses=_BAD_RESPONSE,
          openapi_extra={"requestBody": _body_spec(_TXN_FIELDS, ["amount", "gateway"], True)})
def post_simulate(body: dict):
    try:
        return ops.simulate(**_txn_args(body))
    except (ValueError, RuntimeError, TypeError) as e:
        return _bad(e)


@app.post("/api/evidence", responses=_BAD_RESPONSE,
          openapi_extra={"requestBody": _body_spec(
              {k: _TXN_FIELDS[k] for k in ("amount", "bin6", "funding", "gateway")},
              ["amount", "gateway"], False)})
def post_evidence(body: dict):
    try:
        return ops.segment_evidence(amount=body.get("amount"), bin6=body.get("bin6"),
                                    funding=body.get("funding"), gateway=body.get("gateway"))
    except (ValueError, RuntimeError, TypeError) as e:
        return _bad(e)


@app.post("/api/normalize", responses=_BAD_RESPONSE,
          openapi_extra={"requestBody": _body_spec(
              {"psp": {"type": "string", "description": "See /api/meta -> psps. Required."},
               "raw_code": {"type": "string", "description": "Raw decline code from the PSP."},
               "raw_message": {"type": "string", "description": "Raw decline message, if any."}},
              ["psp"], False)})
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
