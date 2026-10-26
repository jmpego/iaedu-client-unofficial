#!/usr/bin/env python3
"""
Live smoke test against the real IAEdu API.

Needs three environment variables (see README):

    IAEDU_API_KEY     your API key
    IAEDU_AGENT_ID    <agent-id>
    IAEDU_CHANNEL_ID  <channel-id>

Then, from the repository root:

    python examples/smoke.py

Sends one short message and prints the HTTP status, latency, assembled text
and the raw stream lines. Exit codes: 0 ok, 1 call failed, 2 configuration
missing, 3 rate limited.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow running this file directly from a clone without installing anything.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iaedu_client import IaeduClient, IaeduError  # noqa: E402

AGENT_ID = os.environ.get("IAEDU_AGENT_ID", "<agent-id>")
CHANNEL_ID = os.environ.get("IAEDU_CHANNEL_ID", "<channel-id>")
TEST_MESSAGE = "Responde apenas: OK"  # deliberately short, to spend few tokens


def main() -> int:
    missing = []
    if not os.environ.get("IAEDU_API_KEY"):
        missing.append("IAEDU_API_KEY")
    if AGENT_ID.startswith("<"):
        missing.append("IAEDU_AGENT_ID")
    if CHANNEL_ID.startswith("<"):
        missing.append("IAEDU_CHANNEL_ID")
    if missing:
        print(f"[ERROR] Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        client = IaeduClient()
    except IaeduError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print(f"base_url   : {client.base_url}")
    print(f"agent_id   : {AGENT_ID}")
    print(f"channel_id : {CHANNEL_ID}")
    print(f"message    : {TEST_MESSAGE!r}")

    try:
        result = client.chat(AGENT_ID, CHANNEL_ID, TEST_MESSAGE, timeout=120)
    except IaeduError as exc:
        print(f"[ERROR] Call failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nstatus       : {result['status']}")
    print(f"latency_ms   : {result['latency_ms']}")
    print(f"rate_limited : {result['rate_limited']}")
    print(f"model        : {result['model']}")
    print(f"error        : {result['error']}")
    print(f"\ntext:\n{result['text']}")
    print(f"\nraw_chunks ({len(result['raw_chunks'])} lines):")
    print(json.dumps(result["raw_chunks"], ensure_ascii=False, indent=2))

    if result["rate_limited"]:
        print("\n[WARN] Rate limited by the server (HTTP 429 or quota headers).")
        return 3
    if result["status"] >= 400:
        print(f"\n[WARN] HTTP {result['status']} -- see raw_chunks above.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
