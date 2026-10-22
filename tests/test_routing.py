# -*- coding: utf-8 -*-
"""预警通知的收件人路由测试（2026-10-13 需求）。

用户口径
--------
- 学生类预警（无记录 / 长期缺少记录 / 覆盖不足）→ 该生**所在班示示例辅导员**；
- 数据类预警（数据异常）→ **管理员**；
- 后台按班示示例辅导员，**每班留 3 个收件位置**。

本文件锁定的核心不变量：**不许静默丢失**。
班级没示示例辅导员时，如果只是"跳过"，那批学生的预警就永远没人收到，
而系统看起来一切正常 —— 这是本项目反复踩到的失效模式。
所以要求：没示示例辅导员的班 → 预警回落给管理员，并在邮件里写明要补配置。
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta

from tests import dbguard  # noqa: E402

_DB_PATH = dbguard.isolate("routing")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.config import settings  # noqa: E402
from app.core.enums import AlertKind  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AttendanceEvent,
    Course,
    CounselorContact,
    Student,
    StudentCourse,
)
from app.services.routing import (  # noqa: E402
    classes_without_counselor,
    normalize_class,
    route_warnings,
)
from app.services.warnings import Warning  # noqa: E402

NOW = datetime(2026, 10, 13, 20, 0)
TODAY = NOW.date()

#: 教务写法（名册里的班名）
CLASS_A = "2024数字媒体技术（中外合作办学）1班"
#: 翰博尔写法（同一个班的另一种写法）
CLASS_A_SHORT = "数字媒体技术241班"
CLASS_B = "2024视觉传达设计（中外合作办学）1班"

STU_A1 = "202400000001N"
STU_A2 = "202400000001N"
STU_B1 = "202400000001N"


def _w(kind: AlertKind, student_no: str, days: int = 3) -> Warning:
    return Warning(
        kind=kind,
        student_no=student_no,
        reason=f"{kind.value}（{days} 天）",
        first_date=TODAY - timedelta(days=days),
        last_date=TODAY,
        days=days,
    )


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        # 课程/选课关系也要清：方案 B 的用例会造课，留着会让下一个用例
        # 撞 schedule_guid 唯一键（夹具互相污染，不是被测代码的问题）。
        for table in (AttendanceEvent, StudentCourse, Course, CounselorContact, Student):
            self.db.query(table).delete()
        self.db.commit()
        self.db.add_all(
            [
                # active=True 必须显式给：该列没有默认值，留 NULL 的话
                # warning_summary（只算在册学生）会看不到这些人。
                Student(
                    student_no=STU_A1, name="甲", grade="2024",
                    class_name=CLASS_A, active=True,
                ),
                Student(
                    student_no=STU_A2, name="乙", grade="2024",
                    class_name=CLASS_A, active=True,
                ),
                Student(
                    student_no=STU_B1, name="丙", grade="2024",
                    class_name=CLASS_B, active=True,
                ),
            ]
        )
        self.db.commit()
        self._saved = {
            "alert_recipients": settings.alert_recipients,
            "alert_email_enabled": settings.alert_email_enabled,
        }

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(settings, k, v)
        self.db.close()

    def set_admin(self, *emails: str) -> None:
        settings.alert_recipients = ",".join(emails)

    def add_contact(self, class_name: str, *emails: str, note: str = "") -> None:
        slots = list(emails) + [""] * (3 - len(emails))
        self.db.add(
            CounselorContact(
                class_key=normalize_class(class_name),
                class_label=class_name,
                slot1=slots[0], slot2=slots[1], slot3=slots[2],
                note=note,
            )
        )
        self.db.commit()


class TestNormalizeClass(_Base):
    """班级名归一化 —— 对不上就会"永远发不出去"，所以必须钉死。"""

    def test_two_writings_of_same_class_share_a_key(self) -> None:
        self.assertEqual(
            normalize_class(CLASS_A), normalize_class(CLASS_A_SHORT),
            "教务写法与翰博尔写法必须折成同一个键，否则按班配示示例辅导员收不到信",
        )

    def test_different_classes_do_not_collide(self) -> None:
        self.assertNotEqual(normalize_class(CLASS_A), normalize_class(CLASS_B))

    def test_different_class_number_does_not_collide(self) -> None:
        """同专业 1 班和 2 班绝不能并成一个（会把 A 班预警发给 B 示示例辅导员）。"""
        self.assertNotEqual(
            normalize_class(CLASS_A),
            normalize_class("2024数字媒体技术（中外合作办学）2班"),
        )

    def test_unparsable_name_falls_back_to_raw(self) -> None:
        """折不出来就原样用 —— 宁可窄，也不能把两个班并成一个。"""
        raw = "公选课(01S01xxxx)"
        self.assertEqual(normalize_class(raw), raw)

    def test_empty_stays_empty(self) -> None:
        self.assertEqual(normalize_class(""), "")


class TestStudentWarningsGoToClassCounselor(_Base):
    def test_goes_to_that_class_counselor(self) -> None:
        self.set_admin("admin@example.com")
        self.add_contact(CLASS_A, "teacher-a@example.com")
        warnings = [_w(AlertKind.NO_RECORD, STU_A1), _w(AlertKind.NO_RECORD, STU_A2)]

        got = route_warnings(self.db, warnings)

        self.assertIn("teacher-a@example.com", got)
        self.assertNotIn("admin@example.com", got, "配示示例辅导员就不该再打扰管理员")
        self.assertEqual(len(got["teacher-a@example.com"].warnings), 2)

    def test_counselor_only_sees_own_class(self) -> None:
        """A 示示例辅导员绝不能收到 B 班学生的预警。"""
        self.add_contact(CLASS_A, "teacher-a@example.com")
        self.add_contact(CLASS_B, "teacher-b@example.com")
        warnings = [_w(AlertKind.NO_RECORD, STU_A1), _w(AlertKind.NO_RECORD, STU_B1)]

        got = route_warnings(self.db, warnings)

        self.assertEqual(
            [w.student_no for w in got["teacher-a@example.com"].warnings], [STU_A1]
        )
        self.assertEqual(
            [w.student_no for w in got["teacher-b@example.com"].warnings], [STU_B1]
        )

    def test_three_slots_all_receive(self) -> None:
        """每班留 3 个位置，配了几个就发几个。"""
        self.add_contact(
            CLASS_A, "a1@example.com", "a2@example.com", "a3@example.com"
        )
        got = route_warnings(self.db, [_w(AlertKind.NO_RECORD, STU_A1)])
        for email in ("a1@example.com", "a2@example.com", "a3@example.com"):
            self.assertIn(email, got)
            self.assertEqual(len(got[email].warnings), 1)

    def test_partial_slots_are_skipped(self) -> None:
        """只填了第 1、3 个位置 —— 空的第 2 位不能变成收件人。"""
        self.add_contact(CLASS_A, "a1@example.com")
        self.db.query(CounselorContact).delete()
        self.db.add(
            CounselorContact(
                class_key=normalize_class(CLASS_A), class_label=CLASS_A,
                slot1="a1@example.com", slot2="", slot3="a3@example.com",
            )
        )
        self.db.commit()
        got = route_warnings(self.db, [_w(AlertKind.NO_RECORD, STU_A1)])
        self.assertEqual(sorted(got), ["a1@example.com", "a3@example.com"])

    def test_same_person_two_classes_gets_one_mail(self) -> None:
        """同一示示例辅导员管两个班 → 合并成一封，不是两封。"""
        self.add_contact(CLASS_A, "shared@example.com")
        self.add_contact(CLASS_B, "shared@example.com")
        warnings = [_w(AlertKind.NO_RECORD, STU_A1), _w(AlertKind.NO_RECORD, STU_B1)]

        got = route_warnings(self.db, warnings)

        self.assertEqual(list(got), ["shared@example.com"])
        self.assertEqual(len(got["shared@example.com"].warnings), 2)
        self.assertEqual(
            sorted(got["shared@example.com"].classes), sorted([CLASS_A, CLASS_B])
        )

    def test_same_writing_variants_still_route(self) -> None:
        """辅导员按教务写法配的，学生班名是翰博尔写法 —— 也要能发到。"""
        self.add_contact(CLASS_A, "teacher-a@example.com")
        self.db.query(Student).filter(Student.student_no == STU_A1).update(
            {"class_name": CLASS_A_SHORT}
        )
        self.db.commit()
        got = route_warnings(self.db, [_w(AlertKind.NO_RECORD, STU_A1)])
        self.assertIn("teacher-a@example.com", got)


class TestDataWarningsGoToAdmin(_Base):
    def test_data_anomaly_goes_to_admin_only(self) -> None:
        self.set_admin("admin@example.com")
        self.add_contact(CLASS_A, "teacher-a@example.com")
        warnings = [
            _w(AlertKind.DATA_ANOMALY, ""),
            _w(AlertKind.NO_RECORD, STU_A1),
        ]

        got = route_warnings(self.db, warnings)

        self.assertIn("admin@example.com", got)
        self.assertIn("teacher-a@example.com", got)
        self.assertEqual(
            [w.kind for w in got["admin@example.com"].warnings], [AlertKind.DATA_ANOMALY]
        )

    def test_multiple_admins_all_get_data_warnings(self) -> None:
        self.set_admin("admin1@example.com", "admin2@example.com")
        got = route_warnings(self.db, [_w(AlertKind.DATA_ANOMALY, "")])
        self.assertEqual(sorted(got), ["admin1@example.com", "admin2@example.com"])


class TestNoSilentLoss(_Base):
    """**最要紧的一组**：没示示例辅导员的班，预警绝不能悄悄消失。"""

    def test_unassigned_class_falls_back_to_admin(self) -> None:
        self.set_admin("admin@example.com")
        # CLASS_A 没示示例辅导员
        got = route_warnings(self.db, [_w(AlertKind.NO_RECORD, STU_A1)])

        self.assertIn("admin@example.com", got, "没示示例辅导员时必须回落给管理员")
        r = got["admin@example.com"]
        self.assertEqual(len(r.warnings), 1)
        self.assertIn(CLASS_A, r.unassigned_classes, "要记下是哪个班没配，好去补")

    def test_no_email_is_lost(self) -> None:
        """所有预警都必须落在某个收件人身上 —— 一条都不能丢。"""
        self.set_admin("admin@example.com")
        self.add_contact(CLASS_A, "teacher-a@example.com")
        warnings = [
            _w(AlertKind.NO_RECORD, STU_A1),    # A 班（已配）
            _w(AlertKind.NO_RECORD, STU_A2),    # A 班（已配）
            _w(AlertKind.NO_RECORD, STU_B1),    # B 班（没配）
            _w(AlertKind.LOW_COVERAGE, STU_B1),  # B 班（没配）
            _w(AlertKind.DATA_ANOMALY, ""),     # 数据类
        ]

        got = route_warnings(self.db, warnings)

        total = sum(len(r.warnings) for r in got.values())
        self.assertEqual(total, 5, "5 条预警必须全部有归属（可重复投递，但不能丢）")
        self.assertIn(STU_B1, {w.student_no for w in got["admin@example.com"].warnings})
        self.assertIn(CLASS_B, got["admin@example.com"].unassigned_classes)

    def test_student_without_class_still_goes_somewhere(self) -> None:
        """学生没有班名（名册缺数据）也要有人收到 —— 否则就是静默丢失。"""
        self.set_admin("admin@example.com")
        self.db.query(Student).filter(Student.student_no == STU_A1).update(
            {"class_name": ""}
        )
        self.db.commit()
        got = route_warnings(self.db, [_w(AlertKind.NO_RECORD, STU_A1)])
        self.assertIn("admin@example.com", got)
        self.assertEqual(len(got["admin@example.com"].warnings), 1)

    def test_unknown_student_does_not_crash(self) -> None:
        self.set_admin("admin@example.com")
        got = route_warnings(self.db, [_w(AlertKind.NO_RECORD, "999999999999N")])
        self.assertIn("admin@example.com", got)

    def test_fallback_can_be_disabled(self) -> None:
        """显式关掉回落时，没示示例辅导员的班**只记日志**，不硬塞给管理员。"""
        self.set_admin("admin@example.com")
        got = route_warnings(
            self.db, [_w(AlertKind.NO_RECORD, STU_A1)], fallback_to_admin=False
        )
        self.assertEqual(got, {})


class TestRule2IsAlreadyTheStrictReading(_Base):
    """规则 ② 本身就是"方案 B"，不需要额外过滤（2026-10-13 核实）。

    用户当初选的是"只发**一节考勤数据都没有**的学生"。核实发现学校规则 ②
    的写法已经就是这个意思：它的前置条件是"最近 7 天内有课，但相关课程
    **均未产生**可用的考勤数据" —— 只要看得到一天，规则就不成立。

    线上实测两者选中**完全相同的 187 人，差集为空**，所以没有加过滤代码
    （多一层"看起来能调、实际不改变结果"的开关只会误导后来的人）。

    这组用例把这个等价关系钉住：将来若有人把规则 ② 放宽成"部分课看不到
    也报"，这里会红，提醒他重新确认邮件口径。
    """

    def _seed_day(self, student_no: str, *, covered: bool, days_ago: int) -> None:
        from datetime import timedelta as _td

        from app.models import Course, StudentCourse

        day = TODAY - _td(days=days_ago)
        guid = f"cov-{student_no}-{day:%Y%m%d}"
        start = datetime.combine(day, datetime.min.time()).replace(hour=8)
        self.db.add(
            Course(
                schedule_guid=guid, course_name="高等数学", class_date=day,
                start_time=start, end_time=start + _td(minutes=95), covered=covered,
            )
        )
        self.db.add(
            StudentCourse(
                student_no=student_no, schedule_guid=guid, class_date=day,
                source="roster",
            )
        )
        self.db.commit()

    def _kinds(self, student_no: str) -> set:
        from app.services.warnings import evaluate_warnings

        got = evaluate_warnings(self.db, {student_no}, now=NOW).get(student_no, [])
        return {w.kind for w in got}

    def test_all_uncovered_fires_coverage_warning(self) -> None:
        """窗口内一节有考勤源的课都没有 → 报覆盖不足（这类才该发示示例辅导员）。"""
        for offset in (1, 2, 3):
            self._seed_day(STU_B1, covered=False, days_ago=offset)
        self.assertIn(AlertKind.LOW_COVERAGE, self._kinds(STU_B1))

    def test_some_coverage_does_not_fire_coverage_warning(self) -> None:
        """**只要有一天看得到，规则 ② 就不成立** —— 这正是"方案 B"的语义。

        （用户担心的是"部分课看不到"也被天天发出去；实测不会。）
        """
        self._seed_day(STU_A1, covered=True, days_ago=2)
        self._seed_day(STU_A1, covered=False, days_ago=1)
        self.assertNotIn(AlertKind.LOW_COVERAGE, self._kinds(STU_A1))

    def test_page_summary_keeps_full_count(self) -> None:
        """页面统计与邮件同源，不会把系统的盲区藏起来。"""
        from app.services.warnings import warning_summary

        for offset in (1, 2, 3):
            self._seed_day(STU_B1, covered=False, days_ago=offset)
        summary = warning_summary(self.db, now=NOW)
        self.assertGreaterEqual(summary["counts"]["监测覆盖不足提醒"], 1)

    def test_digest_does_not_filter_anything(self) -> None:
        """邮件通道不加过滤：算出来多少就发多少（口径由规则 ② 决定）。"""
        from app.services.alerts import _notifiable

        warnings = [_w(AlertKind.LOW_COVERAGE, STU_B1), _w(AlertKind.NO_RECORD, STU_A1)]
        out = _notifiable(warnings, self.db, NOW)
        self.assertEqual(out, warnings)


class TestCounselorApi(unittest.TestCase):
    """接口层：每个新端点**真发一次请求**。

    为什么非要走 TestClient：`/api/counselors` 上线后第一次调用就 500，
    原因是函数体里用了 `select` 却没导入（`NameError`）。纯函数测试测不到，
    `import app.main` 也测不到 —— ImportError/NameError 在函数体里，
    只有真发一次请求才会暴露。这类"看起来部署成功、一点就崩"的问题
    必须由接口层用例兜住。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client = TestClient(app)

    def setUp(self) -> None:
        from app.core.security import issue_token

        self.db = SessionLocal()
        for table in (AttendanceEvent, StudentCourse, Course, CounselorContact, Student):
            self.db.query(table).delete()
        self.db.commit()
        self.db.add(
            Student(
                student_no=STU_A1, name="甲", grade="2024",
                class_name=CLASS_A, active=True,
            )
        )
        self.db.commit()
        self.client.cookies.set(settings.cookie_name, issue_token())
        self._saved = {"alert_recipients": settings.alert_recipients}

    def tearDown(self) -> None:
        settings.alert_recipients = self._saved["alert_recipients"]
        self.client.cookies.clear()
        self.db.close()

    def test_counselors_list_works(self) -> None:
        r = self.client.get("/api/counselors")
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()
        self.assertTrue(any(x["class_name"] == CLASS_A for x in rows), rows)

    def test_counselors_requires_session(self) -> None:
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/counselors").status_code, 401)

    def test_save_then_delete_roundtrip(self) -> None:
        r = self.client.post(
            "/api/counselors",
            json={
                "class_name": CLASS_A, "slot1": "t1@example.com",
                "slot2": "", "slot3": "t3@example.com", "note": "示示例辅导员",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["slot1"], "t1@example.com")
        self.assertEqual(body["slot3"], "t3@example.com")

        # 列表里能读回来
        rows = self.client.get("/api/counselors").json()
        row = next(x for x in rows if x["class_key"] == body["class_key"])
        self.assertEqual(row["slot1"], "t1@example.com")

        # 保存后该班不再算"未配"
        miss = self.client.get("/api/counselors/missing").json()
        self.assertNotIn(CLASS_A, [x["class_name"] for x in miss["classes"]])

        d = self.client.delete(f"/api/counselors/{body['class_key']}")
        self.assertEqual(d.status_code, 200, d.text)
        self.assertTrue(d.json()["ok"])

    def test_save_accepts_short_writing_of_class(self) -> None:
        """用翰博尔写法保存也要落到同一个班（键归一化）。"""
        r = self.client.post(
            "/api/counselors",
            json={"class_name": CLASS_A_SHORT, "slot1": "t@example.com",
                  "slot2": "", "slot3": "", "note": ""},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["class_key"], normalize_class(CLASS_A),
            "翰博尔写法必须和教务写法落到同一个键，否则按班配示示例辅导员收不到信",
        )

    def test_empty_class_name_is_rejected(self) -> None:
        r = self.client.post(
            "/api/counselors",
            json={"class_name": "   ", "slot1": "a@b.com", "slot2": "",
                  "slot3": "", "note": ""},
        )
        self.assertEqual(r.status_code, 400, r.text)

    def test_missing_counselors_works(self) -> None:
        r = self.client.get("/api/counselors/missing")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(CLASS_A, [x["class_name"] for x in r.json()["classes"]])

    def test_notify_dry_run_works(self) -> None:
        """预演接口：只算收件人，不发信（无预警时也要正常返回）。"""
        r = self.client.post("/api/notify/warnings?force=true&dry_run=true")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("message", body)
        self.assertIn("recipients", body)
        self.assertIn("unassigned_classes", body)

    def test_notify_requires_session(self) -> None:
        self.client.cookies.clear()
        r = self.client.post("/api/notify/warnings?dry_run=true")
        self.assertEqual(r.status_code, 401)


class TestClassesWithoutCounselor(_Base):
    def test_lists_unconfigured_classes_with_counts(self) -> None:
        self.add_contact(CLASS_A, "teacher-a@example.com")
        rows = classes_without_counselor(self.db)
        names = [r["class_name"] for r in rows]
        self.assertNotIn(CLASS_A, names, "配过的班不该再出现在待配置列表里")
        self.assertIn(CLASS_B, names)
        b = next(r for r in rows if r["class_name"] == CLASS_B)
        self.assertEqual(b["students"], 1)

    def test_contact_with_no_email_still_counts_as_unconfigured(self) -> None:
        """建了行但一个邮箱都没填 = 没配（否则这些学生会没人收到）。"""
        self.db.add(
            CounselorContact(
                class_key=normalize_class(CLASS_A), class_label=CLASS_A,
                slot1="", slot2="", slot3="",
            )
        )
        self.db.commit()
        names = [r["class_name"] for r in classes_without_counselor(self.db)]
        self.assertIn(CLASS_A, names)


if __name__ == "__main__":
    unittest.main()
