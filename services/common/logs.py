"""One JSON object per line on stdout. Kubernetes collects stdout from every
container, and log tools can then filter by service, request id, status, etc."""

import json
import sys
from datetime import UTC, datetime


def log(level: str, message: str, **fields) -> None:
    entry = {"time": datetime.now(UTC).isoformat(), "level": level, "message": message, **fields}
    print(json.dumps(entry, default=str), file=sys.stdout, flush=True)
