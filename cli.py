#!/usr/bin/env python
"""CLI for the routing/retry decision engine.

    .venv/bin/python cli.py --amount 450 --bin 412345 --gateway recurring --cost-bias 0 \\
        [--down psp-b psp-d] [--funding debit] [--issuer "Issuer-03"] \\
        [--attempt 2 --last-error insufficient_funds --last-psp psp-a]

    .venv/bin/python cli.py --txn-file demo_transactions.json --index 0

Channels: checkout (user-present, card-not-present), recurring
(merchant-initiated, off-session, scheduled), pos (card-present terminal).
"""
import argparse
import json

from orchestrator import Config, decide


def pretty_print(txn, decision, why=None):
    print("=" * 78)
    print(f"txn: {json.dumps(txn)}")
    if why:
        print(f"why_interesting: {why}")
    print("-" * 78)
    if "error" in decision:
        print(f"ERROR: {decision['error']}")
        return
    print(f"ROUTE -> {decision['route_psp']}" + ("  [STATIC DEFAULT]" if decision["static_default"] else ""))
    print("\neligible PSPs:")
    print(f"  {'psp':<8}{'p_wilson':>10}{'p_hat':>10}{'fee%':>8}{'expected_net':>14}  segment (level)")
    for psp, e in sorted(decision["eligible_psps"].items(), key=lambda kv: -kv[1]["expected_net"]):
        flag = " *thin*" if e["insufficient_data"] else ""
        print(
            f"  {psp:<8}{e['p_wilson']:>10.4f}{e['p_hat']:>10.4f}{e['fee_pct']*100:>7.2f}%"
            f"{e['expected_net']:>14.2f}  {e['segment_used']} n={e['n_support']}{flag}"
        )
    rp = decision["retry_policy"]
    print("\nretry plan:")
    print(f"  should_retry_on_fail: {rp['should_retry_on_fail']}")
    if rp["stop_reason"]:
        print(f"  stop_reason: {rp['stop_reason']}")
    else:
        print(f"  next_psp_candidates: {rp['next_psp_candidates']}")
        print(f"  when: {rp['when']}")
        if rp.get("note"):
            print(f"  note: {rp['note']}")
    print("\nreasoning:")
    for line in decision["reasoning"]:
        print(f"  - {line}")


def run(txn, cost_bias, down, min_support, max_up, max_off, excluded=None):
    config = Config(
        cost_bias=cost_bias,
        psps_down=frozenset(down or []),
        min_support=min_support,
        max_attempts_user_present=max_up,
        max_attempts_off_session=max_off,
        excluded_psps_by_channel={k: set(v) for k, v in (excluded or {}).items()},
    )
    try:
        return decide(txn, config)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--amount", type=float)
    ap.add_argument("--bin", dest="bin6", help="6-digit card prefix; resolves to an issuer via the bin6 map")
    ap.add_argument("--issuer")
    ap.add_argument("--funding", default="debit", choices=["credit", "debit", "prepaid", "unknown"])
    ap.add_argument("--gateway", default="checkout", choices=["checkout", "recurring", "pos"],
                    help="checkout=user-present CNP, recurring=off-session scheduled, pos=card-present")
    ap.add_argument("--cost-bias", type=float, default=0.0,
                    help="0..1; pp of approval you'll trade for a cheaper PSP (tolerance = cost_bias * 10pp)")
    ap.add_argument("--down", nargs="*", default=[], help="PSPs to mark DOWN for this decision")
    ap.add_argument("--min-support", type=int, default=200)
    ap.add_argument("--max-attempts-user-present", type=int, default=8)
    ap.add_argument("--max-attempts-off-session", type=int, default=20)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--last-error")
    ap.add_argument("--last-psp")
    ap.add_argument("--txn-file")
    ap.add_argument("--index", type=int, default=None, help="only run this one index from --txn-file")
    args = ap.parse_args()

    if args.txn_file:
        with open(args.txn_file) as f:
            items = json.load(f)
        if isinstance(items, dict):
            items = [items]
        if args.index is not None:
            items = [items[args.index]]
        for item in items:
            txn = item.get("txn", item)
            decision = run(txn, item.get("cost_bias", args.cost_bias), item.get("down", args.down),
                           args.min_support, args.max_attempts_user_present, args.max_attempts_off_session,
                           item.get("excluded_psps_by_channel"))
            pretty_print(txn, decision, item.get("why_interesting"))
        return

    txn = {
        "amount": args.amount,
        "bin6": args.bin6,
        "issuer": args.issuer,
        "funding": args.funding,
        "gateway": args.gateway,
        "attempt_number": args.attempt,
    }
    if args.last_error:
        txn["error_history"] = [{"psp": args.last_psp, "error_class": args.last_error}]

    decision = run(txn, args.cost_bias, args.down, args.min_support,
                   args.max_attempts_user_present, args.max_attempts_off_session)
    pretty_print(txn, decision)


if __name__ == "__main__":
    main()
