"""Assert-based checks for the AI edges and the HTTP surface. Kept separate from
tests.py: that file guards the deterministic engine, this one guards everything
wrapped around it. No framework -- run directly:

    .venv/bin/python tests_ai.py

Runs green with no API keys set: the LLM route is exercised only for its
failure behaviour (fallback, enum gate), never for a network call.
"""
import json
import os
import sys

import decline_normalizer as dn
import ops
from decline_normalizer import normalize
from fastapi.testclient import TestClient
from orchestrator import ALL_PSPS, ERROR_CLASSES

from api.index import app

FAILS, RUN = [], []
KEY_VARS = ("GEMINI_KEY", "VITE_GEMINI_KEY", "MISTRAL_KEY", "VITE_MISTRAL_KEY")


def check(name, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    RUN.append(name)
    if not cond:
        FAILS.append(name)


def main():
    client = TestClient(app)

    # 1. deterministic table: one hit per PSP dialect, no network involved
    table_cases = [
        ("psp-a", "51", None, "insufficient_funds", "ISO 8583 numeric"),
        ("psp-b", "authentication_required", None, "bank_auth_required", "Stripe-like decline_code"),
        ("psp-c", "FRAUD-CANCELLED", None, "fraud_risk", "Adyen-like refusalReason"),
        ("psp-d", None, "Insufficient funds", "insufficient_funds", "free bank text"),
    ]
    for psp, code, msg, expected, dialect in table_cases:
        got = normalize(psp, code, msg)
        check(f"table hit ({dialect}): {psp} {code or msg} -> {expected}",
              got.error_class == expected and got.source == "table" and got.confidence == 1.0)

    check("table lookup is case/space insensitive",
          normalize("psp-c", "  not enough BALANCE ").error_class == "insufficient_funds")

    # 2. no key configured -> safe fallback, never a crash and never a guess
    saved = {k: os.environ.pop(k, None) for k in KEY_VARS}
    dn._CACHE.clear()
    try:
        miss = normalize("psp-d", None, "Account holder is deceased per issuer notice")
        check("no key -> fallback to generic_decline",
              miss.error_class == "generic_decline" and miss.source == "fallback")
        check("fallback explains itself", "no LLM key" in miss.reasoning)
        check("no code and no message -> fallback",
              normalize("psp-a").source == "fallback")
        check("unknown PSP does not crash", normalize("psp-zzz", "05").error_class in ERROR_CLASSES)

        # 3. the enum gate: whatever the route, the class is always one the retry
        #    state machine understands.
        with open("evals/golden_declines.json") as f:
            golden = json.load(f)
        classes = {normalize(c["psp"], c.get("raw_code"), c.get("raw_message")).error_class
                   for c in golden}
        check(f"every golden case lands inside the enum ({len(golden)} cases)",
              classes <= set(ERROR_CLASSES))

        # a model returning a class outside the enum must be discarded, not passed on
        dn._CACHE.clear()
        os.environ["GEMINI_KEY"] = "test-key-not-used"
        dn._gemini = lambda key, prompt: ({"error_class": "chargeback_imminent",
                                           "confidence": 0.99, "reasoning": "x"}, "gemini:test")
        gated = normalize("psp-d", None, "some unmapped bank prose")
        check("hallucinated class is discarded, not forwarded",
              gated.error_class == "generic_decline" and gated.source == "fallback"
              and "not a valid error class" in gated.reasoning)

        dn._CACHE.clear()
        dn._gemini = lambda key, prompt: ({"error_class": "fraud_risk",
                                           "confidence": 0.2, "reasoning": "x"}, "gemini:test")
        lowconf = normalize("psp-d", None, "other unmapped bank prose")
        check("low-confidence LLM answer is discarded",
              lowconf.error_class == "generic_decline" and lowconf.source == "fallback")
    finally:
        os.environ.pop("GEMINI_KEY", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
        dn._CACHE.clear()

    # 4. HTTP contract over the 8 demo cases
    cases = client.get("/api/cases").json()
    check("GET /api/cases returns the 8 demo transactions", len(cases) == 8)
    for i, item in enumerate(cases):
        body = dict(item["txn"])
        body["cost_bias"] = item.get("cost_bias", 0)
        r = client.post("/api/decide", json=body)
        d = r.json()
        check(f"POST /api/decide demo case {i}",
              r.status_code == 200
              and d["route_psp"] in ALL_PSPS
              and set(d) >= {"route_psp", "static_default", "eligible_psps", "retry_policy", "reasoning"}
              and all(set(v) >= {"p_wilson", "p_hat", "fee_pct", "expected_net", "segment_used",
                                 "n_support", "insufficient_data"} for v in d["eligible_psps"].values())
              and set(d["retry_policy"]) >= {"should_retry_on_fail", "stop_reason",
                                             "next_psp_candidates", "when"})

    m = client.get("/api/meta").json()
    check("GET /api/meta carries the whole vocabulary",
          set(m) >= {"psps", "fees_pct", "funding", "gateways", "error_classes",
                     "sample_bins", "amount_bands"}
          and m["error_classes"] == list(ERROR_CLASSES) and len(m["sample_bins"]) <= 15)

    base = {"amount": 250, "bin6": "596546", "funding": "debit", "gateway": "checkout"}
    sim = client.post("/api/simulate", json=base).json()
    check("POST /api/simulate sweeps 5 cost_bias points and every single-PSP outage",
          len(sim["cost_bias_sweep"]) == 5 and len(sim["psps_down_scenarios"]) == len(ALL_PSPS)
          and all({"cost_bias", "route_psp", "expected_net"} <= set(s) for s in sim["cost_bias_sweep"]))

    ev = client.post("/api/evidence", json=base).json()
    check("POST /api/evidence reports raw counts per PSP",
          set(ev) == set(ALL_PSPS)
          and all(set(v) == {"level", "segment_used", "n", "approvals", "p_hat", "wilson_lb"}
                  for v in ev.values())
          and all(v["approvals"] <= v["n"] for v in ev.values()))

    nz = client.post("/api/normalize", json={"psp": "psp-b", "raw_code": "do_not_honor"}).json()
    check("POST /api/normalize returns the class plus the enum",
          nz["error_class"] == "generic_decline" and nz["source"] == "table"
          and nz["error_class_options"] == list(ERROR_CLASSES))

    samples = client.get("/api/normalize/samples").json()
    check("GET /api/normalize/samples returns ~12 labelled raw declines",
          10 <= len(samples) <= 14
          and all({"psp", "raw_code", "raw_message", "label"} == set(s) for s in samples))

    bt = client.get("/api/backtest").json()
    check("GET /api/backtest serves the cached headline with its caveats",
          isinstance(bt["headline_lift_pp"], float) and bt["caveats"])

    check("invalid body -> 400 with an error message",
          client.post("/api/decide", json={"gateway": "checkout"}).status_code == 400
          and "error" in client.post("/api/decide", json={"gateway": "checkout"}).json())

    # 5. MCP tools are importable and return well-formed payloads (no server needed)
    import mcp_server as srv
    check("MCP route_transaction returns a decision",
          srv.route_transaction(amount=250, gateway="checkout", bin6="596546",
                                funding="debit")["route_psp"] in ALL_PSPS)
    check("MCP explain_decision returns prose with the reasoning trail",
          "reasoning trail:" in srv.explain_decision(amount=250, gateway="checkout"))
    check("MCP simulate returns both sweeps",
          set(srv.simulate(amount=250, gateway="checkout")) ==
          {"cost_bias_sweep", "psps_down_scenarios"})
    check("MCP segment_evidence covers every PSP",
          set(srv.segment_evidence(amount=250, gateway="pos")) == set(ALL_PSPS))
    check("MCP normalize_decline returns the Normalized shape",
          set(srv.normalize_decline("psp-a", "51")) ==
          {"error_class", "confidence", "source", "provider", "reasoning"})
    check("MCP backtest_summary serves the cached headline",
          "headline_lift_pp" in srv.backtest_summary())
    check("MCP tools are registered on the server", len(srv.mcp._tool_manager.list_tools()) == 6)

    # ops is the single implementation behind both front ends
    check("API and MCP share one implementation",
          ops.route_transaction(amount=250, gateway="checkout")["route_psp"]
          == client.post("/api/decide", json={"amount": 250, "gateway": "checkout"}).json()["route_psp"])

    print(f"\n{len(RUN)} checks: {len(FAILS)} failed")
    if FAILS:
        print("FAILED:", FAILS)
        sys.exit(1)
    print("ALL AI/API TESTS PASSED")


if __name__ == "__main__":
    main()
