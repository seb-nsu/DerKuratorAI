"""HTTP API (FastAPI) over the service layer: the one back end for every front end (NiceGUI and
React). No business logic lives here – routes check identity and scope, call the
shared modules and shape JSON. Start: `uvicorn app.api.main:app` (see docs/API.md)."""
