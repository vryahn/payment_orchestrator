"""Out-of-sample backtest: TRAIN segment tables on days 1-21, evaluate the
orchestrator's routing choice on first attempts (retry_index=1) of days 22-31.
Directional evidence only -- see the caveats printed at the end.

Reuses amount_band / issuer_bucket / PSP fees from orchestrator.py (those are
structural definitions, not fitted probabilities, so computing them on the full
month is not train/test leakage). The approval-probability TABLE itself
(n, p_hat, wilson_lb) is rebuilt from the TRAIN split only.

Run with --json to emit the summary as JSON on stdout instead of the human
report (that is how backtest_summary.json is produced, and how ops.run_backtest
reads results back without polluting an MCP server's stdout).
"""
import json
import sys

import duckdb

from build_routing_tables import wilson_lb
from orchestrator import (
    ALL_PSPS,
    PSP_FEES,
    Config,
    amount_band,
    issuer_bucket,
)

PARQUET = "attempts.parquet"
MIN_SUPPORT = 200
COST_BIASES = [0.0, 0.5, 1.0]
CONFIG = Config()  # same channel-exclusion policy the engine would run in production

DIMS_BY_LEVEL = {
    0: ["gateway_group", "funding", "issuer_bucket", "amount_band"],
    1: ["gateway_group", "funding", "issuer_bucket"],
    2: ["gateway_group", "funding"],
    3: ["gateway_group"],
}


def route(evals, cost_bias):
    """Same selection rule as orchestrator.decide(): tolerance=cost_bias*10pp of best
    approval, then cheapest fee among survivors. evals: {psp: (n, p_hat, wilson_lb)}."""
    best_p = max(v[2] for v in evals.values())
    tolerance = cost_bias * 0.10
    survivors = [p for p, v in evals.items() if v[2] >= best_p - tolerance]
    return min(survivors, key=lambda p: PSP_FEES[p])


def load_split(day_lo, day_hi):
    con = duckdb.connect()
    df = con.execute(
        f"""
        SELECT gateway_group, funding, issuer, amount, psp, approved
        FROM read_parquet('{PARQUET}')
        WHERE retry_index=1 AND day BETWEEN {day_lo} AND {day_hi}
        """
    ).fetchdf()
    df["funding"] = df["funding"].fillna("unknown")
    df["issuer_bucket"] = df["issuer"].map(issuer_bucket)
    df["amount_band"] = df["amount"].map(amount_band)
    return df


def build_train_lookup(train):
    lookup = {}
    for level, dims in DIMS_BY_LEVEL.items():
        g = train.groupby(dims + ["psp"], dropna=False)["approved"].agg(["sum", "count"]).reset_index()
        for row in g.itertuples():
            approvals, n = row.sum, row.count
            key_dims = tuple(getattr(row, d) for d in dims)
            key = (level,) + key_dims + (row.psp,)
            lookup[key] = (n, approvals / n, wilson_lb(approvals, n))
    return lookup


def resolve_segment(lookup, gateway_group, funding, issuer_bkt, amt_band, psp, min_support):
    keys_by_level = {
        0: (gateway_group, funding, issuer_bkt, amt_band),
        1: (gateway_group, funding, issuer_bkt),
        2: (gateway_group, funding),
        3: (gateway_group,),
    }
    for level in range(4):
        key = (level,) + keys_by_level[level] + (psp,)
        hit = lookup.get(key)
        if hit and hit[0] >= min_support:
            return hit  # (n, p_hat, wilson_lb)
    key = (3, gateway_group, psp)
    return lookup.get(key)  # may be None -- unseen gateway_group in train


def eligible_pool(gateway_group):
    excluded = set(CONFIG.excluded_psps_by_channel.get(gateway_group, ()))
    return [p for p in ALL_PSPS if p not in excluded]


CAVEATS = [
    "Correlation != causation: historical routing was not randomized, so a segment's higher "
    "approval rate for a PSP may partly reflect which transactions were already sent there.",
    "No capacity constraints modeled: this assumes every PSP could absorb 100% of reallocated "
    "volume at the same approval rate.",
    "'Expected approval' is the TRAIN-period Wilson-LB rate applied to TEST volume, not a live "
    "A/B result.",
    "Wilson-LB is conservative, so the expected number is understated for thick cells and "
    "deliberately pessimistic for thin ones.",
]


def main(as_json=False):
    def log(*a):
        if not as_json:
            print(*a)

    log("Loading TRAIN (days 1-21) and TEST (days 22-31) splits...")
    train = load_split(1, 21)
    test = load_split(22, 31)
    log(f"TRAIN: n={len(train)}, actual approval={train['approved'].mean():.4f}")
    log(f"TEST:  n={len(test)}, actual approval={test['approved'].mean():.4f}")

    lookup = build_train_lookup(train)

    actual_approved = 0
    actual_fee_paid = 0.0
    n = len(test)
    results = {cb: {"psp_counts": {}, "sum_p_wilson": 0.0, "sum_expected_fee": 0.0, "n_unrouted": 0} for cb in COST_BIASES}

    for row in test.itertuples():
        if row.approved:
            actual_approved += 1
            actual_fee_paid += PSP_FEES.get(row.psp, 0.0) * row.amount

        evals = {}
        for psp in eligible_pool(row.gateway_group):
            hit = resolve_segment(lookup, row.gateway_group, row.funding, row.issuer_bucket, row.amount_band, psp, MIN_SUPPORT)
            if hit is not None:
                evals[psp] = hit  # (n, p_hat, wilson_lb)

        if not evals:
            for cb in COST_BIASES:
                results[cb]["n_unrouted"] += 1
            continue

        for cb in COST_BIASES:
            chosen = route(evals, cb)
            p_w = evals[chosen][2]
            r = results[cb]
            r["psp_counts"][chosen] = r["psp_counts"].get(chosen, 0) + 1
            r["sum_p_wilson"] += p_w
            r["sum_expected_fee"] += p_w * PSP_FEES[chosen] * row.amount

    actual_rate = actual_approved / n
    log("\n=== Backtest results (out-of-sample, days 22-31) ===")
    log(f"n test txns: {n}")
    log(f"ACTUAL approval rate: {actual_rate:.4f} ({actual_approved}/{n})")
    log(f"ACTUAL total fee paid on approved txns: {actual_fee_paid:,.0f} units")

    by_cost_bias = []
    for cb in COST_BIASES:
        r = results[cb]
        routed_n = n - r["n_unrouted"]
        exp_appr = r["sum_p_wilson"] / routed_n if routed_n else float("nan")
        by_cost_bias.append({
            "cost_bias": cb,
            "unrouted": r["n_unrouted"],
            "expected_approval": round(exp_appr, 4),
            "lift_pp": round((exp_appr - actual_rate) * 100, 2),
            "expected_fee": round(r["sum_expected_fee"], 0),
            "fee_delta": round(r["sum_expected_fee"] - actual_fee_paid, 0),
            "psp_counts": r["psp_counts"],
        })
        log(f"\n--- cost_bias={cb} ---")
        log(f"  unrouted (no train data at any level): {r['n_unrouted']}")
        log(f"  EXPECTED approval under orchestrator routing: {exp_appr:.4f} "
            f"(lift vs actual: {(exp_appr - actual_rate) * 100:+.2f}pp)")
        log(f"  EXPECTED total fee (p_wilson-weighted): {r['sum_expected_fee']:,.0f} units "
            f"(delta vs actual fee paid: {r['sum_expected_fee'] - actual_fee_paid:+,.0f})")
        log(f"  PSP distribution under routing: {r['psp_counts']}")

    headline = by_cost_bias[0]["lift_pp"]
    log(f"\nHEADLINE (cost_bias=0): expected approval lift vs actual routing = {headline:+.2f}pp")
    log("\n=== Caveats ===")
    for c in CAVEATS:
        log(f"- {c}")

    summary = {
        "test_days": "22-31",
        "n_test_txns": n,
        "actual_approval_rate": round(actual_rate, 4),
        "actual_fee_paid": round(actual_fee_paid, 0),
        "headline_lift_pp": headline,
        "by_cost_bias": by_cost_bias,
        "caveats": CAVEATS,
    }
    if as_json:
        print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main(as_json="--json" in sys.argv)
