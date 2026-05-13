"""NAV TOOLS — SQLite database layer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from config.constants import DB_PATH
from models.account import Account
from utils.logger import log


class Database:
    """Thin wrapper around an SQLite connection with helpers for
    settings and Google account management.
    """

    def __init__(self, db_path: str | Path | None = None):
        self._path = str(db_path or DB_PATH)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()
        return self._conn

    def _migrate(self):
        """Handle schema updates for existing databases."""
        try:
            # Check if token_exp exists
            cursor = self._conn.execute("PRAGMA table_info(accounts)")
            columns = [row[1] for row in cursor.fetchall()]
            if "token_exp" not in columns:
                self._conn.execute("ALTER TABLE accounts ADD COLUMN token_exp TEXT")
                self._conn.commit()
                log.info("Migration: Added token_exp column to accounts table")
        except Exception as e:
            log.warning(f"Migration failed: {e}")

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @property
    def conn(self) -> Optional[sqlite3.Connection]:
        return self._conn

    # ------------------------------------------------------------------
    # Raw SQL pass-through (used by Settings and LoginScreen)
    # ------------------------------------------------------------------

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def commit(self):
        self._conn.commit()

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]

    def set_setting(self, key: str, value: Any):
        serialized = json.dumps(value)
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, serialized),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Account CRUD
    # ------------------------------------------------------------------

    def get_accounts(self, enabled_only: bool = False) -> list[Account]:
        sql = "SELECT id, email, proxy, cookie_path, cookie_exp, tier, credit, enabled, gemini_api_key, token_exp FROM accounts"
        if enabled_only:
            sql += " WHERE enabled = 1"
        rows = self._conn.execute(sql).fetchall()
        return [Account.from_row(r) for r in rows]

    def get_account(self, account_id: int) -> Optional[Account]:
        row = self._conn.execute(
            "SELECT id, email, proxy, cookie_path, cookie_exp, tier, credit, enabled, gemini_api_key, token_exp FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        return Account.from_row(row) if row else None

    def add_account(self, email: str) -> Account:
        """Create a new account with default values."""
        cursor = self._conn.execute(
            "INSERT INTO accounts (email) VALUES (?)", (email,)
        )
        self._conn.commit()
        return Account(id=cursor.lastrowid, email=email)

    def update_account(self, account: Account):
        cookie_exp_str = account.cookie_exp.isoformat() if account.cookie_exp else None
        token_exp_str = account.token_exp.isoformat() if account.token_exp else None
        self._conn.execute(
            """INSERT OR REPLACE INTO accounts
               (id, email, proxy, cookie_path, cookie_exp, tier, credit, enabled, gemini_api_key, token_exp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account.id or None,
                account.email,
                account.proxy,
                account.cookie_path,
                cookie_exp_str,
                account.tier,
                account.credit,
                int(account.enabled),
                account.gemini_api_key,
                token_exp_str,
            ),
        )
        self._conn.commit()

    def update_account_credit(self, account_id: int, credit: int):
        self._conn.execute(
            "UPDATE accounts SET credit = ? WHERE id = ?", (credit, account_id)
        )
        self._conn.commit()

    def delete_account(self, account_id: int):
        self._conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self):
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email           TEXT NOT NULL DEFAULT '',
                proxy           TEXT,
                cookie_path     TEXT DEFAULT '',
                cookie_exp      TEXT,
                tier            TEXT DEFAULT 'FREE',
                credit          INTEGER DEFAULT 0,
                enabled         INTEGER DEFAULT 1,
                gemini_api_key  TEXT DEFAULT '',
                token_exp       TEXT
            );
            """
        )
        self._conn.commit()
