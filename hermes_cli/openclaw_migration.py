from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Optional, Sequence


_OPENCLAW_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "optional-skills"
    / "migration"
    / "openclaw-migration"
    / "scripts"
    / "openclaw_to_hermes.py"
)


@lru_cache(maxsize=1)
def load_openclaw_migration_module() -> ModuleType:
    if not _OPENCLAW_SCRIPT_PATH.exists():
        raise FileNotFoundError(
            f"OpenClaw migration script not found at {_OPENCLAW_SCRIPT_PATH}"
        )

    spec = importlib.util.spec_from_file_location(
        "hermes_openclaw_migration",
        _OPENCLAW_SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Could not load OpenClaw migration script from {_OPENCLAW_SCRIPT_PATH}"
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def get_openclaw_source_root() -> Path:
    return Path.home() / ".openclaw"


def run_openclaw_migration(
    *,
    source_root: Optional[Path] = None,
    target_root: Optional[Path] = None,
    workspace_target: Optional[Path] = None,
    execute: bool = True,
    overwrite: bool = False,
    migrate_secrets: bool = True,
    output_dir: Optional[Path] = None,
    preset: str = "full",
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    skill_conflict_mode: str = "skip",
) -> dict[str, Any]:
    module = load_openclaw_migration_module()
    resolved_source = (source_root or get_openclaw_source_root()).expanduser().resolve()
    resolved_target = (target_root or (Path.home() / ".hermes")).expanduser().resolve()
    resolved_workspace = (
        workspace_target.expanduser().resolve() if workspace_target else None
    )
    resolved_output = output_dir.expanduser().resolve() if output_dir else None
    selected_options = module.resolve_selected_options(include, exclude, preset=preset)
    migrator = module.Migrator(
        source_root=resolved_source,
        target_root=resolved_target,
        execute=execute,
        workspace_target=resolved_workspace,
        overwrite=overwrite,
        migrate_secrets=migrate_secrets,
        output_dir=resolved_output,
        selected_options=selected_options,
        preset_name=preset,
        skill_conflict_mode=skill_conflict_mode,
    )
    return migrator.migrate()
