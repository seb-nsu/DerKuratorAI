"""JSON adapter for the ServiceNow export (normalized relational tables): the JSON connector with the identity
mapping, so it goes through the same schema checks and import report as every mapped source."""
from __future__ import annotations

from pathlib import Path

from app.data_adapters.connectors import JsonConnector
from app.data_adapters.mapped_adapter import MappedPortfolioAdapter
from app.data_adapters.mapping import identity_mapping


class JsonPortfolioAdapter(MappedPortfolioAdapter):
    source_name = "json"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        super().__init__(identity_mapping(JsonConnector(self.path)))
