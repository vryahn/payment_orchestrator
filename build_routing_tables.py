"""Materialize empirical approval-probability tables by segment.

Hierarchy (gateway_group is FIRST-CLASS, never dropped):
  L0 gateway_group x funding x issuer_bucket x amount_band x psp   (finest)
  L1 gateway_group x funding x issuer_bucket x psp
  L2 gateway_group x funding x psp
  L3 gateway_group x psp                                            (coarsest)

Writes routing_tables.parquet (long form, one row per level/segment/psp),
routing_tables.json (same rows, so the serverless engine loads with stdlib
`json` alone -- no pandas/duckdb/pyarrow in the hot path) and routing_meta.json
(amount-band edges, top-issuer whitelist, bin6 -> issuer map, PSP fee table,
min_support default, PSP volume order) so orchestrator.py never has to touch
the raw parquet.

This is the offline half of the project: heavy deps live here, the runtime is
stdlib. The JSON tables are committed; the parquet is regenerable.
"""
import json
import math

import duckdb
import pandas as pd

from synth_attempts import PSP_FEES

PARQUET = "attempts.parquet"
MIN_SUPPORT_DEFAULT = 200
TOP_ISSUERS_LIMIT = 12  # everything outside the whitelist buckets to 'OTHER'
Z = 1.959963985  # 95% two-sided normal quantile


def make_amount_band(edges):
    def amount_band(a: float) -> str:
        if a <= edges[0]:
            return "Q1_low"
        if a <= edges[1]:
            return "Q2_mid_low"
        if a <= edges[2]:
            return "Q3_mid_high"
        return "Q4_high"

    return amount_band


def wilson_lb(approvals: int, n: int, z: float = Z) -> float:
    if n == 0:
        return 0.0
    p = approvals / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def main():
    con = duckdb.connect()

    # Amount bands are quartiles of the observed amount distribution, computed
    # here and frozen into routing_meta.json so the engine and the backtest use
    # the same cutoffs.
    edges = con.execute(
        f"SELECT quantile_cont(amount, [0.25, 0.5, 0.75]) FROM read_parquet('{PARQUET}')"
    ).fetchone()[0]
    edges = [round(float(e), 2) for e in edges]
    amount_band = make_amount_band(edges)
    print(f"amount-band edges (quartiles): {edges}")

    top_issuers = (
        con.execute(
            f"""
            SELECT issuer FROM read_parquet('{PARQUET}')
            WHERE issuer IS NOT NULL
            GROUP BY 1 ORDER BY count(*) DESC LIMIT {TOP_ISSUERS_LIMIT}
            """
        )
        .fetchdf()["issuer"]
        .tolist()
    )
    print(f"top issuers ({len(top_issuers)}): {top_issuers}")

    # bin6 -> issuer map (modal issuer per bin6), so a transaction that only
    # carries a card prefix still resolves to an issuer.
    bin6_map_df = con.execute(
        f"""
        SELECT bin6, issuer FROM (
            SELECT bin6, issuer, count(*) n,
                   row_number() OVER (PARTITION BY bin6 ORDER BY count(*) DESC) rk
            FROM read_parquet('{PARQUET}')
            WHERE bin6 IS NOT NULL AND issuer IS NOT NULL
            GROUP BY bin6, issuer
        ) WHERE rk=1
        """
    ).fetchdf()
    bin6_to_issuer = dict(zip(bin6_map_df["bin6"], bin6_map_df["issuer"]))
    print(f"bin6->issuer map: {len(bin6_to_issuer)} entries")

    df = con.execute(
        f"SELECT gateway_group, funding, issuer, amount, psp, approved, error_class "
        f"FROM read_parquet('{PARQUET}')"
    ).fetchdf()

    # The exact set the retry state machine keys on, derived from the data
    # rather than restated in the engine. decline_normalizer.py normalizes every
    # PSP dialect onto this enum and nothing else.
    error_classes = sorted(c for c in df["error_class"].unique().tolist() if c)

    df["funding"] = df["funding"].fillna("unknown")
    df["issuer_bucket"] = df["issuer"].where(df["issuer"].isin(top_issuers), "OTHER")
    df["amount_band"] = df["amount"].map(amount_band)

    dims_by_level = {
        0: ["gateway_group", "funding", "issuer_bucket", "amount_band", "psp"],
        1: ["gateway_group", "funding", "issuer_bucket", "psp"],
        2: ["gateway_group", "funding", "psp"],
        3: ["gateway_group", "psp"],
    }
    all_dims = ["gateway_group", "funding", "issuer_bucket", "amount_band", "psp"]

    rows = []
    for level, dims in dims_by_level.items():
        g = df.groupby(dims, dropna=False)["approved"].agg(["sum", "count"]).reset_index()
        g = g.rename(columns={"sum": "approvals", "count": "n"})
        for dim in all_dims:
            if dim not in g.columns:
                g[dim] = "ALL"
        g["level"] = level
        g["p_hat"] = g["approvals"] / g["n"]
        g["wilson_lb"] = [wilson_lb(a, n) for a, n in zip(g["approvals"], g["n"])]
        rows.append(g[["level"] + all_dims + ["n", "approvals", "p_hat", "wilson_lb"]])

    out = pd.concat(rows, ignore_index=True)
    out.to_parquet("routing_tables.parquet", index=False)
    # same rows as JSON: the deployed engine reads this one with stdlib json.
    out.to_json("routing_tables.json", orient="records")

    meta = {
        "amount_band_edges": edges,
        "top_issuers": top_issuers,
        "bin6_to_issuer": bin6_to_issuer,
        "psp_fees": PSP_FEES,
        "psp_volume_order": df["psp"].value_counts().index.tolist(),
        "min_support_default": MIN_SUPPORT_DEFAULT,
        "hierarchy_levels": {str(k): v for k, v in dims_by_level.items()},
        "gateway_groups": sorted(df["gateway_group"].dropna().unique().tolist()),
        "funding_values": sorted(df["funding"].unique().tolist()),
        "error_classes": error_classes,
    }
    with open("routing_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print("\n=== summary ===")
    for level in sorted(dims_by_level):
        sub = rows[level]
        n_cells = len(sub)
        n_thin = int((sub["n"] < MIN_SUPPORT_DEFAULT).sum())
        print(
            f"L{level} ({'/'.join(dims_by_level[level])}): "
            f"{n_cells} cells, {n_thin} below min_support={MIN_SUPPORT_DEFAULT} "
            f"({n_thin/n_cells:.1%})"
        )
    print(f"\nwrote routing_tables.parquet + routing_tables.json ({sum(len(r) for r in rows)} rows) "
          f"and routing_meta.json")


if __name__ == "__main__":
    main()
