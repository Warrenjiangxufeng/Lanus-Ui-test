from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default.expanduser()


def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if not str(path).strip() or str(path) == ".":
            continue
        candidate = path.expanduser()
        if candidate.exists():
            return candidate
    return None


def _local_provider_defaults() -> dict[str, Any]:
    """Read provider metadata only. This never invokes the Codex executable."""
    config_path = _env_path(
        "LANUS_PROVIDER_CONFIG",
        Path.home() / ".codex" / "config.toml",
    )
    if not config_path.exists():
        return {}
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        provider_name = str(config.get("model_provider", ""))
        provider = config.get("model_providers", {}).get(provider_name, {})
        return {
            "base_url": str(provider.get("base_url", "")),
            "api_key": str(
                provider.get("experimental_bearer_token")
                or provider.get("api_key")
                or ""
            ),
            "model": str(config.get("model", "")),
        }
    except (OSError, ValueError):
        return {}


@dataclass(slots=True)
class Settings:
    _provider_defaults: dict[str, Any] = field(
        default_factory=_local_provider_defaults,
        init=False,
        repr=False,
    )
    project_root: Path = PROJECT_ROOT
    artifacts_dir: Path = field(
        default_factory=lambda: _env_path(
            "LANUS_ARTIFACTS_DIR",
            Path.home() / "Desktop" / "Lisa输出" / "Lanus-Ui-test",
        )
    )
    database_path: Path | None = None
    planner_backend: str = field(
        default_factory=lambda: os.getenv("LANUS_PLANNER_BACKEND", "responses")
    )
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LANUS_LLM_BASE_URL", "")
    )
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = field(default_factory=lambda: os.getenv("LANUS_LLM_MODEL", ""))
    llm_timeout: int = field(
        default_factory=lambda: _env_int("LANUS_LLM_TIMEOUT", 120)
    )
    headed_default: bool = field(
        default_factory=lambda: not _env_bool("LANUS_HEADLESS", True)
    )
    max_concurrency: int = field(
        default_factory=lambda: _env_int("LANUS_MAX_CONCURRENCY", 3)
    )
    command_timeout: int = field(
        default_factory=lambda: _env_int("LANUS_COMMAND_TIMEOUT", 90)
    )
    observe_timeout: int = field(
        default_factory=lambda: _env_int("LANUS_OBSERVE_TIMEOUT", 30)
    )
    max_step_attempts: int = field(
        default_factory=lambda: _env_int("LANUS_MAX_STEP_ATTEMPTS", 3)
    )
    max_planner_repairs: int = field(
        default_factory=lambda: _env_int("LANUS_MAX_PLANNER_REPAIRS", 2)
    )
    max_action_rounds: int = field(
        default_factory=lambda: _env_int("LANUS_MAX_ACTION_ROUNDS", 8)
    )
    recursion_limit: int = field(
        default_factory=lambda: _env_int("LANUS_RECURSION_LIMIT", 500)
    )
    node_bin: Path | None = field(
        default_factory=lambda: _first_existing(
            _env_path(
                "LANUS_NODE_BIN",
                Path.home() / ".local" / "node-v22.23.2-darwin-x64" / "bin" / "node",
            )
        )
    )
    playwright_bin: Path | None = field(
        default_factory=lambda: _first_existing(
            _env_path("LANUS_PLAYWRIGHT_BIN", Path("/usr/local/bin/playwright-cli")),
            Path(shutil.which("playwright-cli") or ""),
        )
    )
    playwright_js: Path | None = field(
        default_factory=lambda: _first_existing(
            _env_path(
                "LANUS_PLAYWRIGHT_JS",
                Path("/usr/local/lib/node_modules/@playwright/cli/playwright-cli.js"),
            )
        )
    )
    def __post_init__(self) -> None:
        defaults = self._provider_defaults
        if not self.llm_base_url:
            self.llm_base_url = str(defaults.get("base_url", ""))
        if not self.llm_api_key:
            self.llm_api_key = os.getenv("LANUS_LLM_API_KEY") or str(
                defaults.get("api_key", "")
            )
        if not self.llm_model:
            self.llm_model = str(defaults.get("model", ""))

    def ensure_directories(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if self.database_path is None:
            self.database_path = self.artifacts_dir / "tasks.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        path = self.artifacts_dir / "tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        for child in ("snapshots", "screenshots", "reports", "uploads"):
            (path / child).mkdir(parents=True, exist_ok=True)
        return path
