# -*- coding: utf-8 -*-
"""测试数据库隔离守卫。

为什么需要
----------
``app.core.config.settings`` 与 ``app.core.db.engine`` 都是**模块级单例**，
引擎在**首次导入 app.core.db 时**就按当时的 ``DATABASE_URL`` 建好了。

危险后果：某个测试模块若没能在"别的模块导入 app 之前"设置好 ``DATABASE_URL``，
它就会连到**工作区的真实库** ``data/monitor.db``。本项目真出过事：
``tests/test_timeline.py`` 在开发库上跑了一轮演练，把 1200 条演示数据
清成了 8 条。

用法（必须在导入 app.* 之前调用）::

    from tests import dbguard
    dbguard.isolate("timeline")
    # 之后才可以 import app.*

它做三件事：
  1. 设置 ``APP_ENV_FILE`` 指向不存在的文件，避免读到工作区 ``.env``；
  2. 设置 ``DATABASE_URL`` 指向临时目录下的独立库；
  3. 若 app.core.db 已经被导入过（引擎已绑定），**直接替换引擎与 Session**，
     保证无论测试收集顺序如何都写不进真实库。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: 工作区真实库的绝对路径，用于自检与报错
WORKSPACE_DB = (Path(__file__).resolve().parent.parent / "data" / "monitor.db").resolve()

_ISOLATED: dict[str, str] = {}
#: 所有测试模块共用的隔离库路径（由 tests/__init__.py 在包导入时确定）
_SHARED_PATH: str | None = None


def _shared_db_path() -> str:
    """取共用隔离库路径。

    优先用 ``tests/__init__.py`` 在包导入时算好的路径 —— 那是最早的时机，
    不受"哪个测试模块先导入 app"影响。
    """
    from tests import TEST_DB_PATH

    return TEST_DB_PATH


def isolate(tag: str) -> str:
    """把数据库隔离到临时目录，返回库文件路径。

    所有测试模块**共用同一个隔离库**（首次调用决定），而不是每个 tag 一个库。
    原因：``app.core.db`` 的引擎只能绑定一个 URL —— 它在首次导入时就定下来了。
    若每个模块各建一个库，只有"首个导入 app.core.db 的模块"那个库会被真正使用，
    其余模块拿到的是引擎指向的另一个库（表可能不存在），
    表现为"单独跑通过、全套跑失败"。

    共用一库 + 各测试类在 setUp 里清表，是本项目验证过最简单可靠的方案。
    """
    global _SHARED_PATH

    if _SHARED_PATH is not None:
        _ISOLATED[tag] = _SHARED_PATH
        return _SHARED_PATH

    db_path = Path(_shared_db_path())
    url = f"sqlite:///{db_path}"

    # 再钉一次环境变量（tests/__init__.py 已设，这里防御性重设）
    os.environ["DATABASE_URL"] = url
    os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

    # 若 app.core.db 已经导入（引擎已绑定到别处），直接换掉。
    import sys

    db_module = sys.modules.get("app.core.db")
    if db_module is not None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(
            url, future=True, connect_args={"check_same_thread": False}
        )
        db_module.engine = engine
        db_module.SessionLocal = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False
        )
        cfg = sys.modules.get("app.core.config")
        if cfg is not None and hasattr(cfg, "settings"):
            cfg.settings.database_url = url

    _SHARED_PATH = str(db_path)
    _ISOLATED[tag] = _SHARED_PATH
    return _SHARED_PATH


def ensure_schema() -> None:
    """在隔离库上建表。

    在 ``setUpClass`` 里调用（那时 app.models 一定已导入，
    ``Base.metadata`` 才包含各表）。

    注意：不能用 ``app.core.db.init_db()`` —— 它内部按**模块级 engine 的闭包**
    建表，在引擎已被替换的情况下会把表建到别处（本项目的另一个老坑）。
    """
    import sys

    from sqlalchemy import create_engine, inspect

    db_module = sys.modules.get("app.core.db")
    if db_module is None:
        raise RuntimeError("请先导入 app.core.db 再调用 ensure_schema()")

    from app import models  # noqa: F401  注册所有表
    from app.core.base import Base

    engine = db_module.engine
    if "students" in set(inspect(engine).get_table_names()):
        return

    # engine 可能因为模块被重载而失效，重绑一次更稳
    if not hasattr(engine, "url"):
        engine = create_engine(
            _url_of(engine), future=True, connect_args={"check_same_thread": False}
        )
        db_module.engine = engine
    Base.metadata.create_all(bind=engine)


def _url_of(engine) -> str:
    return str(getattr(engine, "url", "sqlite://"))


def assert_not_workspace(db_url: str) -> None:
    """自检：确保没有指向工作区真实库。测试里可以调用。"""
    text = str(db_url).replace("\\", "/")
    if "monitor.db" in text and "sm-test-" not in text:
        raise AssertionError(
            f"测试正在使用工作区真实库：{db_url}\n"
            "请在使用 app.* 之前调用 tests.dbguard.isolate(...)"
        )
