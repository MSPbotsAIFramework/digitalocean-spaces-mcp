"""Non-credential configuration for the DigitalOcean Spaces MCP server.

Credentials are never read from here — see middleware.py. This module only
holds operational knobs (listen address, size caps) and must tolerate any
environment variable the runtime injects that it does not declare.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080

    # AC7: spaces_get_object inline-content cap (Decisions made by DEV01).
    get_inline_max_bytes: int = 100_000
    # AC8: spaces_put_object single-call size cap. Large writes must go
    # through spaces_create_presigned_url instead (Decisions made by DEV01).
    put_max_bytes: int = 5_000_000

    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    max_retries: int = 4


settings = Settings()
