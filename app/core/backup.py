"""Backup of the data folder and a check of the decision log's hash chain – for a scheduler during the pilot.

A backup copies `data/` (decisions, proposals, snapshots, logs, archive, settings) and the portfolio source file if it
lives elsewhere into a dated folder, holds the approval inbox lock while copying (no approval starts meanwhile),
checks the hash chain of the copied decision log and keeps the newest `keep` backups.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core import clock, logs

PREFIX = "derkurator-backup-"
_SKIP = shutil.ignore_patterns("*.lock", "*.writelock", "__pycache__")


@dataclass
class BackupResult:
    folder: Path
    files: int
    chain: dict[str, Any]
    removed: list[Path]

    def summary(self) -> str:
        chain = (f"hash chain intact ({self.chain['records']} records)" if self.chain["ok"] else
                 f"HASH CHAIN BROKEN at line {self.chain['broken_at']}: {self.chain['reason']}")
        removed = f", {len(self.removed)} older backup(s) removed" if self.removed else ""
        return f"Backup {self.folder} – {self.files} files, {chain}{removed}"


def _source_files() -> list[Path]:
    """The portfolio source file of the active data source, if it lives outside the data folder."""
    from app.core.config import get_settings

    cfg = get_settings()
    source = {"json": cfg.json_path, "excel": cfg.excel_path}.get(cfg.data_source)
    data = logs.DATA_DIR.resolve()
    return [source] if source and source.exists() and data not in source.resolve().parents else []


def create_backup(target: Path, keep: int = 7) -> BackupResult:
    """Copies the data folder into `target/derkurator-backup-<timestamp>` and prunes to the newest `keep` backups."""
    from app.core.proposals import _locked

    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    folder = target / f"{PREFIX}{clock.now():%Y%m%d-%H%M%S}"
    with _locked():  # no approval or decision starts while the files are copied
        shutil.copytree(logs.DATA_DIR, folder / "data", ignore=_SKIP)
        for source in _source_files():
            (folder / "source").mkdir(exist_ok=True)
            shutil.copy2(source, folder / "source" / source.name)
    chain = logs.verify_chain(folder / "data" / logs.AUDIT_LOG.name)
    backups = sorted((p for p in target.iterdir() if p.is_dir() and p.name.startswith(PREFIX)), reverse=True)
    removed = backups[max(keep, 1):]
    for old in removed:
        shutil.rmtree(old)
    return BackupResult(folder, sum(1 for p in folder.rglob("*") if p.is_file()), chain, removed)


def verify(folder: Path | None = None) -> dict[str, Any]:
    """Checks the hash chain of the decision log in `folder` (default: the live data folder), e.g. after a restore."""
    return logs.verify_chain(Path(folder) / logs.AUDIT_LOG.name if folder else logs.AUDIT_LOG)
