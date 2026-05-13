"""HTTPS-backed auth service. (Bypass version for development)

Public interface:
    login(username, password)        -> (bool, error_code)
    register(username, password, email="") -> (bool, error_code)
    heartbeat()                       -> (bool, error_code)
    get_me()                          -> Optional[dict]
    user_exists(username)             -> bool
"""

from typing import Optional
import httpx
from utils.logger import log

NETWORK_ERROR = 'NETWORK_ERROR'

class ApiAuthService:
    """Auth service with bypass for local development."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._jwt = "dev-bypass-token"

    def login(self, username: str, password: str) -> tuple[bool, str]:
        log.info(f"Dev Mode: Bypassing login for user '{username}'")
        self._jwt = f"bypassed-jwt-{username}"
        return True, "OK"

    def register(self, username: str, password: str, email: str = '') -> tuple[bool, str]:
        log.info(f"Dev Mode: Bypassing registration for user '{username}'")
        self._jwt = f"bypassed-jwt-{username}"
        return True, "OK"

    def heartbeat(self) -> tuple[bool, str]:
        return True, "OK"

    def get_me(self) -> Optional[dict]:
        return {
            "id": 1,
            "username": "admin",
            "email": "admin@navtools.vn",
            "tier": "premium",
            "expires_at": "2099-12-31T23:59:59Z"
        }

    def user_exists(self, username: str) -> bool:
        return False