# -*- coding: utf-8 -*-
"""轻量迁移测试：给已存在的表补新增列。

为什么单独测这个
----------------
``Base.metadata.create_all()`` 只会建**缺失的表**，对已存在的表**不加列**。
所以给模型加字段后会出现最坑的一种情况：

  - 新库（测试）一切正常；
  - 线上旧库一升级就 `no such column: alerts.detail_json`；
  - 而且报错发生在**采集过程中**，表现成"采集一直失败"，很难联想到加字段。

本模块用"按老结构建表 → 打开新版应用"的方式复刻这个升级场景。

⚠ 关于数据库绑定
----------------
``app.core.config`` / ``app.core.db`` 都是**模块级单例**，环境变量只在
**首次导入时**读取一次。测试收集阶段若别的模块先导入了它们，
工作区 ``.env``（`sqlite:///./data/monitor.db`）就会胜出 ——
本模块曾因此在**真实本地库**上做检查，完全测不到自己的临时库。

因此这里不用环境变量，而是用 ``mock.patch.object`` 显式把
``engine`` / ``settings.database_url`` 指向本模块的临时库。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="sm-migrate-")
_DB = os.path.join(_TMP, "old.db")
os.environ.setdefault("APP_ENV_FILE", os.path.join(_TMP, "none.env"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from sqlalchemy import create_engine, inspect, text  # noqa: E402


class TestLightMigration(unittest.TestCase):
    """用独立的引擎操作同一个文件，避免污染应用自己的连接池。"""

    def setUp(self) -> None:
        self.engine = create_engine(f"sqlite:///{_DB}", future=True)
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alerts"))
            # 故意建成"没有 detail_json"的老结构
            conn.execute(
                text(
                    """
                    CREATE TABLE alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_no VARCHAR(32) NOT NULL,
                        kind VARCHAR(16) NOT NULL,
                        state VARCHAR(16) NOT NULL,
                        consecutive_days INTEGER NOT NULL,
                        first_date DATE NOT NULL,
                        last_date DATE NOT NULL,
                        detail TEXT NOT NULL,
                        email_sent BOOLEAN NOT NULL,
                        email_sent_at DATETIME,
                        email_error TEXT NOT NULL,
                        acknowledged BOOLEAN NOT NULL,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            # 放一行既有数据，确认加列不会丢数据
            conn.execute(
                text(
                    "INSERT INTO alerts (student_no, kind, state, consecutive_days,"
                    " first_date, last_date, detail, email_sent, email_error,"
                    " acknowledged, created_at)"
                    " VALUES ('2024010001','异常','异常',2,'2026-03-01','2026-03-02',"
                    " '旧数据', 0, '', 0, '2026-03-02 10:00:00')"
                )
            )

        # 把应用绑定到本模块的临时库（环境变量在这里不可靠，见模块说明）
        from app.core import db as db_module
        from app.core.config import settings

        patcher_engine = mock.patch.object(db_module, "engine", self.engine)
        patcher_url = mock.patch.object(
            settings, "database_url", f"sqlite:///{_DB}"
        )
        patcher_engine.start()
        patcher_url.start()
        self.addCleanup(patcher_engine.stop)
        self.addCleanup(patcher_url.stop)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_missing_column_is_added(self) -> None:
        from app.core.db import apply_light_migrations

        before = {
            c["name"] for c in inspect(self.engine).get_columns("alerts")
        }
        self.assertNotIn("detail_json", before, "前置条件：老结构里不应有该列")

        applied = apply_light_migrations()

        if "alerts.detail_json" not in applied:
            # 失败时打印现场，便于定位是 metadata 没注册还是别的隔离问题
            from app.core.base import Base
            from app.core.db import engine as _e

            raise AssertionError(
                "应报告补了哪一列，实际 applied=%r；"
                "metadata 表=%r；应用引擎 url=%s"
                % (applied, [t.name for t in Base.metadata.sorted_tables], _e.url)
            )
        after = {c["name"] for c in inspect(self.engine).get_columns("alerts")}
        self.assertIn("detail_json", after)

    def test_existing_rows_survive_and_get_default(self) -> None:
        """加列不能丢数据；既有行的新列应拿到默认值而不是 NULL。"""
        from app.core.db import apply_light_migrations

        apply_light_migrations()

        with self.engine.begin() as conn:
            rows = conn.execute(
                text("SELECT student_no, detail, detail_json FROM alerts")
            ).all()

        self.assertEqual(len(rows), 1, "既有数据不能被删掉")
        student_no, detail, detail_json = rows[0]
        self.assertEqual(student_no, "2024010001")
        self.assertEqual(detail, "旧数据", "原有列的值不能变")
        self.assertEqual(detail_json, "", "新增列应拿到默认空串，而不是 NULL")

    def test_migration_is_idempotent(self) -> None:
        """重复执行不能报错（每次启动都会调用）。"""
        from app.core.db import apply_light_migrations

        first = apply_light_migrations()
        second = apply_light_migrations()

        self.assertIn("alerts.detail_json", first)
        self.assertEqual(second, [], "第二次不应再补任何列")

    def test_orm_can_write_after_migration(self) -> None:
        """补列之后 ORM 必须能正常读写该字段（这是加列的真正目的）。"""
        from app.core.db import apply_light_migrations

        apply_light_migrations()

        from app.models import Alert
        from sqlalchemy.orm import Session, sessionmaker

        Session_ = sessionmaker(bind=self.engine, future=True, class_=Session)
        with Session_() as s:
            from datetime import date

            s.add(
                Alert(
                    student_no="2024010002",
                    kind="严重异常",
                    state="严重异常",
                    consecutive_days=3,
                    first_date=date(2026, 3, 1),
                    last_date=date(2026, 3, 3),
                    detail="测试",
                    detail_json='{"courses": ["高等数学"]}',
                )
            )
            s.commit()
            got = s.query(Alert).filter(Alert.student_no == "2024010002").one()
            self.assertEqual(got.detail_json, '{"courses": ["高等数学"]}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
