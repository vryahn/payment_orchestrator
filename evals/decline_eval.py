#!/usr/bin/env python
"""Eval harness for the decline normalizer -- the LLM edge is the only part of
this system that can be wrong in a way tests cannot catch, so it gets measured
instead of trusted.

    python evals/decline_eval.py                  # score, compare to baseline
    python evals/decline_eval.py --write-baseline # record the current score

Two modes, because the two routes have different failure surfaces:
  table_only  no API key present. Every off-table case falls back to
              generic_decline. This is the floor: what the system scores with
              the LLM edge switched off entirely.
  llm         a key is present, so table misses go to the model. Compared
              against its OWN baseline, never against table_only.

Hallucination count must be 0: any class outside orchestrator.ERROR_CLASSES
would reach the retry state machine as an unrecognized string. The normalizer
gates that; this asserts the gate still holds.

Exits non-zero if accuracy drops more than 2pp below the matching baseline.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decline_normalizer import normalize  # noqa: E402
from orchestrator import ERROR_CLASSES  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(_HERE, "golden_declines.json")
BASELINE = os.path.join(_HERE, "baseline.json")
MAX_DROP_PP = 2.0
BASELINE_NOTE = "run with GEMINI_KEY set and --write-baseline"


def current_mode():
    keys = ("GEMINI_KEY", "VITE_GEMINI_KEY", "MISTRAL_KEY", "VITE_MISTRAL_KEY")
    return "llm" if any(os.environ.get(k) for k in keys) else "table_only"


def _remote_normalize(base_url):
    """Call the deployed /api/normalize instead of the local function (keys stay on the server)."""
    import urllib.request
    from decline_normalizer import Normalized

    def _call(psp, raw_code, raw_message):
        body = json.dumps({"psp": psp, "raw_code": raw_code, "raw_message": raw_message}).encode()
        req = urllib.request.Request(base_url.rstrip("/") + "/api/normalize", data=body,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        return Normalized(d["error_class"], d["confidence"], d["source"], d.get("provider"), d.get("reasoning", ""))
    return _call


def run(cases, normalize=normalize):
    rows, hallucinations = [], []
    for c in cases:
        got = normalize(c["psp"], c.get("raw_code"), c.get("raw_message"))
        if got.error_class not in ERROR_CLASSES:
            hallucinations.append((c, got.error_class))
        rows.append({
            "psp": c["psp"],
            "raw": c.get("raw_code") or c.get("raw_message") or "",
            "expected": c["expected_class"],
            "got": got.error_class,
            "source": got.source,
            "confidence": got.confidence,
            "ok": got.error_class == c["expected_class"],
        })
    return rows, hallucinations


def summarize(rows):
    by_source = {}
    for r in rows:
        s = by_source.setdefault(r["source"], {"n": 0, "correct": 0})
        s["n"] += 1
        s["correct"] += r["ok"]
    for s in by_source.values():
        s["accuracy"] = round(s["correct"] / s["n"], 4)

    confusion = {}
    for r in rows:
        confusion.setdefault(r["expected"], {}).setdefault(r["got"], 0)
        confusion[r["expected"]][r["got"]] += 1

    correct = sum(r["ok"] for r in rows)
    return {
        "n": len(rows),
        "correct": correct,
        "accuracy": round(correct / len(rows), 4),
        "by_source": by_source,
        "confusion": confusion,
    }


def print_report(mode, rows, summary, hallucinations):
    print(f"mode: {mode}   cases: {summary['n']}\n")
    print(f"  {'psp':<7}{'raw':<52}{'expected':<20}{'got':<20}{'source':<10}ok")
    for r in rows:
        raw = r["raw"] if len(r["raw"]) <= 50 else r["raw"][:47] + "..."
        print(f"  {r['psp']:<7}{raw:<52}{r['expected']:<20}{r['got']:<20}"
              f"{r['source']:<10}{'.' if r['ok'] else 'X'}")

    print(f"\naccuracy: {summary['accuracy']:.1%} ({summary['correct']}/{summary['n']})")
    for src, s in sorted(summary["by_source"].items()):
        print(f"  by source: {src:<10} {s['accuracy']:.1%} ({s['correct']}/{s['n']})")

    print("\nconfusion (expected -> got):")
    for exp in sorted(summary["confusion"]):
        got = summary["confusion"][exp]
        detail = ", ".join(f"{k}={v}" for k, v in sorted(got.items(), key=lambda kv: -kv[1]))
        print(f"  {exp:<20} {detail}")

    print(f"\nhallucinations (class outside the enum): {len(hallucinations)}")


def load_baseline():
    if not os.path.exists(BASELINE):
        return {"table_only": None, "llm": None}
    with open(BASELINE) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON too")
    ap.add_argument("--remote", metavar="BASE_URL",
                    help="evaluate the deployed API (e.g. https://orchestrator.vryahn.com); mode is 'llm'")
    args = ap.parse_args()

    with open(GOLDEN) as f:
        cases = json.load(f)

    if args.remote:
        mode = "llm"
        rows, hallucinations = run(cases, _remote_normalize(args.remote))
    else:
        mode = current_mode()
        rows, hallucinations = run(cases)
    summary = summarize(rows)
    print_report(mode, rows, summary, hallucinations)

    assert not hallucinations, f"normalizer emitted classes outside the enum: {hallucinations}"

    if args.json:
        print("\n" + json.dumps(summary, indent=2))

    baseline = load_baseline()
    if args.write_baseline:
        baseline[mode] = summary
        if baseline.get("llm") is None:
            baseline["llm"] = None
            baseline["note"] = BASELINE_NOTE
        with open(BASELINE, "w") as f:
            json.dump(baseline, f, indent=2)
        print(f"\nwrote {BASELINE} ['{mode}']")
        return

    ref = baseline.get(mode)
    if not ref:
        print(f"\nno '{mode}' baseline recorded yet -- {BASELINE_NOTE if mode == 'llm' else 'run with --write-baseline'}")
        return
    drop = (ref["accuracy"] - summary["accuracy"]) * 100
    print(f"\nbaseline['{mode}'] accuracy: {ref['accuracy']:.1%}  delta: {-drop:+.2f}pp")
    if drop > MAX_DROP_PP:
        print(f"REGRESSION: accuracy fell {drop:.2f}pp (> {MAX_DROP_PP}pp allowed)")
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
