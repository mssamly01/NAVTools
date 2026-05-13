"""HTTPS-backed auth service.

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
from utils.machine_id import get_machine_id

NETWORK_ERROR = 'NETWORK_ERROR'
KNOWN_ERROR_CODES = {
    'USER_NOT_FOUND', 'WRONG_PASSWORD', 'ACCOUNT_DISABLED',
    'ACCOUNT_EXPIRED', 'MACHINE_MISMATCH', 'USERNAME_EXISTS',
    'MACHINE_ALREADY_REGISTERED', 'INVALID_API_KEY',
    'INVALID_TOKEN', 'TOKEN_EXPIRED', NETWORK_ERROR,
}


class ApiAuthService:
    """HTTPS-backed auth service."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._jwt: Optional[str] = None
        self._machine_id = get_machine_id()

    def _headers(self, with_jwt: bool = False) -> dict:
        h = {
            "Content-Type": "application/json",
            "X-API-Key": self._api_key,
            "X-Machine-ID": self._machine_id,
        }
        if with_jwt and self._jwt:
            h["Authorization"] = f"Bearer {self._jwt}"
        return h

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _extract_error_code(self, resp: httpx.Response) -> str:
        try:
            data = resp.json()
            code = data.get("error_code") or data.get("code") or data.get("error", "")
            if code in KNOWN_ERROR_CODES:
                return code
        except Exception:
            pass
        return f"HTTP_{resp.status_code}"

    def login(self, username: str, password: str) -> tuple[bool, str]:
        try:
            resp = httpx.post(
                self._url("/auth/login"),
                json={"username": username, "password": password,
                      "machine_id": self._machine_id},
                headers=self._headers(),
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._jwt = data.get("token") or data.get("jwt")
                return True, "OK"
            return False, self._extract_error_code(resp)
        except httpx.RequestError as e:
            log.warning(f"Login network error: {e}")
            return False, NETWORK_ERROR

    def register(self, username: str, password: str, email: str = '') -> tuple[bool, str]:
        try:
            resp = httpx.post(
                self._url("/auth/register"),
                json={"username": username, "password": password,
                      "email": email, "machine_id": self._machine_id},
                headers=self._headers(),
                timeout=self._timeout,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                self._jwt = data.get("token") or data.get("jwt")
                return True, "OK"
            return False, self._extract_error_code(resp)
        except httpx.RequestError as e:
            log.warning(f"Register network error: {e}")
            return False, NETWORK_ERROR

    def heartbeat(self) -> tuple[bool, str]:
        if not self._jwt:
            return False, "INVALID_TOKEN"
        try:
            resp = httpx.post(
                self._url("/auth/heartbeat"),
                json={"machine_id": self._machine_id},
                headers=self._headers(with_jwt=True),
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                return True, "OK"
            return False, self._extract_error_code(resp)
        except httpx.RequestError:
            return False, NETWORK_ERROR

    def get_me(self) -> Optional[dict]:
        if not self._jwt:
            return None
        try:
            resp = httpx.get(
                self._url("/auth/me"),
                headers=self._headers(with_jwt=True),
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                return resp.json()
        except httpx.RequestError as e:
            log.warning(f"get_me error: {e}")
        return None

    def user_exists(self, username: str) -> bool:
        return False  # best-effort, server doesn't expose this
