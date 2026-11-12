import json
import sqlite3

from flask import current_app

from app.db import db_transaction, get_db
from app.services.clock import server_now
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page
from app.services.schedule import (
    MAX_DAILY_FAILURES,
    REASON_OUTSIDE_WINDOW,
    bump_failure,
    ensure_daily_state,
    is_in_window,
)


class RunRejected(Exception):
    """触发被服务端规则拒绝（运行中、当天已达上限等），本次不产生任何采集动作。"""


def run_job(job_id: int) -> dict:
    now = server_now()
    day = now.date().isoformat()
    db = get_db()
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError("job not found")

    # 同一次运行还没结束，拒绝再次触发（服务端状态机守卫，前端时钟无法绕过）
    if job["status"] == "running":
        raise RunRejected("job already running")

    daily = ensure_daily_state(db, job_id, day)
    if daily["fail_count"] >= MAX_DAILY_FAILURES:
        raise RunRejected(
            f"当天失败/越界已达 {MAX_DAILY_FAILURES} 次上限，请填写解除原因后再试"
        )

    seed = job["seed_path"]
    if seed not in CATALOGS:
        _mark_failed(job_id, day, "unknown seed")
        return {"status": "failed", "reason": "unknown seed", "inserted": 0, "skipped_dup": 0}

    try:
        html = render_catalog_page(seed)
        if isinstance(html, tuple):
            html = html[0]
        items = parse_catalog_html(html, seed)
    except Exception as e:  # 抓取/解析异常计入当天失败次数
        _mark_failed(job_id, day, str(e))
        raise

    if is_in_window(job, now):
        # 区间内只消化待补跑，绝不重新抓整个目录冒充补跑
        return _digest_pending(job_id, day, now)
    return _collect_pending(job_id, day, now, items)


def _collect_pending(job_id: int, day: str, now, items: list[dict]) -> dict:
    """区间外触发：本次见到的编码进入待补跑并注明原因，不写入正式结果。"""
    inserted_pending = 0
    skipped_pending = 0
    try:
        with db_transaction() as tx:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
                (job_id,),
            )
            for it in items:
                key = normalize_item_key(it["seed_path"], it["title"])
                try:
                    tx.execute(
                        """
                        INSERT INTO crawl_pending (job_id, item_key, title, payload_json, reason)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (job_id, key, it["title"], json.dumps(it, ensure_ascii=False),
                         REASON_OUTSIDE_WINDOW),
                    )
                    inserted_pending += 1
                except sqlite3.IntegrityError:
                    # 同一编码已在待补跑队列，不重复登记
                    skipped_pending += 1
            bump_failure(tx, job_id, day)
            tx.execute(
                "UPDATE crawl_jobs SET status = 'blocked', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
    except Exception as e:
        _mark_failed(job_id, day, str(e))
        raise

    current_app.logger.info(
        "job %s outside window at %s, pending +%s (skipped %s), no official record written",
        job_id, now.hour, inserted_pending, skipped_pending,
    )
    return {
        "status": "outside_window",
        "reason": "不在区间",
        "hour": now.hour,
        "inserted": 0,
        "inserted_pending": inserted_pending,
        "skipped_pending": skipped_pending,
    }


def _digest_pending(job_id: int, day: str, now) -> dict:
    """区间内补跑：仅处理尚未入库的待补跑编码，来源只有 crawl_pending 表。"""
    db = get_db()
    pending_rows = db.execute(
        "SELECT * FROM crawl_pending WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()

    inserted = 0
    skipped_dup = 0
    digested_pending = 0
    with db_transaction() as tx:
        tx.execute(
            "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        for row in pending_rows:
            already = tx.execute(
                "SELECT 1 FROM crawl_records WHERE job_id = ? AND item_key = ?",
                (job_id, row["item_key"]),
            ).fetchone()
            if already:
                # 同一待补跑不会重复入库：已存在正式记录时仅清掉待补跑条目
                tx.execute(
                    "DELETE FROM crawl_pending WHERE id = ?", (row["id"],)
                )
                digested_pending += 1
                skipped_dup += 1
                continue
            try:
                tx.execute(
                    """
                    INSERT INTO crawl_records (job_id, item_key, title, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, row["item_key"], row["title"], row["payload_json"]),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1
            tx.execute(
                "DELETE FROM crawl_pending WHERE id = ?", (row["id"],)
            )
            digested_pending += 1
        tx.execute(
            "UPDATE crawl_jobs SET status = 'done', finished_at = datetime('now') WHERE id = ?",
            (job_id,),
        )

    current_app.logger.info(
        "job %s catch-up in window at %s, inserted=%s skipped_dup=%s digested_pending=%s",
        job_id, now.hour, inserted, skipped_dup, digested_pending,
    )
    return {
        "status": "done",
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "digested_pending": digested_pending,
        "pending_left": get_db().execute(
            "SELECT COUNT(*) AS c FROM crawl_pending WHERE job_id = ?", (job_id,)
        ).fetchone()["c"],
    }


def _mark_failed(job_id: int, day: str, reason: str) -> None:
    with db_transaction() as tx:
        bump_failure(tx, job_id, day)
        tx.execute(
            "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
    current_app.logger.warning("job %s failed: %s", job_id, reason)
