"""Assert-based fast checks for orchestrator.py. No framework -- run directly:
    .venv/bin/python tests.py
"""
import json
import sys

from orchestrator import ALL_PSPS, Config, decide

FAILS = []
RUN = []

BASE = {"amount": 250, "bin6": "596546", "funding": "debit", "gateway": "checkout", "attempt_number": 1}


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    RUN.append(name)
    if not cond:
        FAILS.append(name)


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def main():
    with open("demo_transactions.json") as f:
        demos = json.load(f)

    # 1. every demo transaction yields a well-formed decision
    for item in demos:
        cfg = Config(cost_bias=item.get("cost_bias", 0.0), psps_down=frozenset(item.get("down", [])))
        d = decide(item["txn"], cfg)
        ok = (
            d["route_psp"] in ALL_PSPS
            and d["route_psp"] not in item.get("down", [])
            and isinstance(d["eligible_psps"], dict)
            and len(d["eligible_psps"]) > 0
            and set(d["eligible_psps"]) <= set(ALL_PSPS)
            and "retry_policy" in d
            and isinstance(d["reasoning"], list)
            and len(d["reasoning"]) > 0
            and set(d["segment_inputs"]) == {"gateway_group", "funding", "issuer", "issuer_bucket", "amount_band"}
        )
        check(f"demo txn valid decision: {item['txn']}", ok)
    check("demo file has 8 transactions", len(demos) == 8)

    # 2. scoring rule: cost_bias=0 is pure approval / max expected_net;
    #    cost_bias=1 is cheapest PSP within 10pp of the best approval.
    d0 = decide(BASE, Config(cost_bias=0.0))
    e0 = d0["eligible_psps"]
    check("cost_bias=0 picks max-p_wilson PSP", d0["route_psp"] == max(e0, key=lambda p: e0[p]["p_wilson"]))
    check("cost_bias=0 pick matches max expected_net PSP", d0["route_psp"] == max(e0, key=lambda p: e0[p]["expected_net"]))

    d1 = decide(BASE, Config(cost_bias=1.0))
    e1 = d1["eligible_psps"]
    best_p = max(v["p_wilson"] for v in e1.values())
    survivors = [p for p, v in e1.items() if v["p_wilson"] >= best_p - 0.10]
    check("cost_bias=1 picks cheapest PSP within 10pp of best approval",
          d1["route_psp"] == min(survivors, key=lambda p: e1[p]["fee_pct"]))
    check("cost_bias is clamped into [0,1]", Config(cost_bias=7.0).cost_bias == 1.0 and Config(cost_bias=-3.0).cost_bias == 0.0)

    # 3. expected_net is p_wilson * amount * (1 - fee)
    net_ok = all(
        abs(v["expected_net"] - v["p_wilson"] * BASE["amount"] * (1 - v["fee_pct"])) < 0.05
        for v in e0.values()
    )
    check("expected_net == p_wilson * amount * (1 - fee_pct)", net_ok)

    # 4. hierarchy: raising min_support can only push segments coarser, never finer
    fine = decide(BASE, Config(min_support=1))
    coarse = decide(BASE, Config(min_support=10**9))
    check("higher min_support never resolves a finer segment",
          all(coarse["eligible_psps"][p]["hierarchy_level"] >= fine["eligible_psps"][p]["hierarchy_level"] for p in fine["eligible_psps"]))
    check("unsatisfiable min_support flags thin data and the static default",
          coarse["static_default"] and all(v["insufficient_data"] for v in coarse["eligible_psps"].values()))

    # 5. operator toggles: psps_down removes a PSP from the pool entirely
    down = decide(BASE, Config(psps_down=frozenset({"psp-a"})))
    check("psps_down removes the PSP from the eligible pool", "psp-a" not in down["eligible_psps"])
    check("all PSPs down raises RuntimeError",
          raises(RuntimeError, lambda: decide(BASE, Config(psps_down=frozenset(ALL_PSPS)))))

    # 6. excluded_psps_by_channel is per-channel and generic (no PSP hardcoded in the engine)
    ex = Config(excluded_psps_by_channel={"recurring": {"psp-c"}})
    rec = decide(dict(BASE, gateway="recurring"), ex)
    chk = decide(BASE, ex)
    check("excluded_psps_by_channel drops the PSP on that channel", "psp-c" not in rec["eligible_psps"])
    check("excluded_psps_by_channel leaves other channels untouched", "psp-c" in chk["eligible_psps"])
    check("channel exclusion is explained in the reasoning trail",
          any("excluded_psps_by_channel" in line for line in rec["reasoning"]))

    # 7. retry state machine, keyed on error_class and channel
    def policy(err, gateway="checkout", psp="psp-a", attempt=2, history=None):
        txn = dict(BASE, gateway=gateway, attempt_number=attempt,
                   error_history=history or [{"psp": psp, "error_class": err}])
        return decide(txn, Config())["retry_policy"]

    check("fraud_risk yields no retry", policy("fraud_risk")["should_retry_on_fail"] is False)
    check("fraud_risk anywhere in history yields no retry",
          policy(None, history=[{"psp": "psp-a", "error_class": "fraud_risk"},
                                {"psp": "psp-b", "error_class": "generic_decline"}])["should_retry_on_fail"] is False)
    check("invalid_card_info yields no retry", policy("invalid_card_info")["should_retry_on_fail"] is False)

    p_if = policy("insufficient_funds", gateway="recurring", psp="psp-b")
    check("insufficient_funds off-session retries the SAME psp", p_if["next_psp_candidates"] == ["psp-b"])
    check("insufficient_funds off-session waits for the next billing window", "next-billing-window" in p_if["when"])
    p_ifu = policy("insufficient_funds", gateway="checkout", psp="psp-b")
    check("insufficient_funds user-present fails over instead of waiting",
          p_ifu["should_retry_on_fail"] and p_ifu["next_psp_candidates"] != ["psp-b"])

    p_auth = policy("bank_auth_required", gateway="recurring")
    check("bank_auth_required off-session stops and reschedules to user-present",
          p_auth["should_retry_on_fail"] is False and p_auth["when"] == "reschedule-to-user-present")
    p_authu = policy("bank_auth_required", gateway="checkout", psp="psp-b")
    check("bank_auth_required user-present retries the same psp with an auth step",
          p_authu["should_retry_on_fail"] and p_authu["next_psp_candidates"] == ["psp-b"])

    p_gen = policy("generic_decline", psp="psp-a")
    check("generic_decline fails over to exactly one other psp",
          len(p_gen["next_psp_candidates"]) == 1 and p_gen["next_psp_candidates"][0] != "psp-a")
    check("two generic declines in a row stop the chain",
          policy(None, history=[{"psp": "psp-a", "error_class": "generic_decline"},
                                {"psp": "psp-b", "error_class": "other"}])["should_retry_on_fail"] is False)
    p_unk = policy("do_not_honor")
    check("unrecognized error_class falls back to the failover policy",
          p_unk["should_retry_on_fail"] and "unrecognized" in p_unk.get("note", ""))

    check("attempt cap stops retries",
          decide(dict(BASE, attempt_number=8), Config())["retry_policy"]["should_retry_on_fail"] is False)
    check("off-session gets a higher attempt cap than user-present",
          decide(dict(BASE, gateway="recurring", attempt_number=8), Config())["retry_policy"]["should_retry_on_fail"] is True)

    # 8. unknown inputs degrade instead of crashing
    unk = decide({"amount": 300, "bin6": "000000", "funding": "credit", "gateway": "pos", "attempt_number": 1}, Config())
    check("unknown BIN routes without crashing", unk["route_psp"] in ALL_PSPS)
    check("unknown BIN buckets the issuer to OTHER", unk["segment_inputs"]["issuer_bucket"] == "OTHER")
    unk_f = decide(dict(BASE, funding="crypto"), Config())
    check("unseen funding value degrades to the 'unknown' bucket", unk_f["segment_inputs"]["funding"] == "unknown")
    check("unseen channel refuses to route rather than guessing",
          raises(RuntimeError, lambda: decide(dict(BASE, gateway="teleport"), Config())))

    # 9. invalid amounts are rejected, not silently routed
    check("non-positive amount raises ValueError", raises(ValueError, lambda: decide(dict(BASE, amount=-50), Config())))
    check("zero amount raises ValueError", raises(ValueError, lambda: decide(dict(BASE, amount=0), Config())))

    print(f"\n{len(RUN)} checks: {len(FAILS)} failed")
    if FAILS:
        print("FAILED:", FAILS)
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
