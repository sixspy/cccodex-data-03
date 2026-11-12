"""服务端时钟。

区间判断只能使用服务端时刻：页面渲染与 runner 都通过本模块取时间，
测试通过 ``app.config["FIXED_NOW"]`` 注入固定时刻，浏览器时钟完全不参与判断。
"""

from datetime import datetime

from flask import current_app, has_app_context


def server_now() -> datetime:
    if has_app_context():
        fixed = current_app.config.get("FIXED_NOW")
        if isinstance(fixed, datetime):
            return fixed
    return datetime.now()
