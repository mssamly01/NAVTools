"""NAV TOOLS — Account data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Account:
    """Google account used for Flow API access."""

    id: int = 0
    email: str = ""
    proxy: Optional[str] = None
    cookie_path: str = ""
    cookie_exp: Optional[datetime] = None
    tier: str = "FREE"
    credit: int = 0
    enabled: bool = True
    gemini_api_key: str = ""
    token_exp: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> Account:
        """Create an Account from a database row tuple.

        Expected column order:
          id, email, proxy, cookie_path, cookie_exp, tier, credit, enabled, gemini_api_key
        """
        vals = list(row) + [None] * max(0, 9 - len(row))
        cookie_exp = None
        if vals[4]:
            try:
                cookie_exp = datetime.fromisoformat(str(vals[4]))
            except (ValueError, TypeError):
                pass
        return cls(
            id=int(vals[0] or 0),
            email=str(vals[1] or ""),
            proxy=vals[2] or None,
            cookie_path=str(vals[3] or ""),
            cookie_exp=cookie_exp,
            tier=str(vals[5] or "FREE"),
            credit=int(vals[6] or 0),
            enabled=bool(int(vals[7] or 1)),
            gemini_api_key=str(vals[8] or ""),
        )
