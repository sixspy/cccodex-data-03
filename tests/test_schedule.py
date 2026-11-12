"""钟点区间与待补跑规则测试。

所有时刻都通过 freeze_clock 固定服务端时钟，不依赖执行环境或浏览器时钟。
任务统一使用区间 [2, 4)，即服务端 2:00–3:59 在区间内，其余钟点在区间外。
"""

import sqlite3
from datetime import datetime

import pytest

from app.db import get_db
from app.services.runner import RunRejected, run_job
from app.services.schedule import unlock_today

DAY = datetime(2026, 11, 12)
OUTSIDE = datetime(2026, 11, 12, 1, 0)      # 1 点，区间外
INSIDE = datetime(2026, 11, 12, 3, 0)       # 3 点，区间内 [2, 4)
NEXT_DAY = datetime(2026, 11, 13, 1, 0)


def create_windowed_job(app, seed="/mock/catalog/beta", start=2, end=4):
    with app.app_context():
        db = get_db()
        db.execute(
            """
            INSERT INTO crawl_jobs (name, seed_path, status, start_hour, end_hour)
            VALUES ('j', ?, 'draft', ?, ?)
            """,
            (seed, start, end),
        )
        job_id = db.execute("SELECT id FROM crawl_jobs LIMIT 1").fetchone()["id"]
        db.commit()
        return job_id


def counts(app, job_id):
    with app.app_context():
        db = get_db()
        records = db.execute(
            "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        pending = db.execute(
            "SELECT COUNT(*) AS c FROM crawl_pending WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        fails = db.execute(
            "SELECT fail_count FROM job_daily_state WHERE job_id = ? AND day = ?",
            (job_id, DAY.date().isoformat()),
        ).fetchone()
        return records, pending, (fails["fail_count"] if fails else 0)


# 1. 区间外不入库并进入待补跑，原因写明「不在区间」
def test_outside_window_never_writes_official_records(app, freeze_clock):
    job_id = create_windowed_job(app)
    freeze_clock(OUTSIDE)
    with app.app_context():
        summary = run_job(job_id)

    assert summary["status"] == "outside_window"
    assert summary["inserted"] == 0
    records, pending, fails = counts(app, job_id)
    assert records == 0
    assert pending == 2  # beta 目录两个编码全部进入待补跑
    assert fails == 1    # 区间外触发计入当天次数
    with app.app_context():
        row = get_db().execute(
            "SELECT reason FROM crawl_pending WHERE job_id = ? LIMIT 1", (job_id,)
        ).fetchone()
        assert row["reason"] == "outside_window"
        job = get_db().execute(
            "SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job["status"] == "blocked"


# 2. 区间内只消化待补跑，不重新抓整个目录
def test_inside_window_digests_only_pending(app, freeze_clock):
    job_id = create_windowed_job(app)
    freeze_clock(OUTSIDE)
    with app.app_context():
        run_job(job_id)
    freeze_clock(INSIDE)
    with app.app_context():
        summary = run_job(job_id)

    assert summary["status"] == "done"
    assert summary["inserted"] == 2
    assert summary["digested_pending"] == 2
    records, pending, _ = counts(app, job_id)
    assert records == 2
    assert pending == 0

    # 区间内再次运行：待补跑已空，不会重新抓取目录，正式记录数保持 2
    with app.app_context():
        again = run_job(job_id)
    assert again["inserted"] == 0
    assert again["digested_pending"] == 0
    records, pending, _ = counts(app, job_id)
    assert records == 2
    assert pending == 0


# 3. 同一待补跑不会重复入库（已有正式记录时只清队列，靠 UNIQUE 与存在性双保险）
def test_same_pending_item_never_inserted_twice(app, freeze_clock):
    job_id = create_windowed_job(app)
    freeze_clock(OUTSIDE)
    with app.app_context():
        run_job(job_id)

    # 人为预置一条与待补跑同键的正式记录，模拟已入库状态
    with app.app_context():
        db = get_db()
        p = db.execute(
            "SELECT item_key, title, payload_json FROM crawl_pending WHERE job_id = ? LIMIT 1",
            (job_id,),
        ).fetchone()
        db.execute(
            "INSERT INTO crawl_records (job_id, item_key, title, payload_json) VALUES (?, ?, ?, ?)",
            (job_id, p["item_key"], p["title"], p["payload_json"]),
        )
        db.commit()

    freeze_clock(INSIDE)
    with app.app_context():
        summary = run_job(job_id)
    records, pending, _ = counts(app, job_id)
    assert pending == 0                      # 待补跑条目被消化清空
    assert records == 2                      # 预置 1 条 + 新写入 1 条，无重复
    assert summary["inserted"] == 1
    assert summary["skipped_dup"] == 1

    # 直接用重复键插入必须被数据库 UNIQUE 约束拒绝
    with app.app_context(), pytest.raises(sqlite3.IntegrityError):
        get_db().execute(
            "INSERT INTO crawl_records (job_id, item_key, title, payload_json) VALUES (?, ?, ?, '{}')",
            (job_id, p["item_key"], "dup"),
        )


# 4. 当天第三次之后被拒绝（区间外累计 3 次即锁定，第 4 次触发不产生任何动作）
def test_third_failure_locks_the_day(app, freeze_clock):
    job_id = create_windowed_job(app)
    freeze_clock(OUTSIDE)
    with app.app_context():
        for _ in range(3):
            run_job(job_id)
    _, pending, fails = counts(app, job_id)
    assert fails == 3
    assert pending == 2  # 后两次没有重复登记新编码

    with app.app_context(), pytest.raises(RunRejected) as exc:
        run_job(job_id)
    assert "上限" in str(exc.value)

    # 被拒绝后状态不改变，也没有新增待补跑或正式记录
    records, pending_after, fails_after = counts(app, job_id)
    assert records == 0
    assert pending_after == pending
    assert fails_after == 3


# 5. 空白原因不能解除；填写非空原因后当天可继续
def test_blank_unlock_reason_rejected(app, client, login, freeze_clock):
    login()
    job_id = create_windowed_job(app)
    freeze_clock(OUTSIDE)
    with app.app_context():
        for _ in range(3):
            run_job(job_id)

    # 空字符串与纯空白都不行
    for blank in ("", "   "):
        r = client.post(
            f"/jobs/{job_id}/unlock",
            data={"reason": blank},
            follow_redirects=True,
        )
        assert "解除原因不能为空白" in r.get_data(as_text=True)

    _, _, fails = counts(app, job_id)
    assert fails == 3  # 未被解除

    # 非空原因解除后，当天失败计数清零，可再次触发
    r = client.post(
        f"/jobs/{job_id}/unlock",
        data={"reason": "源站临时故障已恢复"},
        follow_redirects=True,
    )
    assert "已解除当天上限" in r.get_data(as_text=True)
    _, _, fails = counts(app, job_id)
    assert fails == 0
    with app.app_context():
        summary = run_job(job_id)
    assert summary["status"] == "outside_window"


# 6. 运行尚未结束时拒绝再次触发
def test_running_job_rejects_second_trigger(app, freeze_clock):
    job_id = create_windowed_job(app)
    freeze_clock(INSIDE)
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE crawl_jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        db.commit()
        with pytest.raises(RunRejected) as exc:
            run_job(job_id)
        assert "already running" in str(exc.value)

        # 守卫优先于上限判断：当天计数不增加，待补跑/正式记录均无变化
        records, pending, fails = counts(app, job_id)
        assert (records, pending, fails) == (0, 0, 0)


# 7. 解除只对当天有效：跨天计数重新从零开始，旧解除记录不延续
def test_unlock_and_failure_count_are_scoped_per_day(app, freeze_clock):
    job_id = create_windowed_job(app)
    freeze_clock(OUTSIDE)
    with app.app_context():
        for _ in range(3):
            run_job(job_id)
    with app.app_context():
        unlock_today(get_db(), job_id, "当天解除", OUTSIDE)
        get_db().commit()

    # 次日 1 点：新的一天计数为 0，可正常触发（进入待补跑），无需再次解除
    freeze_clock(NEXT_DAY)
    with app.app_context():
        summary = run_job(job_id)
    assert summary["status"] == "outside_window"
    with app.app_context():
        row = get_db().execute(
            "SELECT fail_count FROM job_daily_state WHERE job_id = ? AND day = ?",
            (job_id, NEXT_DAY.date().isoformat()),
        ).fetchone()
        assert row["fail_count"] == 1


# 8. 页面可见性：列表与详情展示区间、待补跑条数、已达上限（浏览器只读取服务端渲染结果）
def test_pages_show_window_pending_and_limit(app, client, login, freeze_clock):
    login()
    job_id = create_windowed_job(app)
    freeze_clock(OUTSIDE)
    with app.app_context():
        for _ in range(3):
            run_job(job_id)

    list_html = client.get("/").get_data(as_text=True)
    assert "02:00" in list_html and "04:00" in list_html
    assert "2 条待补跑" in list_html
    assert "已达上限" in list_html
    assert "不在区间" not in list_html  # 原因明细在详情页展示

    detail_html = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "不在区间" in detail_html
    assert "已达" in detail_html and "上限" in detail_html
    assert "解除原因" in detail_html
    # 页面展示的是固定后的服务端时刻，而非浏览器本地时间
    assert "2026-11-12 01:00" in detail_html

    # 区间内时列表应显示「在区间内」
    freeze_clock(INSIDE)
    inside_html = client.get("/").get_data(as_text=True)
    assert "在区间内" in inside_html


# 9. 创建任务的服务端校验：钟点必须为 0-23 且结束晚于起始
def test_create_job_hour_validation(app, client, login):
    login()
    base = {"name": "x", "seed_path": "/mock/catalog/beta"}

    r = client.post("/jobs", data={**base, "start_hour": "10", "end_hour": "10"},
                    follow_redirects=True)
    assert "结束钟点必须晚于起始钟点" in r.get_data(as_text=True)

    r = client.post("/jobs", data={**base, "start_hour": "-1", "end_hour": "5"},
                    follow_redirects=True)
    assert "0-23" in r.get_data(as_text=True)

    r = client.post("/jobs", data={**base, "start_hour": "20", "end_hour": "24"},
                    follow_redirects=True)
    assert "0-23" in r.get_data(as_text=True)

    r = client.post("/jobs", data={**base, "start_hour": "2", "end_hour": "4"},
                    follow_redirects=True)
    assert "任务已创建" in r.get_data(as_text=True)
    with app.app_context():
        row = get_db().execute(
            "SELECT start_hour, end_hour FROM crawl_jobs WHERE name = 'x'"
        ).fetchone()
        assert (row["start_hour"], row["end_hour"]) == (2, 4)
