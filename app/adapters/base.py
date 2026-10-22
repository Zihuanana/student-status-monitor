"""考勤源适配器层。

每个考勤软件一个适配器，统一对外提供：
- ``fetch_statistics()`` 拉取课程（录播计划）列表
- ``fetch_details()``    拉取某节课/某时间段的逐人签到明细
并把结果归一化成 :class:`NormalizedEvent`，屏蔽各家字段差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(slots=True)
class NormalizedCourse:
    """归一化后的「一节课」。"""

    schedule_guid: str
    source: str
    title: str = ""
    course_name: str = ""
    #: 教务处课程号。同名不同课（本院有两个「国际背景下的研究」）靠它区分。
    course_no: str = ""
    teacher_name: str = ""
    classroom_name: str = ""
    classroom_no: str = ""
    building_name: str = ""
    week_name: str = ""
    node_name: str = ""
    class_name: str = ""
    org_name: str = ""
    start_time: datetime | None = None
    end_time: datetime | None = None
    should_student_count: int = 0
    attend_student_count: int = 0
    absent_student_count: int = 0
    late_student_count: int = 0
    leave_student_count: int = 0
    attendance_rate: float = 0.0
    #: 这门课**有没有考勤源覆盖**。默认 ``True``：能从适配器拿到的课，
    #: 本身就来自一个考勤源。教务名单导入器建的课会显式传 ``False``
    #: —— 名单里有课不等于我们看得到"来没来"（见 app/models 里 Course.covered）。
    covered: bool = True
    raw: dict = field(default_factory=dict)

    @property
    def class_date(self) -> date | None:
        return self.start_time.date() if self.start_time else None


@dataclass(slots=True)
class NormalizedEvent:
    """归一化后的「一名学生一节课的签到事实」。"""

    source: str
    schedule_guid: str
    student_no: str
    class_date: date
    attend_status: str = ""
    sign_in_time: datetime | None = None
    node_no: str = ""
    classroom_no: str = ""
    user_type: int = 2
    #: 接口若返回姓名就带上，省去等名册导入（翰博尔实测返回 userName）
    student_name: str = ""
    raw: dict | None = None
