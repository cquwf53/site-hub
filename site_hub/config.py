from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

@dataclass(slots=True)
class Settings:
    """Runtime settings. The public listener is deliberately not configurable."""

    host: str = "127.0.0.1"
    port: int = 8686
    project_root: Path = PROJECT_ROOT
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("SITEHUB_DATA_DIR", PROJECT_ROOT / "data"))
    )
    frontend_dir: Path = field(
        default_factory=lambda: Path(os.getenv("SITEHUB_FRONTEND_DIR", PROJECT_ROOT / "frontend" / "dist"))
    )
    node_executable: str = field(default_factory=lambda: os.getenv("SITEHUB_NODE", "node"))
    node_entry: Path = field(
        default_factory=lambda: Path(
            os.getenv("SITEHUB_NODE_ENTRY", PROJECT_ROOT / "worker-node" / "src" / "main.js")
        )
    )
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    checkin_timezone: str = "Asia/Shanghai"
    checkin_window_start: str = "08:00:00"
    checkin_window_end: str = "22:00:00"
    product_interval_minutes: int = 60
    scheduler_tick_seconds: int = 30
    worker_handshake_seconds: int = 10
    worker_ping_seconds: int = 15
    worker_heartbeat_seconds: int = 10
    worker_lease_seconds: int = 45
    worker_shutdown_seconds: int = 20
    scrape_concurrency: int = 3

    @property
    def db_path(self) -> Path:
        return self.data_dir / "site-hub.db"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "browser-profiles"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def allowed_hosts(self) -> set[str]:
        return {
            "127.0.0.1",
            "localhost",
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        }

    def prepare_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.profiles_dir,
            self.artifacts_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
