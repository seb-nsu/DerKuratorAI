"""NiceGUI front end. Talks to the HTTP API only (in-process over ASGI by default, or `API_BASE_URL`), never to the
analysis core directly – the same contract as the React front end."""
