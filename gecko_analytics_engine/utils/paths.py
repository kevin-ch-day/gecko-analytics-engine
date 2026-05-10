"""Path resolution helpers for the Project Gecko Analytics Engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings


@dataclass(frozen=True)
class AppPaths:
    """Resolved filesystem paths used by the application."""

    project_root: Path
    data_root: Path
    output_root: Path
    logs_dir: Path
    runs_dir: Path
    exports_dir: Path
    reports_dir: Path
    figures_dir: Path
    models_dir: Path

    @property
    def main_log_file(self) -> Path:
        return self.logs_dir / "gecko_analytics_engine.log"


def resolve_project_root() -> Path:
    """Resolve the repository root from this module location."""

    return Path(__file__).resolve().parents[2]


def initialize_paths(settings: AppSettings, project_root: Path | None = None) -> AppPaths:
    """Resolve and create required application directories."""

    root = (project_root or resolve_project_root()).resolve()
    data_root = _resolve_under_root(settings.data_root, root)
    output_root = _resolve_under_root(settings.output_root, root)

    paths = AppPaths(
        project_root=root,
        data_root=data_root,
        output_root=output_root,
        logs_dir=output_root / "logs",
        runs_dir=output_root / "runs",
        exports_dir=output_root / "exports",
        reports_dir=output_root / "reports",
        figures_dir=output_root / "figures",
        models_dir=output_root / "models",
    )

    for directory in (
        paths.data_root,
        paths.output_root,
        paths.logs_dir,
        paths.runs_dir,
        paths.exports_dir,
        paths.reports_dir,
        paths.figures_dir,
        paths.models_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    return paths


def _resolve_under_root(path: Path, project_root: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (project_root / path).resolve()
