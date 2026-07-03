"""Runtime configuration persistence — auto-save and auto-load last-used parameters.

File location: ``<sys.executable dir>/merge_tool_last_run.json``

Fallback: ``CWD/merge_tool_last_run.json`` (when the exe directory is not writable).

The module is intentionally minimal: it reads a single JSON file, deserialises
it into typed dataclasses, and provides ``apply_config_to_args`` / ``save_config_from_args``
so callers in *runtime.py* and *guided_commands.py* can integrate with one line.
"""

from __future__ import annotations

import json
import sys
from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_FILE_NAME = "merge_tool_last_run.json"


@dataclass
class MergeConfig:
    repo: str = "."
    base_remote_branch: str = "origin/main"
    work_branch: str | None = None
    merge_branches: list[str] = field(default_factory=list)
    continue_current_branch: bool = False
    allow_dirty_submodules: bool = False
    conflict_tool_command: str = 'code --wait "{file}"'
    conflict_tool_timeout_seconds: int = 600
    enable_cross_submodule_moves: bool = False
    verbose: bool = False


@dataclass
class PublishConfig:
    remote: str = "origin"
    push_target: str = ""
    no_sync_local_branches: bool = False
    no_branch_suffix: bool = False
    no_interactive: bool = False


@dataclass
class CleanupConfig:
    repo: str = "."
    work_branch: str | None = None
    rebuild_work_branch: bool = True
    skip_submodule_update: bool = False
    allow_clean_x: bool = False


@dataclass
class LastRunConfig:
    version: int = 1
    last_command: str = ""
    last_run_at: str = ""
    merge: MergeConfig = field(default_factory=MergeConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)


# -------------------------------------------------------------------- #
#  File-path resolution
# -------------------------------------------------------------------- #

def config_file_path() -> Path:
    """Determine the config file location.

    Prefer the directory containing ``sys.executable`` (works for both
    a normal Python process and a PyInstaller-frozen .exe).  Falls back to
    the current working directory when the exe directory is not writable.
    """
    try:
        exe_dir = Path(sys.executable).parent
    except Exception:
        exe_dir = Path.cwd()
    candidate = exe_dir / CONFIG_FILE_NAME
    if candidate.exists():
        return candidate
    try:
        candidate.touch()
        candidate.unlink()
        return candidate
    except (OSError, PermissionError):
        return Path.cwd() / CONFIG_FILE_NAME


# -------------------------------------------------------------------- #
#  Serialisation helpers
# -------------------------------------------------------------------- #

def _serialize(config: LastRunConfig) -> dict[str, Any]:
    def _convert(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "__dataclass_fields__"):
            return {f.name: _convert(getattr(obj, f.name)) for f in fields(obj)}
        if isinstance(obj, dict):
            return {str(k): _convert(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_convert(v) for v in obj]
        return obj

    raw = asdict(config)
    raw["last_run_at"] = config.last_run_at or datetime.now(timezone.utc).isoformat()
    return _convert(raw)


def _deserialize(raw: dict[str, Any]) -> LastRunConfig:
    def _populate(section_cls: type, data: dict[str, Any]) -> Any:
        kwargs: dict[str, Any] = {}
        for f in fields(section_cls):
            if f.name in data:
                kwargs[f.name] = data[f.name]
        return section_cls(**kwargs)

    return LastRunConfig(
        version=raw.get("version", 1),
        last_command=raw.get("last_command", ""),
        last_run_at=raw.get("last_run_at", ""),
        merge=_populate(MergeConfig, raw.get("merge", {})),
        publish=_populate(PublishConfig, raw.get("publish", {})),
        cleanup=_populate(CleanupConfig, raw.get("cleanup", {})),
    )


# -------------------------------------------------------------------- #
#  Public API
# -------------------------------------------------------------------- #

def load_config() -> LastRunConfig:
    """Load config from file, returning defaults if not found or corrupt."""
    path = config_file_path()
    try:
        if not path.exists():
            return LastRunConfig()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _deserialize(raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return LastRunConfig()


def save_config(config: LastRunConfig) -> None:
    """Save config to file, creating parent dirs if needed."""
    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config.last_run_at = datetime.now(timezone.utc).isoformat()
    payload = _serialize(config)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def save_config_from_args(args: Any, command: str) -> None:
    """Extract relevant fields from an argparse Namespace and persist them."""
    config = load_config()
    config.last_command = command

    if command == "merge":
        mc = config.merge
        mc.repo = str(getattr(args, "repo", mc.repo))
        mc.base_remote_branch = str(getattr(args, "base_remote_branch", mc.base_remote_branch))
        mc.work_branch = getattr(args, "work_branch", None) or None
        mc.merge_branches = list(getattr(args, "merge_branches", []))
        mc.continue_current_branch = bool(getattr(args, "continue_current_branch", False))
        mc.allow_dirty_submodules = bool(getattr(args, "allow_dirty_submodules", False))
        mc.conflict_tool_command = str(getattr(args, "conflict_tool_command", mc.conflict_tool_command))
        mc.conflict_tool_timeout_seconds = int(getattr(args, "conflict_tool_timeout_seconds", 600))
        mc.enable_cross_submodule_moves = bool(getattr(args, "enable_cross_submodule_moves", False))
        mc.verbose = bool(getattr(args, "verbose", False))
    elif command == "publish":
        pc = config.publish
        pc.remote = str(getattr(args, "remote", pc.remote))
        pc.push_target = str(getattr(args, "push_target", pc.push_target))
        pc.no_sync_local_branches = bool(getattr(args, "no_sync_local_branches", False))
        pc.no_branch_suffix = bool(getattr(args, "no_branch_suffix", False))
        pc.no_interactive = bool(getattr(args, "no_interactive", False))
    elif command == "cleanup":
        cc = config.cleanup
        cc.repo = str(getattr(args, "repo", cc.repo))
        cc.work_branch = getattr(args, "work_branch", None) or None
        cc.rebuild_work_branch = bool(getattr(args, "rebuild_work_branch", True))
        cc.skip_submodule_update = bool(getattr(args, "skip_submodule_update", False))
        cc.allow_clean_x = bool(getattr(args, "allow_clean_x", False))

    save_config(config)


def apply_config_to_args(args: Any) -> Any:
    """Apply saved defaults to an argparse Namespace for fields the user did not explicitly set.

    *Explicitly set* is determined by comparing the current value against the
    argparse-defined default.  Only when the two are equal (meaning the user
    did not supply a flag) does the saved config value take effect.

    This function is called in ``main()`` right after ``parse_args()``.
    """
    config = load_config()
    if args.command == "merge":
        _apply_values(args, config.merge)
    elif args.command == "publish":
        _apply_values(args, config.publish)
    elif args.command == "cleanup":
        _apply_values(args, config.cleanup)
    return args


def _apply_values(args: Any, section: Any) -> None:
    """Walk every field defined on *section* and patch *args* when appropriate.

    We compare the current *args* value against the **hardcoded default**
    defined on the config dataclass (e.g. ``"."`` for ``repo``).  If they
    match, the user did not supply a flag and we overwrite with the saved
    config value.
    """
    section_defaults: dict[str, Any] = {}
    for config_field in fields(section):
        if config_field.default is not MISSING:
            section_defaults[config_field.name] = config_field.default
        elif config_field.default_factory is not MISSING:
            section_defaults[config_field.name] = config_field.default_factory()

    for f in fields(section):
        if not hasattr(args, f.name):
            continue
        current = getattr(args, f.name)
        default = section_defaults.get(f.name)
        if current == default and getattr(section, f.name) != default:
            setattr(args, f.name, getattr(section, f.name))
