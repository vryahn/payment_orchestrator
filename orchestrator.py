"""Pure PSP routing + retry decision engine. No I/O side effects at call time;
routing_tables.json / routing_meta.json are loaded once at import with stdlib
`json` only -- no pandas/duckdb/pyarrow -- so this module runs unchanged inside
a serverless function.

    decide(txn: dict, config: Config) -> dict   # the "Decision"

Design thesis: the hot path is deterministic and auditable. There is no LLM
inside decide() and there never will be -- routing money on a sampled token is
not an engineering trade-off anyone wants to defend in a post-mortem. The
language model lives only at the EDGES of this engine:

  IN   decline_normalizer.py maps each PSP's private decline dialect onto the
       six error classes this state machine keys on, with a deterministic table
       first and the LLM only on a table miss.
  OUT  mcp_server.py lets an agent operate and explain the engine (route,
       simulate, inspect segment evidence) without being able to alter a
       decision.

txn keys: amount (float), bin6 (str, optional), issuer (str, optional),
funding (str, optional), gateway (str: checkout/recurring/pos),
error_history (list[{psp, error_class}], optional), attempt_number (int, default 1).

Channels:
  checkout   user-present, card-not-present
  recurring  merchant-initiated, off-session, scheduled (no user to prompt)
  pos        card-present terminal
"""
import json
import os
from dataclasses import dataclass, field

_DIR = os.path.dirname(os.path.abspath(__file__))
TABLES_PATH = os.path.join(_DIR, "routing_tables.json")
META_PATH = os.path.join(_DIR, "routing_meta.json")

with open(META_PATH) as f:
    _meta = json.load(f)
with open(TABLES_PATH) as f:
    _tables = json.load(f)

PSP_FEES = _meta["psp_fees"]
AMOUNT_EDGES = _meta["amount_band_edges"]
TOP_ISSUERS = set(_meta["top_issuers"])
BIN6_TO_ISSUER = _meta["bin6_to_issuer"]
MIN_SUPPORT_DEFAULT = _meta["min_support_default"]
KNOWN_GATEWAY_GROUPS = set(_meta["gateway_groups"])
KNOWN_FUNDING_VALUES = set(_meta["funding_values"])
# The only vocabulary the retry state machine below understands. Anything a PSP
# returns has to be normalized onto this enum before it reaches _retry_policy().
ERROR_CLASSES = _meta["error_classes"]

# The PSP roster comes from the data, not from the engine: no PSP name is
# hardcoded in any routing decision.
ALL_PSPS = sorted(PSP_FEES)
VOLUME_ORDER = _meta["psp_volume_order"]  # tiebreak for the static-default path

# Channels with no user in the loop: retries are scheduled, not immediate, and
# a step-up authentication cannot be satisfied in-flight.
OFF_SESSION_CHANNELS = frozenset({"recurring"})

_FEE_MIN, _FEE_MAX = min(PSP_FEES.values()), max(PSP_FEES.values())
_FEE_SPREAD_PP = (_FEE_MAX - _FEE_MIN) * 100

# lookup: (level, gateway_group, funding, issuer_bucket, amount_band, psp) -> row dict
_LOOKUP = {
    (r["level"], r["gateway_group"], r["funding"], r["issuer_bucket"], r["amount_band"], r["psp"]): r
    for r in _tables
}

# which real dims are "live" (non-'ALL') at each level, finest to coarsest
_HIERARCHY = {0: "gateway_group/funding/issuer_bucket/amount_band",
              1: "gateway_group/funding/issuer_bucket",
              2: "gateway_group/funding",
              3: "gateway_group"}


def amount_band(a: float) -> str:
    e = AMOUNT_EDGES
    if a <= e[0]:
        return "Q1_low"
    if a <= e[1]:
        return "Q2_mid_low"
    if a <= e[2]:
        return "Q3_mid_high"
    return "Q4_high"


def issuer_bucket(issuer):
    if not issuer:
        return "OTHER"
    return issuer if issuer in TOP_ISSUERS else "OTHER"


def resolve_issuer(txn: dict):
    if txn.get("issuer"):
        return txn["issuer"]
    bin6 = txn.get("bin6")
    if bin6 and bin6 in BIN6_TO_ISSUER:
        return BIN6_TO_ISSUER[bin6]
    return None


@dataclass
class Config:
    # cost_bias in [0,1]: 0 = pure expected-net routing (DEFAULT, == max approval
    # since tolerance is 0); 1 = minimize fee among PSPs within 10pp of the best
    # approval. The knob is expressed in percentage points of approval you are
    # willing to trade for a cheaper PSP, not in an abstract normalized score.
    cost_bias: float = 0.0
    psps_down: frozenset = field(default_factory=frozenset)
    min_support: int = MIN_SUPPORT_DEFAULT
    max_attempts_user_present: int = 8
    max_attempts_off_session: int = 20
    # channel -> PSPs to keep out of that channel's candidate pool. Use when a
    # PSP's data for a channel is known to be unrepresentative (an
    # instrumentation gap, a partial rollout, a contractual restriction).
    excluded_psps_by_channel: dict = field(default_factory=dict)

    def __post_init__(self):
        self.cost_bias = max(0.0, min(1.0, self.cost_bias))
        self.psps_down = frozenset(self.psps_down)


def _resolve_segment(gateway_group, funding, issuer_bkt, amt_band, psp, min_support):
    """Walk L0 -> L3 for this psp, return first cell meeting min_support.
    If none meet it, fall back to L3 anyway (flagged insufficient=True).
    Returns None only if the psp has zero data at every level (unseen gateway_group)."""
    for level in range(4):
        key = (
            level,
            gateway_group,
            funding if level <= 2 else "ALL",
            issuer_bkt if level <= 1 else "ALL",
            amt_band if level == 0 else "ALL",
            psp,
        )
        row = _LOOKUP.get(key)
        if row and row["n"] >= min_support:
            return {
                "level": level,
                "n": int(row["n"]),
                "p_hat": float(row["p_hat"]),
                "p_wilson": float(row["wilson_lb"]),
                "segment_desc": f"L{level} ({_HIERARCHY[level]})",
                "insufficient": False,
            }
    # nothing met threshold: fall back to coarsest (L3) data if it exists at all
    row = _LOOKUP.get((3, gateway_group, "ALL", "ALL", "ALL", psp))
    if row is None:
        return None
    return {
        "level": 3,
        "n": int(row["n"]),
        "p_hat": float(row["p_hat"]),
        "p_wilson": float(row["wilson_lb"]),
        "segment_desc": f"L3 ({_HIERARCHY[3]})",
        "insufficient": True,
    }


def _build_pool(gateway_group, config, reasoning):
    pool = list(ALL_PSPS)
    if config.psps_down:
        pool = [p for p in pool if p not in config.psps_down]
        reasoning.append(f"PSPs marked DOWN by the operator: {sorted(config.psps_down)}")

    excluded = set(config.excluded_psps_by_channel.get(gateway_group, ()))
    hit = sorted(p for p in pool if p in excluded)
    if hit:
        pool = [p for p in pool if p not in excluded]
        reasoning.append(
            f"excluded_psps_by_channel: {hit} not eligible on channel '{gateway_group}' "
            "(configured because that PSP's data for this channel is not trustworthy "
            "enough to route on -- other channels are unaffected)"
        )

    reasoning.append(f"eligible pool for '{gateway_group}': {pool}")
    return pool


def _retry_policy(txn, config, scored, is_off_session, reasoning):
    error_history = txn.get("error_history") or []
    attempt_number = txn.get("attempt_number", 1)
    max_attempts = config.max_attempts_off_session if is_off_session else config.max_attempts_user_present
    channel = "off-session" if is_off_session else "user-present"
    scheduled = ("next-billing-window (prefer a window with historically higher approval "
                 "if the data shows one)")
    when_default = scheduled if is_off_session else "immediate"
    ranked = [p for p, _ in sorted(scored.items(), key=lambda kv: -kv[1]["expected_net"])]

    def no_retry(reason):
        return {"should_retry_on_fail": False, "next_psp_candidates": [], "when": None, "stop_reason": reason}

    if attempt_number >= max_attempts:
        return no_retry(f"max attempts reached ({max_attempts}) for the {channel} channel")

    if any(e.get("error_class") == "fraud_risk" for e in error_history):
        return no_retry("hard stop: fraud_risk observed at some point in this transaction's attempt history")

    if not error_history:
        return {
            "should_retry_on_fail": True,
            "next_psp_candidates": ranked,
            "when": when_default,
            "stop_reason": None,
            "note": "no prior failure on this transaction; if this attempt fails, the generic_decline "
                    "policy applies by default (one failover to next-best PSP)",
        }

    last = error_history[-1]
    last_psp, last_err = last.get("psp"), last.get("error_class")
    next_candidates = [p for p in ranked if p != last_psp]  # never retry same PSP immediately after its own decline

    if last_err in ("fraud_risk", "invalid_card_info"):
        note = " Suggest customer action (new card)." if not is_off_session else ""
        return no_retry(
            f"no retry: {last_err} has low marginal recovery -- retrying burns fees and customer "
            f"goodwill for little upside.{note}"
        )

    if last_err == "insufficient_funds":
        if is_off_session:
            return {
                "should_retry_on_fail": True,
                "next_psp_candidates": [last_psp] if last_psp else next_candidates,
                "when": scheduled,
                "stop_reason": None,
                "note": "insufficient_funds is an account-funding problem, not a PSP problem: retry the "
                        "SAME psp on the next billing window rather than failing over. Marginal recovery "
                        "on a rescheduled retry is the highest of any decline reason.",
            }
        return {
            "should_retry_on_fail": True,
            "next_psp_candidates": next_candidates[:1],
            "when": "immediate one-shot failover, then schedule to the next billing window if that also fails",
            "stop_reason": None,
            "note": "insufficient_funds user-present: allow one immediate failover to next-best PSP, "
                    "then fall back to scheduled retries",
        }

    if last_err == "bank_auth_required":
        if is_off_session:
            return {
                "should_retry_on_fail": False,
                "next_psp_candidates": [],
                "when": "reschedule-to-user-present",
                "stop_reason": "bank_auth_required off-session: reschedule to a user-present channel and "
                                "notify the user -- don't burn scheduled retries on a blind retry that "
                                "can't satisfy a step-up authentication",
            }
        return {
            "should_retry_on_fail": True,
            "next_psp_candidates": [last_psp],
            "when": "immediate (requires user to complete a 3DS/auth step)",
            "stop_reason": None,
            "note": "bank_auth_required user-present: prompt the 3DS/auth flow once on the SAME psp, "
                    "no blind same-PSP retry without the user completing auth",
        }

    if last_err in ("generic_decline", "other"):
        tail = 0
        for e in reversed(error_history):
            if e.get("error_class") in ("generic_decline", "other"):
                tail += 1
            else:
                break
        if tail >= 2:
            return no_retry("generic_decline/other: already failed over once and failed again -> stop")
        return {
            "should_retry_on_fail": True,
            "next_psp_candidates": next_candidates[:1],
            "when": when_default,
            "stop_reason": None,
            "note": "generic_decline/other: one failover attempt to the next-best PSP allowed",
        }

    return {
        "should_retry_on_fail": True,
        "next_psp_candidates": next_candidates[:1],
        "when": when_default,
        "stop_reason": None,
        "note": f"unrecognized error_class '{last_err}' -> defaulting to generic-decline failover policy",
    }


def decide(txn: dict, config: Config) -> dict:
    reasoning = []
    amount = txn["amount"]
    if amount is None or amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    gateway_group = txn.get("gateway") or txn.get("gateway_group")
    is_off_session = gateway_group in OFF_SESSION_CHANNELS
    funding = txn.get("funding") or "unknown"

    if gateway_group not in KNOWN_GATEWAY_GROUPS:
        reasoning.append(f"WARNING: gateway_group '{gateway_group}' not seen in training data ({sorted(KNOWN_GATEWAY_GROUPS)})")
    if funding not in KNOWN_FUNDING_VALUES:
        reasoning.append(f"unknown/unseen funding value '{funding}' -> treated as 'unknown' bucket")
        funding = "unknown"

    issuer = resolve_issuer(txn)
    issuer_bkt = issuer_bucket(issuer)
    if issuer is None:
        reasoning.append("no issuer resolvable from bin6/issuer input -> issuer_bucket='OTHER' (hierarchy falls back past the issuer dimension if OTHER is thin)")
    else:
        reasoning.append(f"issuer resolved to '{issuer}' -> issuer_bucket='{issuer_bkt}'" + ("" if issuer_bkt != "OTHER" else " (outside the top-volume issuer whitelist)"))

    amt_band = amount_band(amount)
    reasoning.append(f"amount={amount} -> amount_band='{amt_band}' (edges={AMOUNT_EDGES})")
    reasoning.append(f"channel: gateway_group='{gateway_group}', is_off_session={is_off_session}")

    pool = _build_pool(gateway_group, config, reasoning)
    if not pool:
        raise RuntimeError(
            "All PSPs are down or ineligible for this transaction "
            f"(gateway_group={gateway_group}, psps_down={sorted(config.psps_down)}). Cannot route."
        )

    evals = {}
    for psp in pool:
        seg = _resolve_segment(gateway_group, funding, issuer_bkt, amt_band, psp, config.min_support)
        if seg is None:
            reasoning.append(f"{psp}: no data at any hierarchy level for gateway_group='{gateway_group}' -> excluded")
            continue
        evals[psp] = seg
        flag = " [THIN: below min_support even at coarsest level]" if seg["insufficient"] else ""
        reasoning.append(f"{psp}: segment={seg['segment_desc']}, n={seg['n']}, p_wilson={seg['p_wilson']:.3f}{flag}")

    if not evals:
        raise RuntimeError(f"No routing data available for any eligible PSP in gateway_group='{gateway_group}'. Cannot route.")

    scored = {}
    for psp, seg in evals.items():
        scored[psp] = {
            "p_wilson": round(seg["p_wilson"], 4),
            "p_hat": round(seg["p_hat"], 4),
            "fee_pct": PSP_FEES[psp],
            # expected net collected, currency-agnostic
            "expected_net": round(seg["p_wilson"] * amount * (1 - PSP_FEES[psp]), 2),
            "segment_used": seg["segment_desc"],
            "hierarchy_level": seg["level"],
            "n_support": seg["n"],
            "insufficient_data": seg["insufficient"],
        }

    reasoning.append(
        f"fee spread across PSPs is {_FEE_SPREAD_PP:.2f}pp of amount; approval gaps are typically "
        f"larger, so approval dominates routing economics and the real cost lever is retry burn"
    )

    # cost_bias=0: pure expected-net routing (tolerance=0 -> best-approval PSP).
    # cost_bias=1: minimize fee among PSPs within 10pp of the best approval.
    best_psp = max(scored, key=lambda p: scored[p]["p_wilson"])
    best_p = scored[best_psp]["p_wilson"]
    tolerance = config.cost_bias * 0.10
    survivors = [p for p in scored if scored[p]["p_wilson"] >= best_p - tolerance]
    route_psp = min(survivors, key=lambda p: scored[p]["fee_pct"])
    reasoning.append(
        f"cost_bias={config.cost_bias}: tolerance={tolerance*100:.1f}pp of best approval "
        f"({best_psp} p_wilson={best_p:.3f}); survivors within tolerance: {survivors}"
    )

    static_default = False
    if all(v["insufficient_data"] for v in scored.values()):
        static_pick = next((p for p in VOLUME_ORDER if p in scored), route_psp)
        route_psp = static_pick
        static_default = True
        reasoning.append(
            f"insufficient data at every hierarchy level for every eligible PSP -> static default "
            f"routing to current volume leader '{route_psp}' (reason: insufficient data)"
        )
    else:
        reasoning.append(
            f"final pick: {route_psp} (cheapest among survivors, fee={scored[route_psp]['fee_pct']*100:.2f}%, "
            f"expected_net={scored[route_psp]['expected_net']:.2f})"
        )

    policy = _retry_policy(txn, config, scored, is_off_session, reasoning)
    if policy["stop_reason"]:
        reasoning.append(f"retry policy: STOP -- {policy['stop_reason']}")
    else:
        reasoning.append(
            f"retry policy: retry allowed, candidates={policy['next_psp_candidates']}, when={policy['when']}"
        )

    return {
        "route_psp": route_psp,
        "eligible_psps": scored,
        "retry_policy": policy,
        "reasoning": reasoning,
        "static_default": static_default,
        "segment_inputs": {
            "gateway_group": gateway_group,
            "funding": funding,
            "issuer": issuer,
            "issuer_bucket": issuer_bkt,
            "amount_band": amt_band,
        },
    }
