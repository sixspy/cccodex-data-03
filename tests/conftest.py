import os
import tempfile
from datetime import datetime

import pytest

from app import create_app
from app.db import init_db


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    application = create_app()
    application.config["DATABASE"] = path
    application.config["TESTING"] = True
    with application.app_context():
        init_db(application)
    yield application
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def freeze_clock(app):
    """把服务端时钟固定在指定时刻（区间判断只认这个，浏览器时钟无法干预）。"""

    def _freeze(value: datetime):
        app.config["FIXED_NOW"] = value

    return _freeze


@pytest.fixture()
def login(client):
    def _login():
        return client.post(
            "/login",
            data={"username": "admin", "password": "123456"},
            follow_redirects=True,
        )

    return _login
