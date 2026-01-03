"""Configuration loader for video management system."""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class ConfigLoader:
    """Load and manage configuration from YAML files."""

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            config_dir = Path(__file__).parent.parent / "config"
        self.config_dir = Path(config_dir)
        self._settings: Optional[Dict[str, Any]] = None
        self._secrets: Optional[Dict[str, Any]] = None
        self._credentials: Optional[Dict[str, Any]] = None

    def _load_yaml(self, filename: str) -> Dict[str, Any]:
        """Load a YAML file from the config directory."""
        filepath = self.config_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Config file not found: {filepath}")
        with open(filepath, "r") as f:
            return yaml.safe_load(f) or {}

    def _load_yaml_optional(self, filename: str) -> Dict[str, Any]:
        """Load a YAML file if it exists, otherwise return empty dict."""
        filepath = self.config_dir / filename
        if not filepath.exists():
            return {}
        with open(filepath, "r") as f:
            return yaml.safe_load(f) or {}

    @property
    def settings(self) -> Dict[str, Any]:
        """Load and cache settings.yaml."""
        if self._settings is None:
            self._settings = self._load_yaml("settings.yaml")
        return self._settings

    @property
    def credentials(self) -> Dict[str, Any]:
        """Load and cache credentials.yaml (master credentials file)."""
        if self._credentials is None:
            self._credentials = self._load_yaml_optional("credentials.yaml")
        return self._credentials

    @property
    def secrets(self) -> Dict[str, Any]:
        """Load and cache secrets.yaml (legacy, prefer credentials.yaml)."""
        if self._secrets is None:
            self._secrets = self._load_yaml_optional("secrets.yaml")
        return self._secrets

    @property
    def aws_region(self) -> str:
        # Try credentials.yaml first, then settings
        if self.credentials.get("aws", {}).get("region"):
            return self.credentials["aws"]["region"]
        return self.settings.get("aws", {}).get("region", "us-east-1")

    @property
    def s3_bucket(self) -> str:
        if self.credentials.get("aws", {}).get("s3_bucket"):
            return self.credentials["aws"]["s3_bucket"]
        return self.settings.get("aws", {}).get("s3", {}).get("bucket", "mv-brain")

    @property
    def s3_prefixes(self) -> Dict[str, str]:
        return self.settings.get("aws", {}).get("s3", {}).get("prefixes", {})

    @property
    def aws_access_key(self) -> str:
        # Try credentials.yaml first, then secrets.yaml
        if self.credentials.get("aws", {}).get("access_key_id"):
            return self.credentials["aws"]["access_key_id"]
        return self.secrets.get("aws", {}).get("access_key_id", "")

    @property
    def aws_secret_key(self) -> str:
        if self.credentials.get("aws", {}).get("secret_access_key"):
            return self.credentials["aws"]["secret_access_key"]
        return self.secrets.get("aws", {}).get("secret_access_key", "")

    @property
    def db_connection_string(self) -> str:
        """Build PostgreSQL connection string for video_management database."""
        return self.get_db_connection_string("peraspera_brain")

    def get_db_connection_string(self, db_name: str = "peraspera_brain") -> str:
        """Build PostgreSQL connection string for any configured database."""
        # Try credentials.yaml first (new format)
        if self.credentials.get("databases", {}).get(db_name):
            db_config = self.credentials["databases"][db_name]
            host = db_config.get("host", "localhost")
            port = db_config.get("port", 5432)
            database = db_config.get("database", "video_management")
            username = db_config.get("username", "postgres")
            password = db_config.get("password", "")
            return f"postgresql://{username}:{password}@{host}:{port}/{database}?sslmode=require"

        # Fall back to legacy secrets.yaml format
        rds_secrets = self.secrets.get("rds", {})
        rds_settings = self.settings.get("aws", {}).get("rds", {})

        host = rds_secrets.get("host", "localhost")
        port = rds_settings.get("port", 5432)
        database = rds_settings.get("database", "video_management")
        username = rds_secrets.get("username", "postgres")
        password = rds_secrets.get("password", "")

        return f"postgresql://{username}:{password}@{host}:{port}/{database}?sslmode=require"

    @property
    def openai_api_key(self) -> str:
        """Get OpenAI API key."""
        if self.credentials.get("apis", {}).get("openai", {}).get("api_key"):
            return self.credentials["apis"]["openai"]["api_key"]
        return self.secrets.get("openai", {}).get("api_key", "")

    @property
    def anthropic_api_key(self) -> str:
        """Get Anthropic API key."""
        if self.credentials.get("apis", {}).get("anthropic", {}).get("api_key"):
            return self.credentials["apis"]["anthropic"]["api_key"]
        return self.secrets.get("anthropic", {}).get("api_key", "")

    @property
    def transcription_provider(self) -> str:
        return self.settings.get("transcription", {}).get("provider", "aws")

    @property
    def transcription_language(self) -> str:
        return self.settings.get("transcription", {}).get("language", "en-US")

    @property
    def video_output_format(self) -> str:
        return self.settings.get("video", {}).get("output_format", "mp4")

    @property
    def video_codec(self) -> str:
        return self.settings.get("video", {}).get("codec", "libx264")

    @property
    def audio_codec(self) -> str:
        return self.settings.get("video", {}).get("audio_codec", "aac")

    @property
    def temp_dir(self) -> Path:
        temp = self.settings.get("local", {}).get("temp_dir", "/tmp/video-processing")
        path = Path(temp)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def log_level(self) -> str:
        return self.settings.get("logging", {}).get("level", "INFO")

    @property
    def log_file(self) -> str:
        return self.settings.get("logging", {}).get("file", "logs/video_management.log")


# Global config instance
_config: Optional[ConfigLoader] = None


def get_config() -> ConfigLoader:
    """Get the global config instance."""
    global _config
    if _config is None:
        _config = ConfigLoader()
    return _config
