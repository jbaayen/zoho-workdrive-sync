"""Configuration and token persistence."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get(
    "XDG_CONFIG_HOME", Path.home() / ".config"
)) / "zoho-workdrive-sync"

CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
STATE_DB = CONFIG_DIR / "state.db"


@dataclass
class Config:
    client_id: str = ""
    client_secret: str = ""
    local_folder: str = ""
    remote_folder_id: str = ""
    remote_folder_name: str = ""
    team_id: str = ""
    workspace_id: str = ""
    interval_seconds: int = 300


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    if not CONFIG_FILE.exists():
        return Config()
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    except Exception as e:
        logger.warning(f"Could not load config: {e}")
        return Config()


def save_config(cfg: Config) -> None:
    ensure_config_dir()
    from dataclasses import asdict
    CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2))


def load_refresh_token() -> Optional[str]:
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text())
        return data.get("refresh_token")
    except Exception:
        return None


def save_refresh_token(token: str) -> None:
    ensure_config_dir()
    TOKEN_FILE.write_text(json.dumps({"refresh_token": token}))
    # Restrict permissions to owner only
    TOKEN_FILE.chmod(0o600)
