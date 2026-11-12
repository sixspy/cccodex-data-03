from datetime import datetime

from app.db import get_db
from app.services.runner import run_job


def test_mock_catalog_http(client):
    r = client.get("/mock/catalog/alpha")
    assert r.status_code == 200
    assert "市政管线测绘记录表" in r.get_data(as_text=True)


def test_run_job_dedup_duplicate_title(app, client, login, freeze_clock):
    """alpha 含重复标题：区间外登记待补跑去重一次，区间内补跑正式写入 2 条。"""
    login()
    client.post(
        "/jobs",
        data={
            "name": "t1",
            "seed_path": "/mock/catalog/alpha",
            "start_hour": "2",
            "end_hour": "4",
        },
        follow_redirects=True,
    )
    with app.app_context():
        job_id = get_db().execute("SELECT id FROM crawl_jobs LIMIT 1").fetchone()["id"]

        # 区间外（服务端钟点 1 点）：不入库，只进待补跑；重复标题在登记阶段去重
        freeze_clock(datetime(2026, 11, 12, 1, 0))
        outside = run_job(job_id)
        assert outside["status"] == "outside_window"
        assert outside["inserted_pending"] == 2
        assert outside["skipped_pending"] == 1
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?", (job_id,)
        ).fetchone()["c"] == 0

        # 区间内（3 点）：只消化待补跑，正式写入去重后的 2 条
        freeze_clock(datetime(2026, 11, 12, 3, 0))
        summary = run_job(job_id)
        assert summary["status"] == "done"
        assert summary["inserted"] == 2
        assert summary["skipped_dup"] == 0
        assert summary["digested_pending"] == 2
