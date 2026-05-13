"""VidGen AI — Google Flow AI Sandbox API client.

Auth: Playwright headless → /fx/api/auth/session → ya29.* access_token
API:
  - Upload:   POST aisandbox-pa.googleapis.com/v1/flow/uploadImage
  - Generate: POST aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideo*
  - Poll:     POST aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus
  - Download: tRPC media.getMediaUrlRedirect
  - Image:    POST aisandbox-pa.googleapis.com/v1:runImageFx
Flow: get_token → upload_images → generate → poll → download
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from config.constants import MAX_RETRY_COUNT, POLL_INTERVAL_SECONDS
from utils.logger import log

if TYPE_CHECKING:
    from playwright.async_api import Page


_BUNDLED_PW_DIR = Path(__file__).parent / "playwright"
AISANDBOX_BASE = "https://aisandbox-pa.googleapis.com/v1"
X_CLIENT_DATA = "CIa2yQEIprbJAQipncoBCLb9ygEIlqHLAQiFoM0BCNmqzwEY/qXPARikqM8BGMOrzwE="
X_BROWSER_VALIDATION = "AKIAtsVHZoiKbPixy+qSK1BgKWo="
_RECAPTCHA_LOCKS: dict[str, asyncio.Lock] = {}


def _get_recaptcha_lock(account_email):
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0
    key = f"{loop_id}:{account_email or '?'}"
    lock = _RECAPTCHA_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _RECAPTCHA_LOCKS[key] = lock
    return lock


class FlowClient:
    TRPC = "https://labs.google/fx/api/trpc"
    SESSION_URL = "https://labs.google/fx/api/auth/session"
    _caption_cache: dict[str, str] = {}
    _IMAGE_MODEL_FALLBACK = {
        "Nano Banana 2": "IMAGEN_3_5",
        "Imagen 3.5": "IMAGEN_3_5",
        "Imagen 4": "IMAGEN_4",
        "Imagen 4 Ultra": "IMAGEN_4_ULTRA",
    }

    def __init__(self, page: "Page", cookie_path=None, account_email: str | None = None):
        self._page = page
        self._token = None
        self._session_id = str(uuid.uuid4())
        self._cookie_path = cookie_path
        self._account_email = account_email or "?"
        self._last_remaining_credits = None
        self._last_credit_cost = 0
        self._last_model_key = None
        self._recaptcha_provider = None
        self._recaptcha_fail_count = 0

    async def _fetch_session_token(self):
        """Fetch access token from NextAuth session endpoint. Returns token or None."""
        try:
            result = await self._page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {credentials: "include"});
                    if (!r.ok) return {error: r.status};
                    return await r.json();
                }""",
                self.SESSION_URL,
            )
            token = (result or {}).get("accessToken") or (result or {}).get("access_token")
            if token:
                log.info("Session token obtained")
                return token
            log.debug(f"Session response (no token): {json.dumps(result, ensure_ascii=False)[:300]}")
        except Exception as e:
            log.debug(f"Session fetch error: {e}")
        return None

    async def _dismiss_overlays(self):
        """Dismiss cookie consent, terms dialogs, or other overlays on labs.google."""
        overlay_buttons = [
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("I agree")',
            'button:has-text("Agree")',
            'button:has-text("OK")',
            'button:has-text("Got it")',
            'button:has-text("Đồng ý")',
            'button:has-text("Chấp nhận")',
            'button:has-text("Dismiss")',
            'button:has-text("Close")',
            '[aria-label="Close"]',
            '[aria-label="Dismiss"]',
        ]
        dismissed = False
        for sel in overlay_buttons:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click(force=True)
                    log.info(f"Dismissed overlay: {sel}")
                    dismissed = True
                    await asyncio.sleep(1)
            except Exception:
                pass

        # Also try removing blocking overlays via JS
        try:
            removed = await self._page.evaluate("""() => {
                let removed = 0;
                document.querySelectorAll('[data-state="open"][aria-hidden="true"]').forEach(el => {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                    removed++;
                });
                // Also hide any fixed/absolute overlays blocking clicks
                document.querySelectorAll('div[class*="overlay"], div[class*="modal"], div[class*="backdrop"]').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'absolute') {
                        el.style.display = 'none';
                        removed++;
                    }
                });
                return removed;
            }""")
            if removed:
                log.info(f"Removed {removed} blocking overlay(s) via JS")
                dismissed = True
        except Exception:
            pass

        return dismissed

    async def _handle_google_account_picker(self):
        """Handle the Google account picker page if we land on accounts.google.com."""
        current_url = self._page.url or ""
        if "accounts.google.com" not in current_url:
            return

        log.info(f"On Google account picker: {current_url[:80]}")
        # Try clicking the account email
        try:
            account_el = await self._page.wait_for_selector(
                '[data-email], .JDAKTe, div[role="link"]',
                timeout=10000,
            )
            if account_el:
                await account_el.click()
                log.info("Clicked account, waiting for redirect back...")
        except Exception:
            pass

        # Wait for redirect back to labs.google
        try:
            await self._page.wait_for_url("**/labs.google/**", timeout=30000)
            log.info("Redirected back to labs.google")
        except Exception:
            log.warning(f"Still on: {(self._page.url or '')[:80]}")

    async def _try_sign_in(self):
        """Attempt Google sign-in on labs.google.

        Strategy (in order):
        1. Navigate to NextAuth sign-in endpoint directly (most reliable)
        2. Extract OAuth URL from page and navigate directly
        3. Click Sign-in button with force=True
        """
        await self._dismiss_overlays()

        # Try 1: NextAuth CSRF + sign-in endpoint (bypasses UI entirely)
        try:
            log.info("Trying NextAuth direct sign-in...")
            csrf_result = await self._page.evaluate("""async () => {
                try {
                    const r = await fetch('/fx/api/auth/csrf', {credentials: 'include'});
                    if (r.ok) return await r.json();
                } catch(e) {}
                return null;
            }""")
            csrf_token = (csrf_result or {}).get("csrfToken")

            if csrf_token:
                log.info(f"Got CSRF token, initiating Google sign-in...")
                # Navigate to the NextAuth sign-in page for Google provider
                signin_url = "https://labs.google/fx/api/auth/signin/google"
                await self._page.goto(signin_url, wait_until="load", timeout=30000)
                await asyncio.sleep(2)

                # This should redirect to Google OAuth. Since we have Google
                # cookies in the persistent context, it may auto-complete.
                await self._handle_google_account_picker()
                await asyncio.sleep(2)

                # Check if we're back on labs.google
                if "labs.google" in (self._page.url or ""):
                    token = await self._fetch_session_token()
                    if token:
                        log.info("NextAuth direct sign-in succeeded!")
                        self._token = token
                        return True
                    log.info("NextAuth sign-in redirected back but no token yet")
                else:
                    log.info(f"After NextAuth sign-in, on: {(self._page.url or '')[:80]}")
        except Exception as e:
            log.info(f"NextAuth direct sign-in failed: {e}")

        # Try 2: Extract OAuth URL from page source and navigate
        try:
            log.info("Trying to extract OAuth URL from page...")
            if "labs.google" not in (self._page.url or ""):
                await self._page.goto("https://labs.google/fx", wait_until="load", timeout=30000)
                await asyncio.sleep(3)

            oauth_url = await self._page.evaluate("""() => {
                // Look for Google OAuth URLs in the page
                const links = document.querySelectorAll('a[href*="accounts.google.com/o/oauth2"]');
                if (links.length > 0) return links[0].href;

                // Check meta tags, scripts, etc.
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent || '';
                    const match = text.match(/(https:\\/\\/accounts\\.google\\.com\\/o\\/oauth2[^"'\\s]+)/);
                    if (match) return match[1];
                }

                // Check data attributes on sign-in buttons
                const btns = document.querySelectorAll('button, a');
                for (const b of btns) {
                    const text = b.textContent || '';
                    if (text.includes('Sign in') || text.includes('Đăng nhập')) {
                        // Check onclick, data attributes
                        const onclick = b.getAttribute('onclick') || '';
                        const match2 = onclick.match(/(https:\\/\\/accounts\\.google\\.com[^"'\\s]+)/);
                        if (match2) return match2[1];
                    }
                }
                return null;
            }""")

            if oauth_url:
                log.info(f"Found OAuth URL, navigating directly...")
                await self._page.goto(oauth_url, wait_until="load", timeout=30000)
                await asyncio.sleep(2)
                await self._handle_google_account_picker()
                await asyncio.sleep(2)

                if "labs.google" in (self._page.url or ""):
                    token = await self._fetch_session_token()
                    if token:
                        log.info("OAuth direct navigation succeeded!")
                        self._token = token
                        return True
        except Exception as e:
            log.info(f"OAuth URL extraction failed: {e}")

        # Try 3: Click sign-in button (popup or redirect)
        try:
            if "labs.google" not in (self._page.url or ""):
                await self._page.goto("https://labs.google/fx", wait_until="load", timeout=30000)
                await asyncio.sleep(3)
                await self._dismiss_overlays()

            btn = None
            for sel in ['a[href*="accounts.google.com"]', 'button:has-text("Sign in")',
                        'a:has-text("Sign in")', 'button:has-text("Đăng nhập")']:
                try:
                    btn = await self._page.query_selector(sel)
                    if btn and await btn.is_visible():
                        log.info(f"Found sign-in button: {sel}")
                        break
                    btn = None
                except Exception:
                    btn = None

            if btn:
                # Try popup first
                try:
                    log.info("Attempting sign-in via popup...")
                    async with self._page.context.expect_page(timeout=5000) as page_info:
                        await btn.click(force=True)
                    popup = await page_info.value
                    log.info(f"Popup opened: {popup.url[:80]}")
                    try:
                        await popup.wait_for_event("close", timeout=30000)
                        log.info("Sign-in popup closed")
                    except Exception:
                        try:
                            account_btn = await popup.query_selector('[data-email], .JDAKTe')
                            if account_btn:
                                await account_btn.click()
                                await popup.wait_for_event("close", timeout=15000)
                        except Exception:
                            try:
                                await popup.close()
                            except Exception:
                                pass
                    await asyncio.sleep(3)
                    return True
                except Exception as e:
                    log.info(f"Popup failed ({e}), trying JS navigation...")

                # Try extracting href from the button/link and navigating
                try:
                    href = await btn.get_attribute("href")
                    if href and "accounts.google.com" in href:
                        log.info(f"Navigating to sign-in href directly...")
                        await self._page.goto(href, wait_until="load", timeout=30000)
                        await asyncio.sleep(2)
                        await self._handle_google_account_picker()
                        await asyncio.sleep(2)
                        return "labs.google" in (self._page.url or "")
                except Exception:
                    pass

                # Force click as last resort
                try:
                    await btn.click(force=True)
                    await asyncio.sleep(3)
                    if "accounts.google.com" in (self._page.url or ""):
                        await self._handle_google_account_picker()
                        return True
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Button sign-in failed: {e}")

        return False

    async def ensure_token(self):
        """Get ya29.* access token from NextAuth session."""
        if self._token:
            return self._token

        log.info("Getting session token...")
        if "labs.google" not in (self._page.url or ""):
            await self._page.goto("https://labs.google/fx", wait_until="load", timeout=30000)
            await asyncio.sleep(3)

        # Attempt 1: session might already be valid (e.g. labs.google cookies exist)
        token = await self._fetch_session_token()
        if token:
            self._token = token
            return token

        # Attempt 2: complete the sign-in flow
        log.info("No session token — attempting sign-in...")
        signed_in = await self._try_sign_in()

        if signed_in:
            # Re-navigate to ensure page is on labs.google
            if "labs.google" not in (self._page.url or ""):
                await self._page.goto("https://labs.google/fx", wait_until="load", timeout=30000)
                await asyncio.sleep(3)

            token = await self._fetch_session_token()
            if token:
                self._token = token
                return token

        # Attempt 3: try going directly to image-fx/video-fx which may trigger auth
        log.info("Retrying via direct tool URL...")
        await self._page.goto("https://labs.google/fx/tools/image-fx", wait_until="load", timeout=30000)
        await asyncio.sleep(3)

        token = await self._fetch_session_token()
        if token:
            self._token = token
            return token

        raise RuntimeError(
            "Could not get Google session access token. "
            "Cookies may be expired — try renewing in Settings → Tài khoản Google → Gia hạn"
        )

    def set_recaptcha_provider(self, provider):
        """Set external reCAPTCHA token provider (SubprocessTokenProvider)."""
        self._recaptcha_provider = provider

    async def renew_token(self):
        """Force refresh of the session token."""
        log.info("Renewing session token...")
        self._token = None
        token = await self.ensure_token()
        provider = self._recaptcha_provider
        if provider and getattr(provider, "is_running", False):
            try:
                cookies = await self._page.context.cookies()
                await provider.refresh_cookies(cookies)
            except Exception as e:
                log.warning(f"Failed to refresh provider cookies: {e}")
        return token

    async def get_recaptcha_token(self, action: str):
        lock = _get_recaptcha_lock(self._account_email)
        async with lock:
            provider = self._recaptcha_provider
            if provider:
                try:
                    token = await provider.get_token(action)
                    if token:
                        self._recaptcha_fail_count = 0
                        return token
                except Exception as e:
                    self._recaptcha_fail_count += 1
                    log.warning(f"reCAPTCHA provider failed: {e}")

            # Wait for grecaptcha to load on the page
            for _wait in range(20):
                loaded = await self._page.evaluate(
                    "typeof grecaptcha !== 'undefined' && "
                    "!!(grecaptcha.enterprise || grecaptcha.execute)"
                )
                if loaded:
                    break
                await asyncio.sleep(0.5)

            result = await self._page.evaluate(
                """async (action) => {
                    try {
                        let siteKey = null;
                        const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                        for (const s of scripts) {
                            const m = s.src.match(/[?&]render=([^&]+)/);
                            if (m) { siteKey = m[1]; break; }
                        }
                        if (!siteKey) return {error: 'no_site_key'};
                        if (typeof grecaptcha === 'undefined') return {error: 'grecaptcha_not_loaded'};
                        if (grecaptcha.enterprise) {
                            const token = await grecaptcha.enterprise.execute(siteKey, {action});
                            if (token) return {token, key: siteKey};
                            return {error: 'empty_token_enterprise'};
                        }
                        if (grecaptcha.execute) {
                            const token = await grecaptcha.execute(siteKey, {action});
                            if (token) return {token, key: siteKey};
                            return {error: 'empty_token_v2'};
                        }
                        return {error: 'grecaptcha_api_unavailable'};
                    } catch(e) {
                        return {error: e.message || String(e)};
                    }
                }""",
                action,
            )
            if isinstance(result, dict) and result.get("token"):
                self._recaptcha_fail_count = 0
                return result["token"]
            self._recaptcha_fail_count += 1
            raise RuntimeError(f"Could not get reCAPTCHA token for {action}: {result}")

    async def _api(self, procedure: str, payload=None, method: str = "POST"):
        await self.ensure_token()
        import urllib.parse

        payload = payload or {}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "x-client-data": X_CLIENT_DATA,
            "x-browser-validation": X_BROWSER_VALIDATION,
        }
        if method.upper() == "GET":
            url = f"{self.TRPC}/{procedure}?input={urllib.parse.quote(json.dumps(payload))}"
            resp = await self._page.request.get(url, headers=headers)
        else:
            resp = await self._page.request.post(f"{self.TRPC}/{procedure}", headers=headers, data=json.dumps(payload))
        if not resp.ok:
            text = await resp.text()
            raise RuntimeError(f"tRPC {procedure} failed HTTP {resp.status}: {text[:500]}")
        return await resp.json()

    def _extract(self, result):
        """Extract result.data.json.result from tRPC response."""
        return (((result or {}).get("result") or {}).get("data") or {}).get("json")

    async def _browser_sandbox_request(self, endpoint: str, payload: dict):
        await self.ensure_token()
        url = f"{AISANDBOX_BASE}/{endpoint.lstrip('/')}"
        body_json = json.dumps(payload)
        for attempt in range(MAX_RETRY_COUNT):
            result = await self._page.evaluate(
                """async ({url, token, body}) => {
                    const r = await fetch(url, {
                        method: "POST",
                        headers: {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                        body
                    });
                    const text = await r.text();
                    try { return {status: r.status, ok: r.ok, json: JSON.parse(text)}; }
                    catch(e) { return {status: r.status, ok: r.ok, text}; }
                }""",
                {"url": url, "token": self._token, "body": body_json},
            )
            if result.get("ok"):
                return result.get("json")
            if result.get("status") in (401, 403):
                await self.renew_token()
            await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"Sandbox request failed: {result}")

    async def _sandbox_request(self, endpoint: str, payload=None, method: str = "POST", raw_body=None, content_type="application/json"):
        import httpx

        await self.ensure_token()
        url = f"{AISANDBOX_BASE}/{endpoint.lstrip('/')}"
        body = raw_body if raw_body is not None else json.dumps(payload or {}).encode()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": content_type,
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "x-client-data": X_CLIENT_DATA,
            "x-browser-validation": X_BROWSER_VALIDATION,
        }
        for attempt in range(MAX_RETRY_COUNT):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(url, content=body, headers=headers)
                if resp.status_code in (401, 403):
                    log.warning(f"Sandbox auth failed HTTP {resp.status_code}, renewing token")
                    await self.renew_token()
                    headers["Authorization"] = f"Bearer {self._token}"
                    await asyncio.sleep(1 + attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                log.error(f"Sandbox request error: {e}")
                if attempt == MAX_RETRY_COUNT - 1:
                    raise
                await asyncio.sleep(1 + attempt)

    async def _get_cookies_for_httpx(self):
        """Extract cookies from CDP browser for use with httpx requests."""
        try:
            all_cookies = await self._page.context.cookies()
            return {c.get("name"): c.get("value") for c in all_cookies if "google" in c.get("domain", "")}
        except Exception as e:
            log.warning(f"Failed to extract cookies: {e}")
            return {}

    async def _video_gen_httpx(self, url: str, payload: dict):
        import httpx

        token = self._token
        cookie_dict = await self._get_cookies_for_httpx()
        ua = await self._page.evaluate("() => navigator.userAgent")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "User-Agent": ua,
            "x-client-data": X_CLIENT_DATA,
            "x-browser-validation": X_BROWSER_VALIDATION,
        }
        async with httpx.AsyncClient(timeout=120, cookies=cookie_dict) as client:
            resp = await client.post(url, content=json.dumps(payload).encode(), headers=headers)
        if resp.status_code >= 400:
            log.error(f"Video gen HTTP {resp.status_code}: {resp.text[:500]}")
            raise RuntimeError(resp.text[:500])
        return resp.json()

    async def check_credits(self):
        """Check remaining video credits via aisandbox API."""
        await self.ensure_token()
        try:
            result = await self._page.evaluate(
                """async (token) => {
                    try {
                        const r = await fetch("https://aisandbox-pa.googleapis.com/v1/whisk:getVideoCreditStatus", {
                            method: "GET",
                            headers: {"Authorization": "Bearer " + token, "Origin": "https://labs.google", "Referer": "https://labs.google/"}
                        });
                        if (r.ok) return await r.json();
                        return {error: r.status};
                    } catch(e) { return {error: e.message}; }
                }""",
                self._token,
            )
            if result.get("error"):
                log.warning(f"Credit check failed: {json.dumps(result)[:300]}")
            else:
                log.info(f"Credits: {json.dumps(result, ensure_ascii=False)[:300]}")
            return result
        except Exception as e:
            log.error(f"Credit check error: {e}")
            return None

    async def get_video_models(self):
        """Fetch available video models from tRPC API."""
        result = await self._api("videoFx.getVideoModelConfig", {}, "GET")
        data = self._extract(result) or {}
        models = data.get("videoModelConfigs", [])
        log.info(f"VideoModelRegistry: {len(models)}")
        return models

    async def get_image_models(self):
        """Fetch available image models from tRPC API."""
        try:
            result = await self._api("imageFx.getImageModelConfig", {}, "GET")
            data = self._extract(result) or {}
            models = data.get("imageModelConfigs", [])
            log.info(f"ImageModelRegistry: {len(models)}")
            return models
        except Exception as e:
            log.warning(f"Failed to fetch image models from API: {e}")
            fallback = [{"displayName": "Nano Banana 2", "modelNameType": "IMAGEN_3_5"}]
            log.info(f"ImageModelRegistry: using {len(fallback)} fallback")
            return fallback

    def _map_image_model(self, display_name: str):
        """Map UI display name → API modelNameType enum value."""
        dynamic = getattr(self, "_image_model_mapping", {})
        return dynamic.get(display_name) or self._IMAGE_MODEL_FALLBACK.get(display_name, "IMAGEN_3_5")

    async def load_image_model_mapping(self):
        """Fetch image models from API and cache mapping."""
        models = await self.get_image_models()
        self._image_model_mapping = {}
        display_names = []
        for m in models:
            display = m.get("displayName")
            model_type = m.get("modelNameType")
            if display and model_type:
                self._image_model_mapping[display] = model_type
                display_names.append(display)
        log.info(f"Image model mapping loaded: {display_names}")
        return display_names

    async def upload_image(self, image_path, name: Optional[str] = None):
        await self.ensure_token()
        from PIL import Image
        import io

        image_path = Path(image_path)
        name = name or image_path.name
        with open(image_path, "rb") as f:
            raw = f.read()
        try:
            img = Image.open(io.BytesIO(raw))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((1536, 1536), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            raw = buf.getvalue()
        except Exception:
            pass
        image_b64 = base64.b64encode(raw).decode("ascii")
        payload = {"image": {"bytesBase64Encoded": image_b64, "mimeType": "image/jpeg"}, "name": name}
        log.info(f"Uploading image {name}: {len(raw)} bytes")
        result = await self._sandbox_request("flow/uploadImage", payload)
        self._last_upload_response = result
        return result.get("media") or result.get("name") or result

    async def generate_video(self, prompt, image_paths=None, model="veo-3.1-fast", aspect_ratio="16:9", duration=8, quality="720p", seed=None):
        self._poll_logged = False
        await self.ensure_token()
        project_id = getattr(self, "_project_id", None) or str(uuid.uuid4())
        self._project_id = project_id
        images = []
        for p in image_paths or []:
            images.append(await self.upload_image(p))
        recaptcha_token = await self.get_recaptcha_token("video_generate")
        self._last_model_key = model
        payload = {
            "clientContext": {"sessionId": self._session_id, "projectId": project_id},
            "requests": [{
                "prompt": prompt,
                "imageInputs": images,
                "aspectRatio": aspect_ratio,
                "durationSeconds": int(duration),
                "quality": quality,
                "model": model,
                "seed": seed or int(time.time()),
                "recaptchaToken": recaptcha_token,
            }],
        }
        url = f"{AISANDBOX_BASE}/video:batchAsyncGenerateVideo"
        result = await self._video_gen_httpx(url, payload)
        return self._extract_generation_id(result, 0) or result

    async def flow_concat(self, media_ids, output_path=None, timeout=300):
        if not media_ids:
            log.error("flow_concat called without media_ids")
            return None
        await self.ensure_token()
        input_videos = [{"mediaId": mid, "startOffsetMs": i * 8000} for i, mid in enumerate(media_ids)]
        payload = {"inputVideos": input_videos}
        result = await self._aisandbox_post_absolute(f"{AISANDBOX_BASE}/video:concat", payload)
        media_id = self._extract_generation_id(result, 0) or result.get("mediaId") or result.get("name")
        if output_path and media_id:
            await self.download_video(media_id, output_path)
        return media_id

    async def _aisandbox_post_absolute(self, url: str, payload: dict):
        await self.ensure_token()
        body_json = json.dumps(payload)
        for attempt in range(MAX_RETRY_COUNT):
            try:
                if self._page.is_closed():
                    raise RuntimeError("Browser page is closed")
                result = await asyncio.wait_for(
                    self._page.evaluate(
                        """async ({url, token, body}) => {
                            const r = await fetch(url, {method: "POST", headers: {"Authorization": "Bearer " + token, "Content-Type": "application/json"}, body});
                            const text = await r.text();
                            try { return {ok: r.ok, status: r.status, json: JSON.parse(text)}; }
                            catch(e) { return {ok: r.ok, status: r.status, text}; }
                        }""",
                        {"url": url, "token": self._token, "body": body_json},
                    ),
                    timeout=120,
                )
                if result.get("ok"):
                    return result.get("json")
                if result.get("status") in (401, 403):
                    await self.renew_token()
                await asyncio.sleep(1 + attempt)
            except Exception as e:
                log.warning(f"aisandbox post failed: {e}")
                if attempt == MAX_RETRY_COUNT - 1:
                    raise
        raise RuntimeError("aisandbox post failed")

    async def extend_video(self, media_id, prompt, quality="720p", aspect_ratio="16:9"):
        await self.ensure_token()
        workflow_id = str(uuid.uuid4())
        recaptcha_token = await self.get_recaptcha_token("video_extend")
        payload = {
            "clientContext": {"sessionId": self._session_id, "workflowId": workflow_id},
            "mediaId": media_id,
            "prompt": prompt,
            "quality": quality,
            "aspectRatio": aspect_ratio,
            "recaptchaToken": recaptcha_token,
        }
        result = await self._browser_sandbox_request("video:extend", payload)
        return self._extract_generation_id(result, 0) or result

    def _extract_generation_id(self, result, _depth=0):
        if _depth > 6:
            return None
        if isinstance(result, dict):
            media_arr = result.get("media")
            if isinstance(media_arr, list) and media_arr:
                media_name = media_arr[0].get("name") or media_arr[0].get("mediaId")
                if media_name:
                    return media_name
            for key in ("name", "mediaId", "generationId", "operationName"):
                val = result.get(key)
                if isinstance(val, str):
                    return val
            for val in result.values():
                nested = self._extract_generation_id(val, _depth + 1)
                if nested:
                    return nested
        elif isinstance(result, list):
            for item in result:
                nested = self._extract_generation_id(item, _depth + 1)
                if nested:
                    return nested
        return None

    def _image_to_base64(self, image_path):
        from PIL import Image
        import io

        try:
            with open(os.path.abspath(image_path), "rb") as f:
                raw = f.read()
            img = Image.open(io.BytesIO(raw))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((1536, 1536), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            log.info(f"Image base64 {Path(image_path).name}: {buf.getbuffer().nbytes} bytes")
            return "data:image/jpeg;base64," + data
        except Exception as e:
            log.error(f"Image encode failed: {e}")
            raise

    async def _backbone_generate_caption(self, image_b64: str, category: str):
        """Call backbone.generateCaption tRPC to get a text description of an image."""
        img_hash = hashlib.md5(image_b64[:8192].encode()).hexdigest()
        cached = FlowClient._caption_cache.get(img_hash)
        if cached:
            log.info(f"Backbone caption (cached): {cached[:80]}...")
            return cached
        payload = {"category": category, "image": image_b64, "sessionId": self._session_id}
        try:
            result = await self._api("backbone.generateCaption", payload)
            data = self._extract(result) or result
            caption = data if isinstance(data, str) else data.get("caption") or data.get("prompt") or ""
            FlowClient._caption_cache[img_hash] = caption
            return caption
        except Exception as e:
            log.warning(f"Backbone caption failed: {e}")
            return ""

    async def _backbone_generate_storyboard_prompt(self, characters, additional_input: str):
        """Call backbone.generateStoryBoardPrompt to build an enhanced prompt."""
        payload = {
            "characters": characters,
            "additionalInput": additional_input,
            "sessionId": self._session_id,
        }
        try:
            result = await self._api("backbone.generateStoryBoardPrompt", payload)
            data = self._extract(result) or result
            if isinstance(data, str):
                return data
            if isinstance(data, dict):
                enhanced = data.get("prompt") or data.get("enhancedPrompt")
                if enhanced:
                    return enhanced
                prompts = data.get("prompts")
                if isinstance(prompts, list) and prompts:
                    return prompts[0]
        except Exception as e:
            log.warning(f"Storyboard prompt failed: {e}")
        return additional_input

    async def _generate_image_whisk(self, prompt, image_paths=None, model="Nano Banana 2", aspect_ratio="1:1"):
        await self.ensure_token()
        project_id = getattr(self, "_project_id", None) or str(uuid.uuid4())
        self._project_id = project_id
        inputs = []
        for p in image_paths or []:
            if os.path.isfile(p):
                inputs.append(await self.upload_image(p))
            else:
                log.warning(f"Reference image not found: {p}")
        recaptcha_token = await self.get_recaptcha_token("image_generate")
        payload = {
            "clientContext": {"sessionId": self._session_id, "projectId": project_id},
            "prompt": prompt,
            "imageInputs": inputs,
            "model": self._map_image_model(model),
            "aspectRatio": aspect_ratio,
            "recaptchaToken": recaptcha_token,
        }
        return await self._browser_sandbox_request(":runImageFx", payload)

    async def generate_image(self, prompt, image_paths=None, model="Nano Banana 2", aspect_ratio="1:1"):
        await self.ensure_token()
        if image_paths:
            characters = []
            for i, p in enumerate(image_paths):
                image_b64 = self._image_to_base64(p)
                caption = await self._backbone_generate_caption(image_b64, "CHARACTER")
                characters.append({
                    "imageId": str(uuid.uuid4()),
                    "category": "CHARACTER",
                    "base64Image": image_b64,
                    "isPlaceholder": False,
                    "index": i,
                    "isSelected": True,
                    "prompt": caption,
                })
            prompt = await self._backbone_generate_storyboard_prompt(characters, prompt)

        result = await self._generate_image_whisk(prompt, image_paths, model, aspect_ratio)

        def _extract_first_image(d):
            """Walk known response shapes, return first image dict or None."""
            if not isinstance(d, dict):
                return None
            media = d.get("media")
            if isinstance(media, list) and media:
                first = media[0]
                img_wrap = first.get("image") if isinstance(first, dict) else None
                gen = (img_wrap or {}).get("generatedImage")
                if gen:
                    gen.setdefault("name", first.get("name"))
                    return gen
            generated = d.get("generatedImages")
            if isinstance(generated, list) and generated:
                return generated[0]
            for key in ("imagePanels", "responses"):
                arr = d.get(key)
                if isinstance(arr, list):
                    for entry in arr:
                        got = _extract_first_image(entry)
                        if got:
                            return got
            return None

        image = _extract_first_image(result)
        if not image:
            self._last_error = result
            raise RuntimeError(f"Image gen failed: {json.dumps(result, ensure_ascii=False)[:500]}")
        return image

    async def upsample_image(self, media_id, resolution="2K"):
        await self.ensure_token()
        res_key = resolution.upper().replace(" ", "")
        target_enum = {"2K": "UPSCALE_2K", "4K": "UPSCALE_4K"}.get(res_key, "UPSCALE_2K")
        recaptcha_token = await self.get_recaptcha_token("image_upsample")
        payload = {"mediaId": media_id, "targetResolution": target_enum, "recaptchaToken": recaptcha_token}
        return await self._browser_sandbox_request("image:upscale", payload)

    async def upsample_video(self, media_id, output_path=None, resolution="1080p"):
        await self.ensure_token()
        recaptcha_token = await self.get_recaptcha_token("video_upsample")
        payload = {
            "mediaId": media_id,
            "targetResolution": resolution.upper().replace("P", "p"),
            "sessionId": self._session_id,
            "recaptchaToken": recaptcha_token,
        }
        result = await self._browser_sandbox_request("video:upscale", payload)
        out_id = self._extract_generation_id(result, 0) or media_id
        if output_path:
            await self.download_video(out_id, output_path)
        return out_id

    async def poll_status(self, generation_id):
        await self.ensure_token()
        payload = {"generationIds": [generation_id]}
        raw = await self._browser_sandbox_request("video:batchCheckAsyncVideoGenerationStatus", payload)
        media_arr = raw.get("media") if isinstance(raw, dict) else None
        if isinstance(media_arr, list) and media_arr:
            item = media_arr[0]
            status = str(item.get("status") or item.get("state") or "").upper()
            if status in ("SUCCEEDED", "SUCCESS", "COMPLETED", "DONE"):
                return {"status": "COMPLETED", "result": item, "media": item}
            if status in ("FAILED", "ERROR", "CANCELLED"):
                return {"status": "FAILED", "error": item}
            return {"status": "RUNNING", "raw": item}
        return raw if isinstance(raw, dict) else {"status": "UNKNOWN", "raw": raw}

    async def wait_for_completion(self, generation_id, timeout=600, callback=None, cancel_check=None):
        """Poll until video generation completes or times out."""
        start = time.time()
        polls = 0
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 6
        while time.time() - start < timeout:
            if cancel_check and cancel_check():
                log.info("wait_for_completion: cancelled by caller")
                return {"status": "FAILED", "error": "CANCELLED"}
            polls += 1
            try:
                s = await self.poll_status(generation_id)
                consecutive_errors = 0
                if callback:
                    callback(s)
                status = str(s.get("status", "")).upper()
                if status in ("COMPLETED", "FAILED", "FATAL"):
                    return s
                log.info(f"Poll #{polls}: {json.dumps(s, ensure_ascii=False)[:300]}")
            except Exception as e:
                consecutive_errors += 1
                log.error(f"Poll error {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}: {e}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    return {"status": "FAILED", "error": str(e)}
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        return {"status": "FAILED", "error": "TIMEOUT"}

    async def _fetch_mp4_via_browser_fetch(self, media_name):
        result = await self._page.evaluate(
            """async (mediaName) => {
                try {
                    const r = await fetch("https://aisandbox-pa.googleapis.com/v1/media/" + encodeURIComponent(mediaName));
                    if (!r.ok) return {ok:false, error:r.status};
                    const buf = await r.arrayBuffer();
                    let binary = "";
                    const bytes = new Uint8Array(buf);
                    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                    return {ok:true, b64:btoa(binary), ct:r.headers.get("content-type") || ""};
                } catch(e) { return {ok:false, error:e.message || "unknown"}; }
            }""",
            media_name,
        )
        if not result.get("ok"):
            log.warning(f"Browser-fetch MP4 failed for {media_name}: {result.get('error', 'unknown')}")
            return None
        data = base64.b64decode(result.get("b64", ""))
        log.info(f"Browser-fetch MP4 ok: {len(data)} bytes {result.get('ct')}")
        return data

    async def get_download_url(self, media_id):
        """Get video download URL via labs.google redirect endpoint."""
        redirect_url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
        try:
            result = await self._page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url, {method: "GET", headers: {"Accept": "*/*"}, redirect: "follow"});
                        if (r.ok) {
                            const ct = r.headers.get("content-type") || "";
                            if (ct.includes("json")) return {type: "json", data: await r.json()};
                            return {type: "redirect", url: r.url, size: r.headers.get("content-length")};
                        }
                        return {error: r.status, text: (await r.text()).substring(0, 500)};
                    } catch(e) { return {error: e.message}; }
                }""",
                redirect_url,
            )
            if result.get("type") == "redirect":
                return result.get("url")
            data = result.get("data")
            if isinstance(data, dict):
                extracted = self._extract(data)
                if isinstance(extracted, str):
                    return extracted
                if isinstance(extracted, dict):
                    return extracted.get("url")
            log.warning(f"Download URL failed: {json.dumps(result, ensure_ascii=False)[:500]}")
        except Exception as e:
            log.error(f"Download URL error: {e}")
        return None

    async def download_result(self, download_url, output_path):
        """Download video/image from URL to local file."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"Downloading to {output_path}...")
        try:
            r = await self._page.request.get(download_url)
            if not r.ok:
                log.error(f"Download failed HTTP {r.status}")
                return False
            content = await r.body()
            with open(output_path, "wb") as f:
                f.write(content)
            size_mb = len(content) / 1048576
            log.info(f"Downloaded: {output_path} ({size_mb:.1f} MB)")
            return True
        except Exception as e:
            log.error(f"Download error: {e}")
            return False

    async def download_video(self, generation_id, output_path):
        """Full download flow: get URL via tRPC then download file."""
        url = await self.get_download_url(generation_id)
        if url:
            return await self.download_result(url, output_path)
        log.info("tRPC URL failed, trying direct media fetch...")
        direct_url = f"https://aisandbox-pa.googleapis.com/v1/media/{generation_id}"
        return await self.download_result(direct_url, output_path)

    async def _get_or_create_project(self):
        """Get existing project or create new one (tRPC)."""
        result = await self._api("project.searchUserProjects", {}, "GET")
        data = self._extract(result) or {}
        projects = data.get("projects", [])
        if isinstance(projects, list) and projects:
            pid = projects[0].get("id") or projects[0].get("projectId")
            log.info(f"Using existing project: {pid}")
            return pid
        log.info("No existing project found")
        return str(uuid.uuid4())
