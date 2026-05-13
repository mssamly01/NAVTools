"""Background task manager."""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal

from utils.logger import log


class WorkerSignals(QObject):
    task_started = Signal(int)
    task_completed = Signal(int)
    task_error = Signal(int, str)
    task_progress = Signal(int, int, int)
    item_status_changed = Signal(int, str)
    item_completed = Signal(int, str)
    item_error = Signal(int, str)
    credit_updated = Signal(int, int)
    account_disabled = Signal(int, str, str)


class AccountPool:
    def __init__(self, db):
        self.db = db
        self._busy = set()

    def acquire(self):
        accounts = self.db.get_accounts(enabled_only=True) if self.db else []
        for account in accounts:
            if account.id not in self._busy:
                self._busy.add(account.id)
                return account
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

    def _cancellable_sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self._cancelled:
                return False
            time.sleep(0.1)
        return True

    def cancel(self):
        self._cancelled = True

    def _schedule_close(self):
        return None

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def run(self):
        task = self.task
        task_id = id(task) if isinstance(task, dict) else getattr(task, "id", 0)
        try:
            asyncio.run(self._async_execute(task, task_id))
        except Exception as e:
            log.error(f"Task {task_id} error: {e}")
            self.signals.task_error.emit(task_id, str(e))

    async def _async_execute(self, task, task_id):
        self.signals.task_started.emit(task_id)

        if isinstance(task, dict):
            prompts = task.get("prompts", [])
            mode = task.get("mode", "image")
            output_folder = task.get("output_folder", "")
        else:
            prompts = getattr(task, "prompts", []) or []
            items = getattr(task, "items", None)
            if items and not prompts:
                prompts = [getattr(it, "prompt", "") for it in items if getattr(it, "prompt", "")]
            mode = getattr(task, "mode", "image")
            output_folder = getattr(task, "output_folder", "")

        if not prompts:
            self.signals.task_error.emit(task_id, "Không có prompt nào để xử lý")
            return

        total = len(prompts)
        account = self.account_pool.acquire() if self.account_pool else None
        if not account:
            self.signals.task_error.emit(task_id, "Không có tài khoản Google khả dụng. Hãy thêm và đăng nhập tài khoản trong Cài đặt.")
            return

        try:
            await self._process_prompts(task, prompts, account, mode, output_folder, task_id, total)
        finally:
            if self.account_pool:
                self.account_pool.release(account)
            if self.browser_manager:
                try:
                    await self.browser_manager.close_context(account.id)
                except Exception:
                    pass

        self.signals.task_completed.emit(task_id)

    async def _process_prompts(self, task, prompts, account, mode, output_folder, task_id, total):
        from services.flow_client import FlowClient

        if not self.browser_manager:
            self.signals.task_error.emit(task_id, "BrowserManager chưa được khởi tạo")
            return

        is_video = mode in ("video_plain", "char_video", "video_ref", "frame_video")
        url = "https://labs.google/fx/tools/video-fx" if is_video else "https://labs.google/fx/tools/image-fx"

        try:
            page = await self.browser_manager.get_page(
                account_id=account.id,
                email=account.email,
                proxy=account.proxy,
                cookie_path=account.cookie_path,
                url=url,
            )
        except Exception as e:
            self.signals.task_error.emit(task_id, f"Không thể kết nối trình duyệt: {e}")
            return

        client = FlowClient(page, cookie_path=account.cookie_path, account_email=account.email)

        config = task if isinstance(task, dict) else {}
        aspect_ratio = config.get("aspect_ratio", "16:9")
        quality = config.get("quality", "720p")
        model = config.get("model") or config.get("image_model") or ("veo-3.1-fast" if is_video else "Nano Banana 2")
        char_images = config.get("character_images", {})
        per_row_images = config.get("per_row_character_images", {})
        output_dir = Path(output_folder) if output_folder else Path.home() / ".vidgen" / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        for i, prompt_data in enumerate(prompts):
            if self._cancelled:
                break
            while self._paused and not self._cancelled:
                await asyncio.sleep(0.2)

            prompt_text = prompt_data if isinstance(prompt_data, str) else prompt_data.get("prompt", "")
            item_id = i + 1
            self.signals.item_status_changed.emit(item_id, "RUNNING")

            image_paths = []
            row_imgs = per_row_images.get(i, {}) if per_row_images else {}
            imgs = row_imgs or char_images
            if imgs:
                image_paths = [v for v in imgs.values() if v and Path(v).exists()]

            try:
                if is_video:
                    gen_id = await client.generate_video(
                        prompt=prompt_text,
                        image_paths=image_paths or None,
                        model=model,
                        aspect_ratio=aspect_ratio,
                        quality=quality,
                    )
                    if isinstance(gen_id, str):
                        out_file = output_dir / f"video_{i+1:03d}_{uuid.uuid4().hex[:6]}.mp4"
                        await client.download_video(gen_id, str(out_file))
                        self.signals.item_completed.emit(item_id, str(out_file))
                    else:
                        self.signals.item_completed.emit(item_id, "")
                else:
                    result = await client.generate_image(
                        prompt=prompt_text,
                        image_paths=image_paths or None,
                        model=model,
                        aspect_ratio=aspect_ratio,
                    )
                    out_file = output_dir / f"image_{i+1:03d}_{uuid.uuid4().hex[:6]}.png"
                    image_data = result.get("encodedImage") or result.get("imageBytes") or ""
                    if image_data:
                        out_file.parent.mkdir(parents=True, exist_ok=True)
                        with open(out_file, "wb") as f:
                            f.write(base64.b64decode(image_data))
                        self.signals.item_completed.emit(item_id, str(out_file))
                    else:
                        download_url = result.get("url") or result.get("uri") or ""
                        if download_url:
                            await client.download_result(download_url, str(out_file))
                            self.signals.item_completed.emit(item_id, str(out_file))
                        else:
                            self.signals.item_completed.emit(item_id, "")
                            log.warning(f"No image data in response for prompt {i+1}")

                credits = client._last_remaining_credits
                if credits is not None and self.db:
                    self.db.update_account_credit(account.id, credits)
                    self.signals.credit_updated.emit(account.id, credits)

            except Exception as e:
                log.error(f"Prompt {i+1} failed: {e}")
                self.signals.item_error.emit(item_id, str(e))

            self.signals.task_progress.emit(task_id, i + 1, total)


class UpscaleSignals(QObject):
    done = Signal(str)
    error = Signal(str)


class UpscaleRunnable(QRunnable):
    def __init__(self, image_path, output_path=None):
        super().__init__()
        self.image_path = image_path
        self.output_path = output_path
        self.signals = UpscaleSignals()

    def run(self):
        try:
            self._execute()
        except Exception as e:
            self.signals.error.emit(str(e))

    def _execute(self):
        self.signals.done.emit(str(self.output_path or self.image_path))


class TaskManager(QObject):
    def __init__(self, db=None, browser_manager=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.browser_manager = browser_manager
        self.account_pool = AccountPool(db)
        self.workers = {}
        self.thread_pool = QThreadPool.globalInstance()

    def start_task(self, task):
        worker = TaskWorker(task, self.db, self.browser_manager, self.account_pool)
        task_id = getattr(task, "id", id(worker))
        self.workers[task_id] = worker
        worker.finished.connect(lambda tid=task_id: self._on_task_done(tid))
        worker.start()
        return worker

    def _get_all_workers(self):
        return list(self.workers.values())

    def pause_task(self, task_id):
        worker = self.workers.get(task_id)
        if worker:
            worker.pause()

    def resume_task(self, task_id):
        worker = self.workers.get(task_id)
        if worker:
            worker.resume()

    def run_upscale(self, image_path, output_path=None):
        runnable = UpscaleRunnable(image_path, output_path)
        self.thread_pool.start(runnable)
        return runnable

    def cancel_task(self, task_id):
        worker = self.workers.get(task_id)
        if worker:
            worker.cancel()

    def cancel_all(self):
        for worker in self._get_all_workers():
            worker.cancel()

    stop_all = cancel_all
    stop_task = cancel_task

    def _on_task_done(self, task_id):
        self.workers.pop(task_id, None)

    def active_tasks(self):
        return list(self.workers)

    def available_accounts(self):
        return self.account_pool.available_count()
