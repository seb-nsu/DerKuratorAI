"""HTTP API (FastAPI) over the service layer: the one back end for every front end except the Streamlit pages, which
still call the service layer in-process. No business logic lives here – routes check identity and scope, call the
shared modules and shape JSON. Start: `uvicorn app.api.main:app` (see docs/API.md)."""
