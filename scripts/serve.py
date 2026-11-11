"""Starts DerKurator AI with the front end chosen by FRONTEND (default nicegui):

  nicegui   – NiceGUI pages and the HTTP API in one process (frontends/nicegui)
  react     – the built React front end (frontends/react/dist) and the HTTP API in one process

HOST/PORT set the address (default 127.0.0.1:8080). Only one front end serves users at a time; both use the same
data, sign-in and rules.

One server process by default. WEB_CONCURRENCY above 1 starts several workers only for FRONTEND=react with
STATE_STORE=file and an API_TOKEN_SECRET: runs, sign-outs and calculation counters are then shared through files on
the data volume. NiceGUI holds each user's session in its process and is refused (see docs/API.md,
"Limits")."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FRONTENDS = ("nicegui", "react")


def workers(frontend: str) -> int:
    """The number of server processes (uvicorn reads WEB_CONCURRENCY as its worker count); exits when they cannot
    serve `frontend`."""
    from app.api.state_store import several_workers_refused, worker_count

    try:
        count = worker_count()
    except ValueError:
        sys.exit(f"WEB_CONCURRENCY must be a number (got {os.getenv('WEB_CONCURRENCY')!r})")
    try:
        refused = several_workers_refused(count, frontend)
    except RuntimeError as exc:  # an unknown STATE_STORE
        sys.exit(str(exc))
    if refused:
        sys.exit(refused)
    return max(1, count)


def main() -> None:
    choice = os.getenv("FRONTEND", "nicegui").strip().lower()
    if choice not in FRONTENDS:
        sys.exit(f"FRONTEND must be one of {', '.join(FRONTENDS)} (got {choice!r})")
    count = workers(choice)
    import uvicorn

    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8080"))
    if count > 1:  # React only: every worker builds its own app from the factory
        uvicorn.run("frontends.react_host:create_app", factory=True, host=host, port=port, log_level="info",
                    workers=count)
        return
    if choice == "nicegui":
        from frontends.nicegui.main import build

        app = build()
    else:
        from app.api.main import create_app
        from frontends.react_host import mount

        app = mount(create_app())
    uvicorn.run(app, host=host, port=port, log_level="info", workers=1)


if __name__ == "__main__":
    main()
