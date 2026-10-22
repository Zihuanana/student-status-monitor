# -*- coding: utf-8 -*-
"""数据清理与保留策略测试。

采集是持续轮询，若不做清理，SQLite 会无限增长（服务器磁盘仅约 10G）。
这里验证：超期数据被删、窗口内数据保留、告警不被误删。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.base import Base
from app.models import AttendanceDaily, AttendanceEvent, Course, Student
from app.services.maintenance import checkpoint, db_stats, prune

NOW = datetime(2026, 3, 11, 15, 0)
TODAY = NOW.date()


class TestPrune(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sm-test-"))
        engine = create_engine(f"sqlite:///{self.tmp / 'test.db'}", future=True)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self.db = self.Session()
        self.db.add(Student(student_no="2024010001", name="测试", grade="2024", major_code="01"))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _add_day(self, day: date, guid: str) -> None:
        self.db.add(
            Course(
                schedule_guid=guid,
                course_name="测试课",
                class_date=day,
                start_time=datetime.combine(day, datetime.min.time()),
                end_time=datetime.combine(day, datetime.min.time()) + timedelta(hours=2),
            )
        )
        self.db.add(
            AttendanceEvent(
                source="hanboer",
                schedule_guid=guid,
                student_no="2024010001",
                class_date=day,
                attend_status="出勤",
            )
        )
        self.db.add(
            AttendanceDaily(student_no="2024010001", class_date=day, has_class=True, has_signed=True)
        )
        self.db.commit()

    def test_old_data_is_pruned(self) -> None:
        old = TODAY - timedelta(days=500)
        self._add_day(old, "old-guid")
        result = prune(self.db, retention_days=400, now=NOW)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["daily"], 1)
        self.assertEqual(result["courses"], 1)  # 已无事件引用才删
        self.assertEqual(db_stats(self.db)["events"], 0)

    def test_recent_data_is_kept(self) -> None:
        self._add_day(TODAY - timedelta(days=3), "recent-guid")
        result = prune(self.db, retention_days=400, now=NOW)
        self.assertEqual(result["events"], 0)
        self.assertEqual(db_stats(self.db)["events"], 1)
        self.assertEqual(db_stats(self.db)["courses"], 1)

    def test_students_never_pruned(self) -> None:
        self._add_day(TODAY - timedelta(days=900), "very-old")
        prune(self.db, retention_days=400, now=NOW)
        self.assertEqual(db_stats(self.db)["students"], 1)

    def test_course_kept_if_still_referenced(self) -> None:
        """课程行过期但仍被窗口内的事件引用时不能删。"""
        guid = "shared-guid"
        self.db.add(
            Course(
                schedule_guid=guid,
                course_name="跨期课",
                class_date=TODAY - timedelta(days=500),
                start_time=datetime.combine(TODAY - timedelta(days=1), datetime.min.time()),
                end_time=datetime.combine(TODAY - timedelta(days=1), datetime.min.time())
                + timedelta(hours=2),
            )
        )
        # 事件日期较新（模拟补录），课程日期较旧
        self.db.add(
            AttendanceEvent(
                source="hanboer",
                schedule_guid=guid,
                student_no="2024010001",
                class_date=TODAY - timedelta(days=1),
                attend_status="出勤",
            )
        )
        self.db.commit()
        prune(self.db, retention_days=400, now=NOW)
        remaining = self.db.execute(select(Course.schedule_guid)).scalars().all()
        self.assertIn(guid, remaining, "仍被引用的事件所属课程不应被删除")

    def test_checkpoint_does_not_raise(self) -> None:
        """回归：checkpoint 曾因用 db.connection() 抛 ResourceClosedError。"""
        checkpoint(self.db)
        # 之后会话仍可用
        self.db.add(Student(student_no="2024010002", name="测试2"))
        self.db.commit()
        self.assertEqual(db_stats(self.db)["students"], 2)

    def test_retention_floor(self) -> None:
        """保留天数有下限（30 天），避免误配置把有效数据删光。"""
        self._add_day(TODAY - timedelta(days=45), "mid-guid")
        # 传入 1 天也会被夹到 30 天，45 天前的数据仍应被删除
        result = prune(self.db, retention_days=1, now=NOW)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["cutoff"], (TODAY - timedelta(days=30)).isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
