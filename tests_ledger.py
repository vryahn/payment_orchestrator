"""Assert-based checks for ledger.py's outbox pattern. No framework -- run
directly:
    .venv/bin/python tests_ledger.py

Every test gets its own tempfile database -- never the repo's ledger.db.
"""
import json
import os
import sqlite3
import sys
import tempfile

import ledger

FAILS, RUN = [], []


def check(name, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
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


def _tmp_db():
    d = tempfile.mkdtemp()
    return os.path.join(d, "ledger.db")


def test_atomicity():
    db = _tmp_db()
    ledger._connect(db).close()  # create the schema, then attach a forced failure
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TRIGGER force_outbox_fail BEFORE INSERT ON outbox "
        "BEGIN SELECT RAISE(ABORT, 'forced failure for the atomicity test'); END;")
    conn.close()

    txn, decision = {"amount": 100, "gateway": "checkout"}, {"route_psp": "psp-a"}
    check("forced outbox failure raises",
          raises(sqlite3.DatabaseError,
                 lambda: ledger.record_decision(txn, decision, "atomic-key", db_path=db)))

    conn = sqlite3.connect(db)
    n_attempts = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    n_outbox = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    conn.close()
    check("no orphaned attempts row after rollback", n_attempts == 0)
    check("no orphaned outbox row after rollback", n_outbox == 0)


def test_idempotency():
    db = _tmp_db()
    txn = {"amount": 250, "gateway": "checkout"}
    first = {"route_psp": "psp-b"}
    d1 = ledger.record_decision(txn, first, "idem-key", db_path=db)
    d2 = ledger.record_decision(txn, {"route_psp": "psp-c"}, "idem-key", db_path=db)
    check("replay with the same key returns the original decision", d1 == first and d2 == first)

    conn = sqlite3.connect(db)
    n_attempts = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE idempotency_key = ?", ("idem-key",)).fetchone()[0]
    n_outbox = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    conn.close()
    check("exactly one attempt row for the key", n_attempts == 1)
    check("exactly one outbox event for the key", n_outbox == 1)


def test_crash_mid_drain():
    db = _tmp_db()
    ledger.record_decision({"amount": 300, "gateway": "pos"}, {"route_psp": "psp-a"},
                           "crash-key", db_path=db)

    sink = []

    def crash_after_publish(event):
        sink.append(event)
        raise RuntimeError("simulated crash: publish succeeded, process died before the UPDATE")

    check("drain propagates a mid-loop publish failure",
          raises(RuntimeError, lambda: ledger.drain(crash_after_publish, db_path=db)))

    conn = sqlite3.connect(db)
    published_at = conn.execute("SELECT published_at FROM outbox").fetchone()[0]
    conn.close()
    check("event is still marked unpublished after the crash", published_at is None)

    n = ledger.drain(lambda event: sink.append(event), db_path=db)
    check("re-drain republishes the same event", n == 1 and len(sink) == 2)

    deduped = {event["id"]: event for event in sink}
    check("dedup by event id collapses the redelivery to one logical delivery", len(deduped) == 1)


def test_durability():
    db = _tmp_db()
    ledger.record_decision({"amount": 500, "gateway": "checkout"}, {"route_psp": "psp-a"},
                           "durable-key", db_path=db)

    # A fresh connection stands in for the process restarting: nothing here
    # reuses the connection record_decision() opened and closed above.
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT published_at FROM outbox").fetchone()
    conn.close()
    check("unpublished row survives closing and reopening the database",
          row is not None and row[0] is None)


def main():
    test_atomicity()
    test_idempotency()
    test_crash_mid_drain()
    test_durability()

    print(f"\n{len(RUN)} checks: {len(FAILS)} failed")
    if FAILS:
        print("FAILED:", FAILS)
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
