"""每日钟点区间、待补跑与当天失败上限的领域规则。"""

from datetime import datetime

from app.db import db_transaction

# 同一任务当天因「不在区间」或「执行失败」累计达到该次数后，当天禁止再补跑，
# 必须填写非空解除原因（仅当天有效）。
MAX_DAILY_FAILURES = 3

# 待补跑原因码
REASON_OUTSIDE_WINDOW = "outside_window"

PENDING_REASON_TEXT = {
    REASON_OUTSIDE_WINDOW: "不在区间",
    "run_failed": "执行失败",
}


def is_in_window(job, now: datetime) -> bool:
    """半开区间 [start_hour, end_hour)：起始整点允许，结束整点即视为区间外。"""
    return job["start_hour"] <= now.hour < job["end_hour"]


def ensure_daily_state(db, job_id: int, day: str):
    db.execute(
        "INSERT OR IGNORE INTO job_daily_state (job_id, day) VALUES (?, ?)",
        (job_id, day),
    )
    return db.execute(
        "SELECT * FROM job_daily_state WHERE job_id = ? AND day = ?",
        (job_id, day),
    ).fetchone()


def bump_failure(tx, job_id: int, day: str) -> None:
    tx.execute(
        """
        INSERT INTO job_daily_state (job_id, day, fail_count)
        VALUES (?, ?, 1)
        ON CONFLICT(job_id, day) DO UPDATE SET fail_count = fail_count + 1
        """,
        (job_id, day),
    )


def unlock_today(db, job_id: int, reason: str, now: datetime) -> None:
    """填写解除原因后重置当天失败计数。原因空白一律拒绝；记录按天隔离，只对当天有效。"""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("解除原因不能为空白")
    day = now.date().isoformat()
    ensure_daily_state(db, job_id, day)
    with db_transaction() as tx:
        tx.execute(
            """
            UPDATE job_daily_state
               SET fail_count = 0,
                   unlock_reason = ?,
                   unlocked_at = ?
             WHERE job_id = ? AND day = ?
            """,
            (reason, now.isoformat(sep=" ", timespec="seconds"), job_id, day),
        )
