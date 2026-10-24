"""A dispatcher process that stops at one exact point and waits to be killed (tests/test_postgres_notifications.py).

    python tests/notification_child.py claim-then-wait      claims every due notification, commits, prints, waits

The claim is the dispatcher's real ``claim_due`` on its own connection, as ``notifications dispatch`` makes it;
the test then kills this process like a crash, before any attempt began. Reads the database settings from
AGENT_FACTORY_DATABASE_URL and AGENT_FACTORY_DATABASE_SCHEMA.
"""

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repository, as for tests/dashboard_worker.py

from agent_factory.domain.notifications import new_dispatcher_id
from agent_factory.persistence.config import DatabaseConfig
from agent_factory.persistence.notifications import open_notification_outbox


def main() -> None:
    mode = sys.argv[1]
    if mode != "claim-then-wait":
        raise SystemExit(f"unknown mode {mode!r}")
    lease = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    outbox = open_notification_outbox(DatabaseConfig.from_environment())
    claimed = outbox.claim_due(new_dispatcher_id(), timedelta(seconds=lease), limit=50)
    print(json.dumps({"event": "claimed", "ids": [n.id for n in claimed]}), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
