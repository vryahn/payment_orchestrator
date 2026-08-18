"""Synthetic authorization-attempt generator -> attempts.parquet.

`attempts.parquet` is the only raw input the rest of the pipeline needs
(`build_routing_tables.py` and `backtest.py` read it; the engine never does).
Everything below is drawn from a fixed seed, so the whole project reproduces
from an empty folder.

Schema written (one row per authorization attempt):
    gateway_group  str   checkout | recurring | pos
    funding        str   credit | debit | prepaid
    issuer         str   Issuer-01 .. Issuer-15
    bin6           str   6-digit card prefix, pinned to one issuer + funding
    amount         float ~20-900 generic units, log-normal
    psp            str   psp-a | psp-b | psp-c | psp-d
    approved       int   1 = authorized, 0 = declined
    error_class    str   '' when approved, else the decline reason
    day            int   1..31
    retry_index    int   1 = first attempt, 2+ = retry in the same chain

Generative parameters (all tunable right below):

  FIRST_WAVE_ROWS   first attempts; retries push the total to ~300k.
  CHANNEL_MIX       checkout = user-present / card-not-present,
                    recurring = merchant-initiated, off-session, scheduled,
                    pos = card-present terminal.
  PSP_FEES          fee charged on the approved amount. psp-d is the cheap one
                    AND the weakest approver, so `cost_bias` has a real
                    trade-off to make instead of a dominated option.
  PSP_ROUTING_MIX   how historical traffic was split. Deliberately uneven so
                    psp-d's cells stay thin and the min_support / hierarchy
                    fallback path is actually exercised.
  Approval model    additive log-odds, then sigmoid -> Bernoulli:
                    INTERCEPT + PSP_BASE + PSP x channel + PSP x funding
                    + per-(issuer, PSP) effect + channel base
                    + AMOUNT_SLOPE * (amount - AMOUNT_PIVOT)
                    + RETRY_PENALTY on retries.
                    Designed so psp-a leads on credit/checkout, psp-b on debit
                    and pos, psp-c on the recurring channel, psp-d nowhere.
  ERROR_MIX         error_class distribution per channel, given a decline.
  RETRY_PROB        a decline with a retryable error_class spawns a follow-up
                    attempt, sometimes on a different PSP; off-session retries
                    land a few days later (next billing window).
  BIN_TABLE         ~60 bin6 codes, each pinned to exactly one issuer and one
                    funding type, so bin6 -> issuer/funding is self-consistent.
"""
import numpy as np
import pandas as pd

SEED = 42
FIRST_WAVE_ROWS = 260_000
MAX_RETRY_INDEX = 4
DAYS = 31

CHANNELS = ["checkout", "recurring", "pos"]
CHANNEL_MIX = [0.55, 0.30, 0.15]
FUNDINGS = ["credit", "debit", "prepaid"]
FUNDING_MIX = [0.38, 0.47, 0.15]

PSPS = ["psp-a", "psp-b", "psp-c", "psp-d"]
PSP_FEES = {"psp-a": 0.029, "psp-b": 0.031, "psp-c": 0.030, "psp-d": 0.027}
PSP_ROUTING_MIX = [0.40, 0.28, 0.20, 0.12]

N_ISSUERS = 15
ISSUERS = [f"Issuer-{i:02d}" for i in range(1, N_ISSUERS + 1)]
ISSUER_DECAY = 0.85  # volume share decays geometrically down the issuer list

INTERCEPT = 0.55
PSP_BASE = {"psp-a": 0.10, "psp-b": 0.00, "psp-c": -0.05, "psp-d": -0.45}
PSP_X_CHANNEL = {
    "psp-a": {"checkout": 0.40, "recurring": -0.20, "pos": 0.00},
    "psp-b": {"checkout": -0.05, "recurring": 0.00, "pos": 0.35},
    "psp-c": {"checkout": -0.15, "recurring": 0.50, "pos": -0.05},
    "psp-d": {"checkout": 0.00, "recurring": 0.00, "pos": 0.00},
}
PSP_X_FUNDING = {
    "psp-a": {"credit": 0.35, "debit": -0.10, "prepaid": -0.25},
    "psp-b": {"credit": -0.15, "debit": 0.35, "prepaid": 0.00},
    "psp-c": {"credit": 0.00, "debit": 0.00, "prepaid": 0.20},
    "psp-d": {"credit": 0.00, "debit": 0.00, "prepaid": 0.00},
}
CHANNEL_BASE = {"checkout": 0.45, "recurring": -0.55, "pos": 0.70}
ISSUER_EFFECT_SD = 0.35

AMOUNT_LOG_MEAN, AMOUNT_LOG_SD = 5.2, 0.70
AMOUNT_MIN, AMOUNT_MAX = 20.0, 900.0
AMOUNT_PIVOT = 180.0
AMOUNT_SLOPE = -0.0011
RETRY_PENALTY = -0.55

ERROR_CLASSES = [
    "insufficient_funds",
    "generic_decline",
    "bank_auth_required",
    "invalid_card_info",
    "fraud_risk",
    "other",
]
ERROR_MIX = {
    "checkout":  [0.35, 0.28, 0.12, 0.12, 0.06, 0.07],
    "recurring": [0.50, 0.25, 0.10, 0.07, 0.03, 0.05],
    "pos":       [0.30, 0.35, 0.05, 0.15, 0.07, 0.08],
}
RETRYABLE = {"insufficient_funds", "generic_decline", "bank_auth_required", "other"}
RETRY_PROB = 0.42
RETRY_SAME_PSP_PROB = 0.50


def build_bin_table(rng):
    """~60 bin6 codes. Each code belongs to exactly one issuer and one funding
    type, and carries the draw weight it contributes to the overall mix."""
    rows, seen = [], set()
    issuer_w = ISSUER_DECAY ** np.arange(N_ISSUERS)
    issuer_w = issuer_w / issuer_w.sum()
    for i, issuer in enumerate(ISSUERS):
        k = int(rng.integers(3, 6))
        for _ in range(k):
            while True:
                bin6 = f"{rng.integers(400000, 599999)}"
                if bin6 not in seen:
                    seen.add(bin6)
                    break
            funding = FUNDINGS[int(rng.choice(len(FUNDINGS), p=FUNDING_MIX))]
            rows.append((bin6, issuer, i, funding, issuer_w[i] / k))
    return rows


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def logodds(ch, fu, iss_idx, psp_idx, amount, retry_index, issuer_effect):
    z = np.full(len(amount), INTERCEPT)
    for j, psp in enumerate(PSPS):
        m = psp_idx == j
        if not m.any():
            continue
        z[m] += PSP_BASE[psp]
        for c, chan in enumerate(CHANNELS):
            mc = m & (ch == c)
            z[mc] += PSP_X_CHANNEL[psp][chan] + CHANNEL_BASE[chan]
        for f, fund in enumerate(FUNDINGS):
            z[m & (fu == f)] += PSP_X_FUNDING[psp][fund]
    z += issuer_effect[iss_idx, psp_idx]
    z += AMOUNT_SLOPE * (amount - AMOUNT_PIVOT)
    z += RETRY_PENALTY * (retry_index > 1)
    return z


def draw_outcomes(rng, ch, fu, iss_idx, psp_idx, amount, retry_index, issuer_effect):
    p = sigmoid(logodds(ch, fu, iss_idx, psp_idx, amount, retry_index, issuer_effect))
    approved = (rng.random(len(p)) < p).astype(np.int8)
    err = np.empty(len(p), dtype=object)
    err[:] = ""
    for c, chan in enumerate(CHANNELS):
        m = (approved == 0) & (ch == c)
        n = int(m.sum())
        if n:
            err[m] = rng.choice(ERROR_CLASSES, size=n, p=ERROR_MIX[chan])
    return approved, err


def main():
    rng = np.random.default_rng(SEED)
    bin_table = build_bin_table(rng)
    bin6s = np.array([r[0] for r in bin_table])
    bin_issuer_idx = np.array([r[2] for r in bin_table])
    bin_funding_idx = np.array([FUNDINGS.index(r[3]) for r in bin_table])
    bin_p = np.array([r[4] for r in bin_table])
    bin_p = bin_p / bin_p.sum()

    issuer_effect = rng.normal(0.0, ISSUER_EFFECT_SD, size=(N_ISSUERS, len(PSPS)))

    n = FIRST_WAVE_ROWS
    b = rng.choice(len(bin_table), size=n, p=bin_p)
    ch = rng.choice(len(CHANNELS), size=n, p=CHANNEL_MIX)
    psp_idx = rng.choice(len(PSPS), size=n, p=PSP_ROUTING_MIX)
    amount = np.clip(
        rng.lognormal(AMOUNT_LOG_MEAN, AMOUNT_LOG_SD, size=n), AMOUNT_MIN, AMOUNT_MAX
    ).round(2)
    day = rng.integers(1, DAYS + 1, size=n)
    retry_index = np.ones(n, dtype=np.int16)

    waves = []
    while True:
        iss_idx, fu = bin_issuer_idx[b], bin_funding_idx[b]
        approved, err = draw_outcomes(rng, ch, fu, iss_idx, psp_idx, amount, retry_index, issuer_effect)
        waves.append(
            pd.DataFrame(
                {
                    "gateway_group": np.array(CHANNELS)[ch],
                    "funding": np.array(FUNDINGS)[fu],
                    "issuer": np.array(ISSUERS)[iss_idx],
                    "bin6": bin6s[b],
                    "amount": amount,
                    "psp": np.array(PSPS)[psp_idx],
                    "approved": approved,
                    "error_class": err.astype(str),
                    "day": day.astype(np.int16),
                    "retry_index": retry_index,
                }
            )
        )
        if retry_index[0] >= MAX_RETRY_INDEX:
            break

        retryable = np.isin(err.astype(str), list(RETRYABLE))
        m = (approved == 0) & retryable & (rng.random(len(err)) < RETRY_PROB)
        if not m.any():
            break

        b, ch, amount = b[m], ch[m], amount[m]
        old_psp = psp_idx[m]
        keep = rng.random(len(old_psp)) < RETRY_SAME_PSP_PROB
        alt = rng.choice(len(PSPS), size=len(old_psp), p=PSP_ROUTING_MIX)
        alt = np.where(alt == old_psp, (alt + 1) % len(PSPS), alt)
        psp_idx = np.where(keep, old_psp, alt)
        # off-session retries wait for the next billing window; user-present retries are immediate
        gap = np.where(ch == CHANNELS.index("recurring"), rng.integers(2, 5, size=len(old_psp)), 0)
        day = np.minimum(day[m] + gap, DAYS)
        retry_index = retry_index[m] + 1

    out = pd.concat(waves, ignore_index=True)
    out.to_parquet("attempts.parquet", index=False, compression="zstd")

    print(f"wrote attempts.parquet: {len(out):,} rows, {len(bin_table)} bin6 codes, {N_ISSUERS} issuers")
    print(f"overall approval: {out['approved'].mean():.4f}")
    print("approval by channel x psp:")
    piv = out.pivot_table(index="gateway_group", columns="psp", values="approved", aggfunc="mean")
    print(piv.round(4).to_string())
    print("\nattempts by psp:")
    print(out["psp"].value_counts().to_string())
    print(f"\nretry chains: {(out['retry_index'] > 1).sum():,} follow-up attempts")


if __name__ == "__main__":
    main()
