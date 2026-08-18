"""Operator-facing functions over the engine. One implementation, two front
ends: api/index.py (HTTP, for the web UI) and mcp_server.py (stdio, for an
agent). Neither front end contains logic -- if the agent and the UI could
disagree about what "simulate" means, the demo would be a lie.

Stdlib only, on purpose: everything here runs inside the serverless function.
The one exception, run_backtest(), shells out to the offline pipeline and says
so clearly when the offline deps are missing.
"""
import json
import os
import subprocess
import sys

import orchestrator as orch
from decline_normalizer import normalize
from orchestrator import ALL_PSPS, ERROR_CLASSES, PSP_FEES, Config, decide

_DIR = os.path.dirname(os.path.abspath(__file__))
CASES_PATH = os.path.join(_DIR, "demo_transactions.json")
BACKTEST_PATH = os.path.join(_DIR, "backtest_summary.json")
ATTEMPTS_PATH = os.path.join(_DIR, "attempts.parquet")

COST_BIAS_SWEEP = [0.0, 0.25, 0.5, 0.75, 1.0]

# ponytail: 12 literals lifted from evals/golden_declines.json rather than a
# lookup into it -- the UI dropdown must not depend on the eval fixture file
# being deployed.
NORMALIZE_SAMPLES = [
    {"psp": "psp-a", "raw_code": "05", "raw_message": None, "label": "ISO 8583 05 -- do not honor (table)"},
    {"psp": "psp-a", "raw_code": "65", "raw_message": None, "label": "ISO 8583 65 -- soft decline, SCA (table)"},
    {"psp": "psp-a", "raw_code": "1A", "raw_message": None, "label": "ISO 8583 1A -- SCA soft decline (off-table)"},
    {"psp": "psp-b", "raw_code": "insufficient_funds", "raw_message": None, "label": "Stripe-like insufficient_funds (table)"},
    {"psp": "psp-b", "raw_code": "authentication_required", "raw_message": None, "label": "Stripe-like authentication_required (table)"},
    {"psp": "psp-b", "raw_code": "insufficent_funds", "raw_message": None, "label": "Stripe-like, misspelled by the PSP (off-table)"},
    {"psp": "psp-c", "raw_code": "FRAUD-CANCELLED", "raw_message": None, "label": "Adyen-like FRAUD-CANCELLED (table)"},
    {"psp": "psp-c", "raw_code": "Blocked Card", "raw_message": None, "label": "Adyen-like Blocked Card (table)"},
    {"psp": "psp-c", "raw_code": "Card Reported Lost", "raw_message": None, "label": "Adyen-like, unusual wording (off-table)"},
    {"psp": "psp-d", "raw_code": None, "raw_message": "Insufficient funds", "label": "Bank text, pinned exact string (table)"},
    {"psp": "psp-d", "raw_code": None, "raw_message": "The transaction was declined because the available balance on the account is lower than the amount requested.", "label": "Bank text, verbose prose (off-table)"},
    {"psp": "psp-d", "raw_code": None, "raw_message": "Declined.", "label": "Bank text, ambiguous (off-table)"},
]


def cases() -> list:
    with open(CASES_PATH) as f:
        return json.load(f)


def meta() -> dict:
    bins = [{"bin6": b, "issuer": i} for b, i in sorted(orch.BIN6_TO_ISSUER.items())[:15]]
    return {
        "psps": list(ALL_PSPS),
        "fees_pct": dict(PSP_FEES),
        "funding": sorted(orch.KNOWN_FUNDING_VALUES),
        "gateways": sorted(orch.KNOWN_GATEWAY_GROUPS),
        "error_classes": list(ERROR_CLASSES),
        "sample_bins": bins,
        "amount_bands": ["Q1_low", "Q2_mid_low", "Q3_mid_high", "Q4_high"],
        "amount_band_edges": list(orch.AMOUNT_EDGES),
    }


def _txn(amount, bin6=None, funding=None, gateway=None, attempt_number=1, error_history=None):
    if amount is None:
        raise ValueError("amount is required")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError(f"amount must be a number, got {amount!r}")
    if not gateway:
        raise ValueError(f"gateway is required, one of {sorted(orch.KNOWN_GATEWAY_GROUPS)}")
    for e in error_history or []:
        if not isinstance(e, dict) or "error_class" not in e:
            raise ValueError("error_history entries must look like {psp, error_class}")
    return {
        "amount": amount,
        "bin6": bin6,
        "funding": funding,
        "gateway": gateway,
        "attempt_number": int(attempt_number or 1),
        "error_history": error_history or [],
    }


def _config(cost_bias=0.0, psps_down=None):
    unknown = sorted(set(psps_down or ()) - set(ALL_PSPS))
    if unknown:
        raise ValueError(f"unknown PSPs in psps_down: {unknown} (known: {list(ALL_PSPS)})")
    return Config(cost_bias=float(cost_bias or 0.0), psps_down=frozenset(psps_down or ()))


def route_transaction(amount, bin6=None, funding=None, gateway=None, attempt_number=1,
                      cost_bias=0.0, psps_down=None, error_history=None) -> dict:
    return decide(_txn(amount, bin6, funding, gateway, attempt_number, error_history),
                  _config(cost_bias, psps_down))


def explain_decision(**kw) -> str:
    d = route_transaction(**kw)
    lines = [f"ROUTE -> {d['route_psp']}" + ("  [STATIC DEFAULT: insufficient data]" if d["static_default"] else "")]
    lines.append("")
    lines.append("segment evidence per PSP:")
    for psp, e in sorted(d["eligible_psps"].items(), key=lambda kv: -kv[1]["expected_net"]):
        thin = "  [THIN]" if e["insufficient_data"] else ""
        lines.append(f"  {psp}: {e['segment_used']}, n={e['n_support']}, "
                     f"p_wilson={e['p_wilson']:.4f}, p_hat={e['p_hat']:.4f}, "
                     f"fee={e['fee_pct'] * 100:.2f}%, expected_net={e['expected_net']:.2f}{thin}")
    rp = d["retry_policy"]
    lines += ["", "retry plan:", f"  should_retry_on_fail: {rp['should_retry_on_fail']}"]
    if rp["stop_reason"]:
        lines.append(f"  stop_reason: {rp['stop_reason']}")
    else:
        lines.append(f"  next_psp_candidates: {rp['next_psp_candidates']}")
        lines.append(f"  when: {rp['when']}")
        if rp.get("note"):
            lines.append(f"  note: {rp['note']}")
    lines += ["", "reasoning trail:"] + [f"  - {r}" for r in d["reasoning"]]
    return "\n".join(lines)


def simulate(amount, bin6=None, funding=None, gateway=None, attempt_number=1,
             cost_bias=0.0, psps_down=None, error_history=None) -> dict:
    """Sweep the cost knob and the one-PSP-down incidents, same transaction."""
    txn = _txn(amount, bin6, funding, gateway, attempt_number, error_history)

    sweep = []
    for cb in COST_BIAS_SWEEP:
        d = decide(txn, _config(cb, psps_down))
        sweep.append({"cost_bias": cb, "route_psp": d["route_psp"],
                      "expected_net": d["eligible_psps"][d["route_psp"]]["expected_net"]})

    scenarios = []
    for psp in ALL_PSPS:
        try:
            d = decide(txn, _config(cost_bias, [psp]))
            scenarios.append({"psps_down": [psp], "route_psp": d["route_psp"],
                              "expected_net": d["eligible_psps"][d["route_psp"]]["expected_net"]})
        except RuntimeError as e:
            scenarios.append({"psps_down": [psp], "route_psp": None, "expected_net": None,
                              "error": str(e)})
    return {"cost_bias_sweep": sweep, "psps_down_scenarios": scenarios}


def segment_evidence(amount, bin6=None, funding=None, gateway=None) -> dict:
    """Raw counts behind every PSP's score: which segment was used and why."""
    txn = _txn(amount, bin6, funding, gateway)
    gw = txn["gateway"]
    if gw not in orch.KNOWN_GATEWAY_GROUPS:
        raise ValueError(f"unknown gateway '{gw}', expected one of {sorted(orch.KNOWN_GATEWAY_GROUPS)}")
    fund = txn["funding"] or "unknown"
    if fund not in orch.KNOWN_FUNDING_VALUES:
        fund = "unknown"
    ib = orch.issuer_bucket(orch.resolve_issuer(txn))
    ab = orch.amount_band(txn["amount"])

    out = {}
    for psp in ALL_PSPS:
        seg = orch._resolve_segment(gw, fund, ib, ab, psp, orch.MIN_SUPPORT_DEFAULT)
        if seg is None:
            out[psp] = {"level": None, "segment_used": None, "n": 0, "approvals": 0,
                        "p_hat": None, "wilson_lb": None}
            continue
        lvl = seg["level"]
        row = orch._LOOKUP[(lvl, gw,
                            fund if lvl <= 2 else "ALL",
                            ib if lvl <= 1 else "ALL",
                            ab if lvl == 0 else "ALL", psp)]
        out[psp] = {
            "level": lvl,
            "segment_used": seg["segment_desc"],
            "n": int(row["n"]),
            "approvals": int(row["approvals"]),
            "p_hat": round(float(row["p_hat"]), 4),
            "wilson_lb": round(float(row["wilson_lb"]), 4),
        }
    return out


def normalize_decline(psp: str, raw_code: str = None, raw_message: str = None) -> dict:
    if not psp:
        raise ValueError("psp is required")
    return normalize(psp, raw_code, raw_message).to_dict()


def cached_backtest() -> dict:
    """The committed headline. The API serves this; it never replays at request time."""
    with open(BACKTEST_PATH) as f:
        return json.load(f)


def run_backtest() -> dict:
    """Replay the backtest live. Needs attempts.parquet + the offline deps.

    Runs in a subprocess so its pandas/duckdb output can never land on an MCP
    server's stdout, where it would corrupt the protocol stream.
    """
    if not os.path.exists(ATTEMPTS_PATH):
        raise RuntimeError(
            "attempts.parquet is missing (it is gitignored -- regenerate it with "
            "`python synth_attempts.py`). Use the cached summary in backtest_summary.json "
            "if the offline pipeline is not available here."
        )
    p = subprocess.run([sys.executable, "backtest.py", "--json"], cwd=_DIR,
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(
            "backtest failed -- it needs the offline dependencies "
            f"(pip install -r requirements-dev.txt). stderr tail: {p.stderr[-400:]}"
        )
    return json.loads(p.stdout)
