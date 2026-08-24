"""Transactional outbox for routing decisions -- a durable audit trail plus an
at-least-once event feed, without a message broker.

ponytail: SQLite via stdlib `sqlite3`, not Postgres/Redis/Kafka. This is a
portfolio repo demonstrating the pattern, not a production payments company --
one transaction commits the fact (`attempts`) and the event (`outbox`)
together, and a separate drain() publishes. Swap the store for a real database
when a second writer or network access is needed; the outbox/drain shape does
not change.

Vercel's filesystem is read-only, so this module never runs in the deployed
demo -- api/index.py does not import it. It exists for the repo, the
container, and the tests: a non-serverless deployment of the same engine could
call record_decision() from a route handler.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "LEDGER_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    txn_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    publish_count INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_decision(txn: dict, decision: dict, idempotency_key: str, db_path=None) -> dict:
    """Record a routing decision and its outbox event as one atomic unit.

    This is the pattern the whole module exists to demonstrate: the attempt
    row and its outbox event are created together or not at all. A writer
    that persisted `attempts` but crashed before the `outbox` insert would
    produce a decision with no downstream event -- exactly the class of bug
    an outbox pattern rules out. `with conn:` commits both inserts on a clean
    exit and rolls back both on any exception.

    A replay of an already-seen idempotency_key is a no-op: the recorded
    decision is returned and neither table gets a second row. The INSERT
    (not a SELECT-then-INSERT) is what makes this race-safe under concurrent
    writers -- SQLite serializes writes, so the loser of the race gets the
    UNIQUE violation and falls back to reading what the winner wrote.
    """
    conn = _connect(db_path)
    try:
        decision_json = json.dumps(decision)
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO attempts (idempotency_key, txn_json, decision_json, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (idempotency_key, json.dumps(txn), decision_json, "routed", _now()))
                conn.execute(
                    "INSERT INTO outbox (attempt_id, event_type, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (cur.lastrowid, "decision.routed", decision_json, _now()))
        except sqlite3.IntegrityError:
            # Either idempotency_key was already recorded (expected, common),
            # or some other constraint on this transaction failed and the
            # whole thing rolled back (the atomicity test forces this case).
            # Both look the same from here: check whether the key exists now.
            row = conn.execute(
                "SELECT decision_json FROM attempts WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone()
            if row is None:
                raise
            return json.loads(row[0])
        return decision
    finally:
        conn.close()


def drain(publish, limit=100, db_path=None) -> int:
    """Publish up to `limit` un-published outbox events, oldest first, then
    mark each one published. Returns the count published.

    At-least-once, not exactly-once: publish(event) can succeed and this
    process can die before the UPDATE that marks the event published. The
    next drain() call re-selects that same row (published_at is still NULL)
    and calls publish() again. Callers must dedupe by event["id"] -- that
    responsibility cannot live here, because the crash that loses the UPDATE
    is, by definition, a crash this function never gets to react to. See
    worker.py's jsonl_sink() for the dedup side of this contract.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, attempt_id, event_type, payload_json, created_at "
            "FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()
        published = 0
        for event_id, attempt_id, event_type, payload_json, created_at in rows:
            event = {"id": event_id, "attempt_id": attempt_id, "event_type": event_type,
                     "payload": json.loads(payload_json), "created_at": created_at}
            publish(event)
            with conn:
                conn.execute(
                    "UPDATE outbox SET published_at = ?, publish_count = publish_count + 1 WHERE id = ?",
                    (_now(), event_id))
            published += 1
        return published
    finally:
        conn.close()
