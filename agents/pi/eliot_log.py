"""Emit Eliot events for the pi TypeScript harness."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from eliot import log_message

from agents.pi.logging_config import configure_pi_logging


def log_pi_eliot(payload: dict[str, Any]) -> None:
    """Write one structured Eliot message for pi usage accounting."""
    data = dict(payload)
    message_type = str(data.pop("message_type"))
    log_message(message_type=message_type, harness="pi", **data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--payload",
        required=True,
        help="JSON object; must include message_type",
    )
    parser.add_argument(
        "--run-log",
        default=None,
        help="Per-run Eliot log file created by prepare",
    )
    args = parser.parse_args(argv)
    configure_pi_logging(args.run_log)
    payload = json.loads(args.payload)
    if not isinstance(payload, dict):
        raise SystemExit("payload must be a JSON object")
    if "message_type" not in payload:
        raise SystemExit("payload must include message_type")
    log_pi_eliot(payload)
    json.dump({"ok": True}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
