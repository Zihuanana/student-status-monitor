# -*- coding: utf-8 -*-
"""翰博尔接口客户端测试：签名算法、字段映射、时间解析。

签名算法依据说明书：
    除签名的其余参数按参数名（小写）正序排序 → 拼接「参数名+参数值」
    → 把 AccessKeySecret 拼到头和尾 → MD5(32位) → 转大写。
"""

from __future__ import annotations

import hashlib
import unittest

from app.adapters.hanboer import (
    HanboerClient,
    build_signed_url,
    parse_date,
    parse_dt,
)


class TestSignature(unittest.TestCase):
    def setUp(self) -> None:
        self.client = HanboerClient(
            base_url="https://example.edu.cn",
            access_key="AK123",
            access_key_secret="SECRET",
            app_source="app01",
        )

    def test_sign_is_uppercase_md5(self) -> None:
        signed = self.client.build_sign({"pageNumber": 1}, time_stamp="1700000000000")
        self.assertEqual(len(signed["sign"]), 32)
        self.assertEqual(signed["sign"], signed["sign"].upper())
        int(signed["sign"], 16)  # 必须是合法十六进制

    def test_sign_matches_manual_computation(self) -> None:
        params = {"pageNumber": 1, "pageSize": 50}
        ts = "1700000000000"
        signed = self.client.build_sign(params, time_stamp=ts)

        fields = {
            "timestamp": ts,
            "appsource": "app01",
            "accesskey": "AK123",
            "pagenumber": "1",
            "pagesize": "50",
        }
        joined = "".join(f"{k}{fields[k]}" for k in sorted(fields))
        expected = hashlib.md5(f"SECRET{joined}SECRET".encode()).hexdigest().upper()
        self.assertEqual(signed["sign"], expected)

    def test_empty_and_none_params_excluded(self) -> None:
        a = self.client.build_sign({"courseName": None, "sort": ""}, time_stamp="1700000000000")
        b = self.client.build_sign({}, time_stamp="1700000000000")
        self.assertEqual(a["sign"], b["sign"])

    def test_param_order_does_not_matter(self) -> None:
        ts = "1700000000000"
        a = self.client.build_sign({"a": 1, "b": 2, "c": 3}, time_stamp=ts)
        b = self.client.build_sign({"c": 3, "a": 1, "b": 2}, time_stamp=ts)
        self.assertEqual(a["sign"], b["sign"])

    def test_signature_params_present(self) -> None:
        signed = self.client.build_sign({"pageNumber": 1})
        for key in ("sign", "timeStamp", "appSource", "accessKey"):
            self.assertIn(key, signed)
        self.assertEqual(signed["appSource"], "app01")
        self.assertEqual(signed["accessKey"], "AK123")
        self.assertTrue(self.client.verify_sign(signed))

    def test_stale_timestamp_rejected_by_ttl_check(self) -> None:
        signed = self.client.build_sign({"pageNumber": 1}, time_stamp="1600000000000")
        self.assertFalse(self.client.verify_sign(signed))

    def test_build_signed_url_contains_all_parts(self) -> None:
        url = build_signed_url(
            "https://example.edu.cn",
            "/attendance/api/v1/openApi/getAttendanceStatistics",
            {"pageNumber": 1},
        )
        self.assertTrue(url.startswith("https://example.edu.cn/attendance/"))
        for token in ("sign=", "timeStamp=", "appSource=", "accessKey=", "pageNumber=1"):
            self.assertIn(token, url)


class TestParsing(unittest.TestCase):
    def test_parse_dt_standard(self) -> None:
        self.assertEqual(parse_dt("2024-07-11 06:24:00").year, 2024)
        self.assertEqual(parse_dt("2024-07-11 06:24:00").hour, 6)
        self.assertEqual(parse_dt("2024-07-11").day, 11)

    def test_parse_dt_compact_like_doc_sample(self) -> None:
        # 说明书示例里时间被排版成 '2024-07-1106:24:00'
        self.assertIsNotNone(parse_dt("2024-07-1106:24:00"))

    def test_parse_dt_none_values(self) -> None:
        for raw in (None, "", "null", "   "):
            self.assertIsNone(parse_dt(raw))

    def test_parse_dt_epoch_millis(self) -> None:
        parsed = parse_dt(1720664019175)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2024)

    def test_parse_date(self) -> None:
        self.assertEqual(str(parse_date("2025-06-11")), "2025-06-11")
        self.assertEqual(str(parse_date("2025/06/11")), "2025-06-11")
        self.assertIsNone(parse_date(None))


class TestFieldMapping(unittest.TestCase):
    def test_map_course_row(self) -> None:
        row = {
            "scheduleGuid": "000a5934ababc73aa598113a692757e2",
            "classroomName": "Q4195",
            "teacherName": "0107t1",
            "courseName": None,
            "buildingName": "逸夫楼",
            "weekName": "第28周",
            "orgName": "",
            "createTime": "2024-07-11 10:13:38",
            "startTime": "2024-07-11 06:24:00",
            "endTime": "2024-07-11 11:20:00",
            "title": "Q4195语文2024-07-11",
            "lateStudentCount": 0,
            "leaveStudentCount": 0,
            "shouldStudentCount": 10,
            "attendStudentCount": 8,
            "absentStudentCount": 2,
            "attendanceRate": 0.8,
        }
        course = HanboerClient._to_course(row)
        self.assertEqual(course.schedule_guid, "000a5934ababc73aa598113a692757e2")
        self.assertEqual(course.building_name, "逸夫楼")
        self.assertEqual(course.should_student_count, 10)
        self.assertEqual(course.absent_student_count, 2)
        self.assertAlmostEqual(course.attendance_rate, 0.8)
        self.assertEqual(str(course.class_date), "2024-07-11")
        self.assertEqual(course.source, "hanboer")

    def test_map_detail_row(self) -> None:
        row = {
            "userNo": "0711Teacher",
            "attendDate": "2025-06-11",
            "attendStatus": "缺勤",
            "nodeNo": "第一节",
            "classroomNo": "Q31A330",
            "signInTime": None,
            "userType": 1,
        }
        ev = HanboerClient._to_event(row, "guid-1")
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.student_no, "0711Teacher")
        self.assertEqual(ev.attend_status, "缺勤")
        self.assertEqual(ev.user_type, 1)
        self.assertEqual(str(ev.class_date), "2025-06-11")
        self.assertIsNone(ev.sign_in_time)

    def test_map_detail_row_with_sign_time(self) -> None:
        row = {
            "userNo": "2024010001",
            "attendDate": "2025-06-11",
            "attendStatus": "迟到",
            "nodeNo": "第二节",
            "signInTime": "2025-06-11 08:15:00",
            "userType": 2,
        }
        ev = HanboerClient._to_event(row, "guid-2")
        assert ev is not None
        self.assertEqual(ev.sign_in_time.hour, 8)
        self.assertEqual(ev.user_type, 2)

    def test_map_detail_row_rejects_incomplete(self) -> None:
        self.assertIsNone(HanboerClient._to_event({"userNo": "", "attendDate": "2025-06-11"}, "g"))
        self.assertIsNone(HanboerClient._to_event({"userNo": "x", "attendDate": None}, "g"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
