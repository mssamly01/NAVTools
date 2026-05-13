"""NAV TOOLS — Task and TaskItem data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskItem:
    """A single prompt/generation item inside a task."""

    id: int = 0
    task_id: int = 0
    prompt: str = ""
    status: str = "PENDING"
    output_path: str = ""
    error: str = ""
    generation_id: str = ""
    retry_count: int = 0


@dataclass
class VideoTask:
    """A batch task containing multiple items to generate."""

    id: int = 0
    name: str = ""
    mode: str = "video_plain"
    status: str = "PENDING"
    quality: str = "Veo 3.1 - Fast"
    aspect_ratio: str = "16:9"
    output_dir: str = ""
    items: list[TaskItem] = field(default_factory=list)
    account_id: Optional[int] = None
