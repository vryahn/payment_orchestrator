#!/usr/bin/env python
"""The LLM edge on the way OUT: an agent can operate and interrogate the
routing engine, but cannot change what it decides.

Every tool below is a thin wrapper over ops.py -- the same functions the HTTP
API calls. The agent gets the reasoning trail, the segment counts and the
what-if sweeps; the decision itself stays deterministic. That asymmetry is the
whole design: an analyst asks questions in English, a state machine answers in
numbers it can show its work for.

    python mcp_server.py     # stdio transport; see MCP.md for registration
"""
import ops
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("payment-routing-orchestrator")


@mcp.tool()
def route_transaction(amount: float, gateway: str, bin6: str = None, funding: str = None,
                      attempt_number: int = 1, cost_bias: float = 0.0,
                      psps_down: list[str] = None,
                      error_history: list[dict] = None) -> dict:
    """Decide which PSP one authorization should go to, and what to do if it declines.

    Call this when the user asks where a specific transaction should route, or what
    the retry plan for it is. gateway is one of checkout (user-present),
    recurring (off-session, scheduled) or pos (card-present). cost_bias in [0,1] is
    how many percentage points of approval (up to 10) the operator will trade for a
    cheaper PSP. psps_down marks providers as unavailable for this decision only.
    error_history is the prior declines on this same transaction, as
    [{"psp": "psp-a", "error_class": "insufficient_funds"}] -- error_class values must
    already be normalized (use normalize_decline first if you have a raw PSP code).

    Returns the full decision including the reasoning trail. For a readable narrative
    instead of the raw dict, call explain_decision.
    """
    return ops.route_transaction(amount=amount, bin6=bin6, funding=funding, gateway=gateway,
                                 attempt_number=attempt_number, cost_bias=cost_bias,
                                 psps_down=psps_down, error_history=error_history)


@mcp.tool()
def explain_decision(amount: float, gateway: str, bin6: str = None, funding: str = None,
                     attempt_number: int = 1, cost_bias: float = 0.0,
                     psps_down: list[str] = None,
                     error_history: list[dict] = None) -> str:
    """Explain a routing decision in prose: the pick, each PSP's segment level and
    support, the retry plan and the full reasoning trail.

    Call this instead of route_transaction whenever the user asks WHY a transaction
    routes where it does, or wants something they can paste into a review. Same
    arguments as route_transaction.
    """
    return ops.explain_decision(amount=amount, bin6=bin6, funding=funding, gateway=gateway,
                                attempt_number=attempt_number, cost_bias=cost_bias,
                                psps_down=psps_down, error_history=error_history)


@mcp.tool()
def simulate(amount: float, gateway: str, bin6: str = None, funding: str = None,
             attempt_number: int = 1, cost_bias: float = 0.0,
             error_history: list[dict] = None) -> dict:
    """What-if sweep for one transaction: the route and expected net at cost_bias
    0 / 0.25 / 0.5 / 0.75 / 1.0, plus the route under each single-PSP outage.

    Call this when the user asks how sensitive a route is to the cost knob ("at what
    point does this move to the cheap provider?"), or what happens during an incident
    ("if psp-b goes down, where does this land and what does it cost us?").
    """
    return ops.simulate(amount=amount, bin6=bin6, funding=funding, gateway=gateway,
                        attempt_number=attempt_number, cost_bias=cost_bias,
                        error_history=error_history)


@mcp.tool()
def segment_evidence(amount: float, gateway: str, bin6: str = None,
                     funding: str = None) -> dict:
    """The raw evidence behind a route: per PSP, the hierarchy level used, the segment
    description, n, approvals, p_hat and the Wilson lower bound.

    Call this when the user challenges a decision ("is that number real or is it three
    observations?"), or asks how much data supports a PSP in some segment. This is the
    audit view -- it shows counts, not conclusions.
    """
    return ops.segment_evidence(amount=amount, bin6=bin6, funding=funding, gateway=gateway)


@mcp.tool()
def normalize_decline(psp: str, raw_code: str = None, raw_message: str = None) -> dict:
    """Translate one PSP's raw decline (ISO 8583 code, Stripe-like decline_code,
    Adyen-like refusalReason, or free bank text) into the engine's error_class enum.

    Call this FIRST whenever the user hands you a raw decline code or a bank message and
    then wants a retry decision -- route_transaction only understands normalized
    error_class values. Returns the class, a confidence, and whether it came from the
    deterministic table, an LLM, or the safe fallback.
    """
    return ops.normalize_decline(psp=psp, raw_code=raw_code, raw_message=raw_message)


@mcp.tool()
def backtest_summary(live: bool = False) -> dict:
    """The out-of-sample backtest headline: expected approval lift vs the historical
    routing, per cost_bias, with its caveats.

    Call this when the user asks whether the router actually beats what was already
    happening, or for the headline number. Defaults to the committed summary, which is
    instant. Pass live=True to replay the backtest now -- that needs attempts.parquet
    and the offline dependencies, takes a few seconds, and errors clearly if either is
    missing. The lift is directional evidence from a static replay, not an A/B result;
    the caveats travel with the number for that reason.
    """
    return ops.run_backtest() if live else ops.cached_backtest()


if __name__ == "__main__":
    mcp.run()
