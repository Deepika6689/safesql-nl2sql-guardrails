"""Application settings, loaded from environment variables / a local .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application (least-privilege, SELECT-only) database account
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: SecretStr = SecretStr("")
    mysql_database: str = "safesql"

    # Administrator account: used ONLY by scripts/init_db.py, never by the API.
    mysql_admin_user: str = ""
    mysql_admin_password: SecretStr = SecretStr("")

    # Optional full SQLAlchemy URL override (used for the disposable SQLite test database).
    database_url: SecretStr | None = None

    # LLM
    llm_mode: Literal["auto", "ollama", "demo"] = "auto"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:3b"
    llm_timeout_seconds: float = 60.0

    # Guardrails
    max_rows: int = 100
    query_timeout_seconds: float = 5.0
    max_question_chars: int = 500
    max_request_bytes: int = 8192
    max_sql_chars: int = 2000

    log_level: str = "INFO"

    @property
    def dialect(self) -> str:
        if self.database_url and self.database_url.get_secret_value().startswith("sqlite"):
            return "sqlite"
        return "mysql"

    def app_url(self) -> str | URL:
        if self.database_url:
            return self.database_url.get_secret_value()
        return URL.create(
            "mysql+pymysql",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
            query={"charset": "utf8mb4"},
        )

    def admin_url(self, password: str | None = None, with_database: bool = True) -> str | URL:
        if self.database_url:
            return self.database_url.get_secret_value()
        pw = password if password is not None else self.mysql_admin_password.get_secret_value()
        return URL.create(
            "mysql+pymysql",
            username=self.mysql_admin_user,
            password=pw,
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database if with_database else None,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
