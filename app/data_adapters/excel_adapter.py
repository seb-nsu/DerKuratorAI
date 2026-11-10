"""Excel adapter: the same relational tables as worksheets – the Excel connector with the identity mapping. Approved
changes are written back into the workbook (a missing sheet or row column is created; an unknown project column is
skipped with a warning)."""
from __future__ import annotations

from pathlib import Path

from app.data_adapters.connectors import ExcelConnector
from app.data_adapters.mapped_adapter import MappedPortfolioAdapter
from app.data_adapters.mapping import identity_mapping


class ExcelPortfolioAdapter(MappedPortfolioAdapter):
    source_name = "excel"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        super().__init__(identity_mapping(ExcelConnector(self.path)))
