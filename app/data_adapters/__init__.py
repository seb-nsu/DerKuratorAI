from app.core.config import Settings, get_settings
from app.data_adapters.base import (
    ApplyResult,
    DataConflictError,
    DataSourceError,
    PortfolioDataProvider,
    ProjectRecord,
    WriteNotAuthorizedError,
)
from app.data_adapters.dataset import PortfolioDataset
from app.data_adapters.excel_adapter import ExcelPortfolioAdapter
from app.data_adapters.json_adapter import JsonPortfolioAdapter
from app.data_adapters.mapped_adapter import MappedPortfolioAdapter


def get_data_provider(settings: Settings | None = None) -> PortfolioDataProvider:
    """Factory: selects the adapter based on DATA_SOURCE (default: json; mapped = a mapping file, MAPPING_FILE;
    servicenow = the snapshot of the ServiceNow Table API through SERVICENOW_MAPPING_FILE)."""
    settings = settings or get_settings()
    if settings.data_source == "json":
        return JsonPortfolioAdapter(settings.json_path)
    if settings.data_source == "excel":
        return ExcelPortfolioAdapter(settings.excel_path)
    if settings.data_source == "mapped":
        return MappedPortfolioAdapter(settings.mapping_path)
    if settings.data_source == "servicenow":
        return MappedPortfolioAdapter(settings.servicenow_mapping_path)
    raise DataSourceError(f"Unknown DATA_SOURCE '{settings.data_source}'. Supported: json, excel, mapped, servicenow")


def _mapping_path(settings: Settings):
    return {"mapped": settings.mapping_path, "servicenow": settings.servicenow_mapping_path}.get(settings.data_source)


def servicenow_connectors(settings: Settings | None = None) -> list:
    """The ServiceNow sources of the configured mapping (empty for any other data source)."""
    from app.data_adapters.mapping import load_mapping
    from app.data_adapters.servicenow import ServiceNowConnector

    path = _mapping_path(settings or get_settings())
    if path is None:
        return []
    return [c for c in load_mapping(path).connectors.values() if isinstance(c, ServiceNowConnector)]


def sync_servicenow(settings: Settings | None = None, full: bool = False) -> list:
    """Reads the changes from ServiceNow into the local snapshot of every ServiceNow source; returns the reports."""
    connectors = servicenow_connectors(settings)
    if not connectors:
        raise DataSourceError("The configured data source has no ServiceNow source (DATA_SOURCE=servicenow).")
    return [c.sync(full=full) for c in connectors]


def work_connectors(settings: Settings | None = None) -> list:
    """The Microsoft Planner and monday.com sources of the configured mapping (empty for any other data source)."""
    from app.data_adapters.mapping import load_mapping
    from app.data_adapters.work_management import SnapshotConnector

    path = _mapping_path(settings or get_settings())
    if path is None:
        return []
    return [c for c in load_mapping(path).connectors.values() if isinstance(c, SnapshotConnector)]


def sync_work_management(settings: Settings | None = None) -> list:
    """Reads every Planner plan and monday.com board of the configured mapping into its snapshot; returns the reports."""
    connectors = work_connectors(settings)
    if not connectors:
        raise DataSourceError("The configured mapping has no Microsoft Planner or monday.com source.")
    return [c.sync() for c in connectors]


def source_files(settings: Settings | None = None) -> list:
    """The files that make up the configured data (change detection), without reading the data itself."""
    settings = settings or get_settings()
    if settings.data_source == "json":
        return [settings.json_path]
    if settings.data_source == "excel":
        return [settings.excel_path]
    if settings.data_source in ("mapped", "servicenow"):
        from app.data_adapters.mapping import load_mapping

        return load_mapping(_mapping_path(settings)).files()
    return []


__all__ = [
    "ApplyResult",
    "DataConflictError",
    "DataSourceError",
    "ExcelPortfolioAdapter",
    "JsonPortfolioAdapter",
    "MappedPortfolioAdapter",
    "PortfolioDataProvider",
    "PortfolioDataset",
    "ProjectRecord",
    "WriteNotAuthorizedError",
    "get_data_provider",
    "servicenow_connectors",
    "source_files",
    "sync_servicenow",
    "sync_work_management",
    "work_connectors",
]
