"""Background task manager with full BrowserManager and FlowClient integration."""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from utils.logger import log


class WorkerSignals(QObject):
    task_started = Signal(object)
    task_completed = Signal(object)
    task_error = Signal(object, str)
    task_progress = Signal(object, int, int)
    item_status_changed = Signal(object, str)
    item_completed = Signal(object, str)
    item_error = Signal(object, str)
    credit_updated = Signal(object, int)
    account_disabled = Signal(object, str, str)


class AccountPool:
    def __init__(self, db):
        self.db = db
        self._busy = set()

    def acquire(self):
        try:
            accounts = self.db.get_accounts(enabled_only=True) if self.db else []
            for account in accounts:
                if account.id not in self._busy:
                    self._busy.add(account.id)
                    return account
        except Exception as e:
            log.error(f"AccountPool: acquire error — {e}")
        return None

    def release(self, account):
        if account:
            self._busy.discard(account.id)

    def available_count(self):
        try:
            return len([a for a in self.db.get_accounts(enabled_only=True) if a.id not in self._busy])
        except Exception:
            return 0


class TaskWorker(QThread):
    def __init__(self, task, db=None, browser_manager=None, account_pool=None, parent=None):
        super().__init__(parent)
        self.task = task
        self.db = db
        self.browser_manager = browser_manager
        self.account_pool = account_pool
        self.signals = WorkerSignals()
        self._cancelled = False
        self._paused = False
        self._recaptcha_provider = None

    def cancel(self):
        self._cancelled = True

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def run(self):
        """Thread entry point: setup event loop and execute."""
        try:
            asyncio.run(self._execute())
        except Exception as e:
            log.error(f"TaskWorker: fatal error — {e}")
            self.signals.task_error.emit(0, str(e))

    async def _execute(self):
        if not self.account_pool:
            self.signals.task_error.emit(0, "Account pool not initialized")
            return

        account = self.account_pool.acquire()
        if not account:
            self.signals.task_error.emit(0, "Không có tài khoản khả dụng (đang bận hoặc chưa bật)")
            return

        log.info(f"TaskWorker: using account {account.email}")
        task_id = id(self)
        self.signals.task_started.emit(task_id)

        try:
            await self._process_task(account, task_id)
        except Exception as e:
            log.error(f"TaskWorker: task {task_id} failed — {e}")
            self.signals.task_error.emit(task_id, str(e))
        finally:
            self.account_pool.release(account)
            if self.browser_manager:
                try:
                    await self.browser_manager.close_context(account.id)
                except Exception:
                    pass
            if self._recaptcha_provider:
                try:
                    await self._recaptcha_provider.stop()
                except Exception:
                    pass
            self.signals.task_completed.emit(task_id)

    async def _process_task(self, account, task_id):
        from services.flow_client import FlowClient

        if not self.browser_manager:
            raise RuntimeError("BrowserManager not initialized")

        config = self.task if isinstance(self.task, dict) else {}
        prompts = config.get("prompts", [])
        mode = config.get("mode", "video_plain")
        output_folder = config.get("output_folder") or str(Path.home() / ".vidgen" / "output")
        total = len(prompts)

        is_video = "video" in mode
        url = "https://labs.google/fx/tools/video-fx" if is_video else "https://labs.google/fx/tools/image-fx"

        page = await self.browser_manager.get_page(
            account_id=account.id,
            email=account.email,
            proxy=account.proxy,
            cookie_path=account.cookie_path,
            url=url,
        )

        client = FlowClient(page, cookie_path=account.cookie_path, account_email=account.email)

        # Initialize reCAPTCHA provider
        try:
            from automation.recaptcha_provider import SubprocessTokenProvider
            cookies = await page.context.cookies()
            self._recaptcha_provider = SubprocessTokenProvider()
            await self._recaptcha_provider.start(cookies)
            client.set_recaptcha_provider(self._recaptcha_provider)
        except Exception as e:
            log.warning(f"TaskWorker: SubprocessTokenProvider failed to start (will use fallback): {e}")

        output_dir = Path(output_folder)
        output_dir.mkdir(parents=True, exist_ok=True)

        char_images = config.get("character_images", {})
        per_row_images = config.get("per_row_character_images", {})

        for i, prompt_text in enumerate(prompts):
            if self._cancelled:
                break
            while self._paused and not self._cancelled:
                await asyncio.sleep(0.5)

            item_id = i + 1
            self.signals.item_status_changed.emit(item_id, "RUNNING")
            log.info(f"TaskWorker: processing prompt {item_id}/{total}")

            try:
                row_imgs = per_row_images.get(i, {}) or char_images
                image_paths = [v for v in row_imgs.values() if v and Path(v).exists()]

                if is_video:
                    gen_id = await client.generate_video(
                        prompt=prompt_text,
                        image_paths=image_paths or None,
                        model=config.get("model", "veo-3.1-fast"),
                        aspect_ratio=config.get("aspect_ratio", "16:9"),
                    )
                    out_file = output_dir / f"video_{int(time.time())}_{i+1}.mp4"
                    # In a real impl, we'd poll here. For now, assume gen_id is enough to signal success or we poll.
                    # Simplified poll logic:
                    res = await client.wait_for_completion(gen_id, cancel_check=lambda: self._cancelled)
                    if res.get("status") == "COMPLETED":
                        await client.download_video(gen_id, str(out_file))
                        self.signals.item_completed.emit(item_id, str(out_file))
                    else:
                        raise RuntimeError(f"Generation failed: {res.get('error')}")
                else:
                    result = await client.generate_image(
                        prompt=prompt_text,
                        image_paths=image_paths or None,
                        model=config.get("model", "Nano Banana 2"),
                        aspect_ratio=config.get("aspect_ratio", "1:1"),
                    )
                    out_file = output_dir / f"image_{int(time.time())}_{i+1}.png"
                    
                    # Image generation usually returns bytes or URL immediately
                    image_data = result.get("encodedImage") or result.get("imageBytes")
                    if image_data:
                        with open(out_file, "wb") as f:
                            f.write(base64.b64decode(image_data))
                        self.signals.item_completed.emit(item_id, str(out_file))
                    else:
                        url = result.get("url") or result.get("uri")
                        if url:
                            await client.download_result(url, str(out_file))
                            self.signals.item_completed.emit(item_id, str(out_file))
                        else:
                            raise RuntimeError("No image data returned")

                if self.db and client._last_remaining_credits is not None:
                    self.db.update_account_credit(account.id, client._last_remaining_credits)
                    self.signals.credit_updated.emit(account.id, client._last_remaining_credits)

            except Exception as e:
                log.error(f"TaskWorker: prompt {item_id} failed — {e}")
                self.signals.item_error.emit(item_id, str(e))

            self.signals.task_progress.emit(task_id, i + 1, total)


class TaskManager(QObject):
    def __init__(self, db=None, browser_manager=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.browser_manager = browser_manager
        self.account_pool = AccountPool(db)
        self.workers = {}

    def start_task(self, task):
        worker = TaskWorker(task, self.db, self.browser_manager, self.account_pool)
        task_id = id(worker)
        self.workers[task_id] = worker
        worker.finished.connect(lambda: self.workers.pop(task_id, None))
        worker.start()
        return worker

    def cancel_all(self):
        for worker in list(self.workers.values()):
            worker.cancel()

    stop_all = cancel_all

    def active_tasks(self):
        return list(self.workers.keys())

    def available_accounts(self):
        return self.account_pool.available_count()
