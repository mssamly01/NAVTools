# NAVTools — Hướng dẫn Vibe Code khắc phục lỗi

> Tài liệu hướng dẫn step-by-step để sửa các lỗi còn tồn tại trong repo NAVTools.
> Mỗi mục gồm: **Plan** (phân tích), **Code** (code mẫu sửa), **Prompt** (prompt dùng cho AI assistant).

---

## Mục lục

1. [Khôi phục ApiAuthService (auth bypass)](#1-khôi-phục-apiauthservice)
2. [Di chuyển API key ra .env](#2-di-chuyển-api-key-ra-env)
3. [Sửa import VEO_CLIP_SECONDS sai tên](#3-sửa-import-veo_clip_seconds)
4. [Browser headless mode configurable](#4-browser-headless-mode)
5. [Xóa __static_attributes__ không chuẩn](#5-xóa-static_attributes)
6. [Đóng crash log file handle](#6-đóng-crash-log-file-handle)

---

## 1. Khôi phục ApiAuthService

### Plan

File `services/api_auth.py` hiện tại là **stub** — tất cả method đều bypass:
- `__init__` là `pass` (không lưu `base_url`, `api_key`, `timeout`)
- `login()` luôn trả `True, "OK"` mà không gọi API
- `get_me()` hardcode trả về admin user
- `_headers`, `_url`, `_extract_error_code` đều là `pass`

**Ảnh hưởng:** Không có xác thực thực sự — bất kỳ ai cũng "đăng nhập" được.

### Code

```python
# services/api_auth.py
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
```

### Prompt

```
Hãy khôi phục logic thực cho file services/api_auth.py.

Hiện tại tất cả methods đều là stub (pass hoặc return hardcoded).
Cần implement đầy đủ:
- __init__: lưu base_url, api_key, timeout, machine_id
- _headers: tạo headers với API key và optional JWT
- _url: build full URL từ base_url + path
- _extract_error_code: parse error code từ response JSON
- login: POST /auth/login với username, password, machine_id
- register: POST /auth/register
- heartbeat: POST /auth/heartbeat với JWT
- get_me: GET /auth/me với JWT

Dùng httpx cho HTTP calls. Wrap tất cả trong try/except httpx.RequestError
để trả NETWORK_ERROR khi mất kết nối. Giữ nguyên interface (return types).
```

---

## 2. Di chuyển API key ra .env

### Plan

File `config/constants.py:240` hardcode API key:
```python
CLIENT_API_KEY = os.environ.get("NAVTOOLS_CLIENT_API_KEY", "0erOa6TaylTHz8WNAM-LeZfM-YXAqNBvQ4iiN8N7cnc")
```
Và Google Sheet ID:
```python
GSHEET_SHEET_ID = "1VyZFh5bhkM-ZV1wHmRxGBY2sSr62CCsQHIeF536tsHw"
```

**Ảnh hưởng:** Credential bị lộ trong source code. Ai có repo đều thấy API key.

### Code

```python
# config/constants.py — thay đổi dòng 240
CLIENT_API_KEY = os.environ.get("NAVTOOLS_CLIENT_API_KEY", "")

# Tạo file .env.example
NAVTOOLS_API_BASE_URL=https://workspace.navtools.vn
NAVTOOLS_CLIENT_API_KEY=your-api-key-here
GSHEET_SHEET_ID=your-gsheet-id-here
```

Thêm vào `main.py` (đầu file, trước khi import constants):
```python
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")
```

Thêm `python-dotenv` vào `requirements.txt`.

### Prompt

```
Di chuyển API key và credentials ra khỏi source code:

1. Trong config/constants.py, xóa default value của CLIENT_API_KEY
   (thay "0erOa6TaylTHz8WNAM-LeZfM-YXAqNBvQ4iiN8N7cnc" thành "")
2. Tạo file .env.example với template cho các biến môi trường cần thiết
3. Thêm python-dotenv vào requirements.txt
4. Load .env trong main.py bằng dotenv.load_dotenv()
5. Đảm bảo .env đã có trong .gitignore (đã có rồi)
6. GSHEET_SHEET_ID cũng nên chuyển sang env var
```

---

## 3. Sửa import VEO_CLIP_SECONDS

### Plan

File `services/script_analyzer.py:18` import tên không tồn tại:
```python
from services.youtube_analyzer import VEO_CLIP_SECONDS  # KHÔNG TỒN TẠI
```
Tên đúng trong `youtube_analyzer.py` là `VEO_VIDEO_LENGTH`.
Code có fallback nên không crash, nhưng import luôn fail → chạy vào except.

### Code

```python
# services/script_analyzer.py — sửa dòng 17-23
# Trước:
try:
    from services.youtube_analyzer import VEO_CLIP_SECONDS
except ImportError:
    try:
        from services.youtube_analyzer import VEO_VIDEO_LENGTH as VEO_CLIP_SECONDS
    except ImportError:
        VEO_CLIP_SECONDS = 8

# Sau:
from services.youtube_analyzer import VEO_VIDEO_LENGTH as VEO_CLIP_SECONDS
```

### Prompt

```
Trong services/script_analyzer.py, sửa import VEO_CLIP_SECONDS.

Hiện tại code try import "VEO_CLIP_SECONDS" từ youtube_analyzer nhưng tên
đó không tồn tại — tên đúng là "VEO_VIDEO_LENGTH".

Thay toàn bộ try/except block bằng:
from services.youtube_analyzer import VEO_VIDEO_LENGTH as VEO_CLIP_SECONDS
```

---

## 4. Browser headless mode

### Plan

File `automation/browser_manager.py:124` hardcode `headless=False`:
```python
browser = await pw.chromium.launch(
    headless=False,
    executable_path=chrome_exe,
```

Trên server hoặc CI environment, Chrome sẽ cần headless mode.

### Code

```python
# automation/browser_manager.py — thêm parameter
class BrowserManager:
    def __init__(self, ..., headless: bool = False):
        ...
        self._headless = headless

    # Trong method launch:
    browser = await pw.chromium.launch(
        headless=self._headless,
        executable_path=chrome_exe,
    )
```

### Prompt

```
Trong automation/browser_manager.py, làm cho headless mode có thể cấu hình:

1. Thêm parameter headless=False vào __init__ của BrowserManager
2. Lưu vào self._headless
3. Thay headless=False ở dòng 124 thành headless=self._headless
4. Cho phép đọc từ settings hoặc environment variable NAVTOOLS_HEADLESS
```

---

## 5. Xóa __static_attributes__

### Plan

File `automation/recaptcha_provider.py:64` define `__static_attributes__` thủ công:
```python
__static_attributes__ = ('_browser', '_chrome_proc', ...)
```
Đây là internal attribute của Python 3.12+ compiler, không nên tự define.

### Code

```python
# Xóa hoàn toàn block __static_attributes__ trong class SubprocessTokenProvider
# Thay bằng khai báo instance variables trong __init__ nếu chưa có
```

### Prompt

```
Trong automation/recaptcha_provider.py, xóa __static_attributes__ tuple
ở dòng 64. Đây là internal Python 3.12 attribute không nên define thủ công.

Đảm bảo tất cả các attributes liệt kê trong tuple đó đều được khởi tạo
trong __init__ thay vì dùng __static_attributes__.
```

---

## 6. Đóng crash log file handle

### Plan

File `main.py:48` mở file handle cho crash log mà không bao giờ đóng:
```python
_crash_fp = open(_crash_dir / "crash.log", "a", buffering=1)
faulthandler.enable(file=_crash_fp)
```

### Code

```python
# main.py — thêm cleanup
import atexit

_crash_fp = open(_crash_dir / "crash.log", "a", buffering=1)
faulthandler.enable(file=_crash_fp)
atexit.register(lambda: _crash_fp.close())
```

### Prompt

```
Trong main.py, file handle _crash_fp (crash.log) được mở nhưng không bao
giờ đóng. Thêm atexit.register để đóng file khi app thoát:

import atexit
atexit.register(lambda: _crash_fp.close())

Đặt ngay sau dòng faulthandler.enable(file=_crash_fp).
```

---

## Tổng hợp — Mega Prompt

Dùng prompt dưới đây để sửa tất cả cùng lúc:

```
Sửa các lỗi sau trong repo NAVTools:

1. **services/api_auth.py**: Khôi phục logic thực cho ApiAuthService.
   - __init__: lưu base_url, api_key, timeout, tạo machine_id
   - _headers: trả dict với X-API-Key, X-Machine-ID, optional Authorization Bearer
   - _url: return f"{base_url}/{path}"
   - _extract_error_code: parse error_code từ response.json()
   - login: POST /auth/login, lưu JWT từ response
   - register: POST /auth/register
   - heartbeat: POST /auth/heartbeat với JWT
   - get_me: GET /auth/me với JWT
   Dùng httpx, wrap trong try/except httpx.RequestError → return NETWORK_ERROR

2. **config/constants.py:240**: Xóa default API key hardcoded.
   Đổi từ: os.environ.get("NAVTOOLS_CLIENT_API_KEY", "0erOa6TaylTHz8WNAM-LeZfM-YXAqNBvQ4iiN8N7cnc")
   Thành: os.environ.get("NAVTOOLS_CLIENT_API_KEY", "")
   Tạo file .env.example với template.

3. **services/script_analyzer.py:17-23**: Sửa import VEO_CLIP_SECONDS.
   Thay try/except block bằng:
   from services.youtube_analyzer import VEO_VIDEO_LENGTH as VEO_CLIP_SECONDS

4. **automation/browser_manager.py:124**: Làm headless configurable.
   Thêm headless param vào __init__, dùng self._headless thay vì hardcode False.

5. **automation/recaptcha_provider.py:64**: Xóa __static_attributes__ tuple.

6. **main.py**: Thêm atexit.register(lambda: _crash_fp.close()) sau faulthandler.enable.
```

---

## Checklist sau khi sửa

- [ ] App khởi động không crash (`python main.py`)
- [ ] Login screen hiển thị đúng
- [ ] Login với credentials thực hoạt động (nếu có server)
- [ ] Thêm tài khoản Google không lỗi
- [ ] Gia hạn cookie không lỗi
- [ ] Script analyzer chạy không crash
- [ ] Video concat chạy không crash
- [ ] Không có credentials nào trong source code
