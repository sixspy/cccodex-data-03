from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.routes.auth import login_required
from app.services.clock import server_now
from app.services.mock_catalog import CATALOGS
from app.services.runner import RunRejected, run_job
from app.services.schedule import (
    MAX_DAILY_FAILURES,
    PENDING_REASON_TEXT,
    ensure_daily_state,
    is_in_window,
    unlock_today,
)

bp = Blueprint("jobs", __name__)


def _parse_hour(raw: str, field: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}必须是 0-23 的整数")
    if not 0 <= value <= 23:
        raise ValueError(f"{field}必须是 0-23 的整数")
    return value


@bp.route("/")
@login_required
def list_jobs():
    now = server_now()
    today = now.date().isoformat()
    db = get_db()
    jobs = db.execute(
        "SELECT * FROM crawl_jobs ORDER BY id DESC"
    ).fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    pending_counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_pending GROUP BY job_id"
        )
    }
    daily_rows = {
        row["job_id"]: row
        for row in db.execute(
            "SELECT * FROM job_daily_state WHERE day = ?", (today,)
        )
    }
    in_window_map = {job["id"]: is_in_window(job, now) for job in jobs}
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        pending_counts=pending_counts,
        daily_rows=daily_rows,
        in_window_map=in_window_map,
        max_daily_failures=MAX_DAILY_FAILURES,
        server_now=now,
        seeds=sorted(CATALOGS.keys()),
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    try:
        start_hour = _parse_hour(request.form.get("start_hour", ""), "起始钟点")
        end_hour = _parse_hour(request.form.get("end_hour", ""), "结束钟点")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("jobs.list_jobs"))
    if end_hour <= start_hour:
        flash("结束钟点必须晚于起始钟点", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    db.execute(
        """
        INSERT INTO crawl_jobs (name, seed_path, status, start_hour, end_hour)
        VALUES (?, ?, 'draft', ?, ?)
        """,
        (name, seed, start_hour, end_hour),
    )
    db.commit()
    flash(
        f"任务已创建，每日可运行区间 {start_hour:02d}:00–{end_hour:02d}:00（服务端时间，半开区间）",
        "ok",
    )
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
    except RunRejected as e:
        # 服务端拒绝：运行中、当天已达上限等
        flash(str(e), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    except Exception as e:
        flash(f"运行失败: {e}", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))

    if summary["status"] == "outside_window":
        flash(
            f"当前服务端钟点 {summary['hour']} 点不在任务区间内：未写入正式结果，"
            f"本次 {summary['inserted_pending']} 条编码已进入待补跑（原因：不在区间）",
            "error",
        )
    elif summary["status"] == "failed":
        flash(f"运行失败：{summary.get('reason', '未知原因')}，已计入当天失败次数", "error")
    else:
        flash(
            f"区间内补跑完成：正式写入 {summary['inserted']} 条，"
            f"去重跳过 {summary['skipped_dup']} 条，剩余待补跑 {summary['pending_left']} 条",
            "ok",
        )
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/unlock")
@login_required
def unlock_view(job_id: int):
    reason = request.form.get("reason", "")
    db = get_db()
    job = db.execute("SELECT id FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    try:
        unlock_today(db, job_id, reason, server_now())
        db.commit()
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    flash("已解除当天上限（仅当天有效），请在区间内再次运行以消化待补跑", "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    now = server_now()
    today = now.date().isoformat()
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    pending = db.execute(
        "SELECT * FROM crawl_pending WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    daily = ensure_daily_state(db, job_id, today)
    locked = daily["fail_count"] >= MAX_DAILY_FAILURES
    return render_template(
        "job_detail.html",
        job=job,
        records=records,
        pending=pending,
        daily=daily,
        locked=locked,
        in_window=is_in_window(job, now),
        max_daily_failures=MAX_DAILY_FAILURES,
        reason_text=PENDING_REASON_TEXT,
        server_now=now,
    )
