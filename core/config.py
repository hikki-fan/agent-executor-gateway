"""
Neutral Configuration Module for Agent Executor Gateway.
Defines server defaults, connection limits, and transport-level configuration.
Core remains strictly executor-neutral and contains zero provider configuration.
"""

from __future__ import annotations
import os
from dataclasses import dataclass

# Transport and network defaults
DEFAULT_PORT = 8765
DEFAULT_TOKEN_FILE = "/home/codex/.codex/acp_token"
DEFAULT_MAX_CONTENT_LENGTH = 2 * 1024 * 1024  # 2MB Limit
DEFAULT_MAX_HTTP_CONNECTIONS = 50
DEFAULT_MAX_POST_CONNECTIONS = 45
DEFAULT_GATEWAY_MAX_CONCURRENCY = 2
DEFAULT_SOCKET_TIMEOUT = 10.0  # 10s socket read/write timeout


@dataclass(frozen=True)
class GatewayConfig:
    """Transport and security configuration for the gateway server."""
    port: int = DEFAULT_PORT
    token_file: str = DEFAULT_TOKEN_FILE
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH
    max_http_connections: int = DEFAULT_MAX_HTTP_CONNECTIONS
    max_post_connections: int = DEFAULT_MAX_POST_CONNECTIONS
    max_gateway_concurrency: int = DEFAULT_GATEWAY_MAX_CONCURRENCY
    socket_timeout_sec: float = DEFAULT_SOCKET_TIMEOUT

    @classmethod
    def from_env(cls) -> GatewayConfig:
        """Construct GatewayConfig from environment variables for transport settings."""
        port = int(os.environ.get("ACP_PORT", DEFAULT_PORT))
        token_file = os.environ.get("ACP_TOKEN_FILE") or DEFAULT_TOKEN_FILE
        max_gateway_concurrency = int(
            os.environ.get("GATEWAY_MAX_CONCURRENCY", DEFAULT_GATEWAY_MAX_CONCURRENCY)
        )

        return cls(
            port=port,
            token_file=token_file,
            max_content_length=DEFAULT_MAX_CONTENT_LENGTH,
            max_http_connections=DEFAULT_MAX_HTTP_CONNECTIONS,
            max_post_connections=DEFAULT_MAX_POST_CONNECTIONS,
            max_gateway_concurrency=max_gateway_concurrency,
            socket_timeout_sec=DEFAULT_SOCKET_TIMEOUT,
        )
