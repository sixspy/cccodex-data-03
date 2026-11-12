import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                start_hour INTEGER NOT NULL DEFAULT 0,
                end_hour INTEGER NOT NULL DEFAULT 24,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS crawl_pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS job_daily_state (
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                day TEXT NOT NULL,
                fail_count INTEGER NOT NULL DEFAULT 0,
                unlock_reason TEXT,
                unlocked_at TEXT,
                PRIMARY KEY (job_id, day)
            );
            """
        )
        # 轻量迁移：为旧库补齐钟点区间列（新库建表时已包含）
        legacy_cols = {row["name"] for row in db.execute("PRAGMA table_info(crawl_jobs)")}
        if "start_hour" not in legacy_cols:
            db.execute("ALTER TABLE crawl_jobs ADD COLUMN start_hour INTEGER NOT NULL DEFAULT 0")
        if "end_hour" not in legacy_cols:
            db.execute("ALTER TABLE crawl_jobs ADD COLUMN end_hour INTEGER NOT NULL DEFAULT 24")
        db.commit()
