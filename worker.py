"""Drains ledger.py's outbox in a loop and publishes events to a JSON Lines
file.

ponytail: no message broker. A JSONL sink is the simplest thing that proves
the pattern end to end -- a real deployment swaps jsonl_sink() for a call to
SNS/Kafka/whatever and drain() itself does not change.

Runs as the container's default overridden process:
    docker run --rm -e LEDGER_DB=/data/ledger.db orchestrator python worker.py
or directly:
    .venv/bin/python worker.py
"""
import json
import os
import sys
import time

import ledger

SINK_PATH = os.environ.get(
    "LEDGER_SINK", os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox_sink.jsonl"))
POLL_SECONDS = float(os.environ.get("LEDGER_POLL_SECONDS", "5"))


def jsonl_sink(path):
    """Build a publish() that appends to `path`, deduplicating by event id.

    drain() is at-least-once (see ledger.py's docstring), so the same event
    can arrive here more than once after a crash between publish and the
    outbox UPDATE. This is where that gets collapsed back to one logical
    delivery: seen ids are loaded from the existing file once at startup, so
    dedup survives a worker restart too.
    """
    seen = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    seen.add(json.loads(line)["id"])

    def publish(event):
        if event["id"] in seen:
            return
        seen.add(event["id"])
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")

    return publish


def main():
    publish = jsonl_sink(SINK_PATH)
    while True:
        n = ledger.drain(publish)
        if n:
            print(f"drained {n} event(s) -> {SINK_PATH}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
