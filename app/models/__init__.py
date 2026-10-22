"""ORM 模型定义。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, utcnow


class Student(Base):
    """学生名册。年级/专业从学号切片解析后落库，便于索引与分组。"""

    __tablename__ = "students"

    student_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="", index=True)
    #: 姓名从哪来：``"roster"`` = 教务处名册（权威），``"attendance"`` = 考勤接口。
    #:
    #: 为什么需要它：翰博尔返回的 ``userName`` 是**学生自己设的账号名**，
    #: 实测有人叫「爸爸」「cxy」「Jackie」。采集每轮都调 ``upsert_students``，
    #: 于是这些昵称会把名册里的真名一遍遍覆盖掉（2026-10-12 老师报
    #: 「24 智科人名不对」就是这个原因）。有了来源标记，采集侧只"填空"、
    #: 不覆盖名册给的名字。
    name_source: Mapped[str] = mapped_column(
        String(16), default="attendance", server_default=text("'attendance'"), index=True
    )
    grade: Mapped[str] = mapped_column(String(16), default="", index=True)
    major_code: Mapped[str] = mapped_column(String(16), default="", index=True)
    major_name: Mapped[str] = mapped_column(String(64), default="")
    class_name: Mapped[str] = mapped_column(String(64), default="")
    org_name: Mapped[str] = mapped_column(String(64), default="")
    enroll_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # ── 老师手动标记（"这个学生我确认过"）─────────────────────────
    #: 老师断言的状态（目前只有「活跃」）。空串 = 没有标记。
    manual_state: Mapped[str] = mapped_column(String(16), default="", index=True)
    #: 标记时刻。规则：**「标记后，视作一次没有课程的签到」** ——
    #: 这次标记相当于学生刚签到过一次（那天没课），把标记日及以前的
    #: 连续无签到全部清零；但**不赠送豁免**，从第二天起照常判定。
    manual_marked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manual_reason: Mapped[str] = mapped_column(String(255), default="")
    manual_by: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def group_key(self) -> str:
        """分组键：年级 + 专业（例如 2024级智能科学与技术）。"""
        major = self.major_name or (f"专业{self.major_code}" if self.major_code else "未知专业")
        return f"{self.grade}级{major}" if self.grade else major


class Course(Base):
    """一节课 = 一条录播计划（scheduleGuid 唯一）。"""

    __tablename__ = "courses"

    schedule_guid: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    course_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    #: 教务处课程号（如 ``2291000001``）。为什么需要它：**同名不同课**是真实
    #: 存在的 —— 本院有两个「国际背景下的研究」（课序号 01 / 01S01，
    #: 一个 176 人一个 30 人，上课时间也不同）。只按课程名匹配会把两门课
    #: 合并成一门，选课关系就张冠李戴了。
    course_no: Mapped[str] = mapped_column(String(32), default="", index=True)
    teacher_name: Mapped[str] = mapped_column(String(64), default="")
    classroom_name: Mapped[str] = mapped_column(String(64), default="")
    classroom_no: Mapped[str] = mapped_column(String(64), default="")
    building_name: Mapped[str] = mapped_column(String(64), default="")
    week_name: Mapped[str] = mapped_column(String(32), default="")
    node_name: Mapped[str] = mapped_column(String(32), default="")
    class_name: Mapped[str] = mapped_column(String(64), default="")
    org_name: Mapped[str] = mapped_column(String(64), default="")
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    class_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    should_student_count: Mapped[int] = mapped_column(Integer, default=0)
    attend_student_count: Mapped[int] = mapped_column(Integer, default=0)
    absent_student_count: Mapped[int] = mapped_column(Integer, default=0)
    late_student_count: Mapped[int] = mapped_column(Integer, default=0)
    leave_student_count: Mapped[int] = mapped_column(Integer, default=0)
    attendance_rate: Mapped[float] = mapped_column(Float, default=0.0)
    #: 这门课**有没有考勤源覆盖**（能不能看到"来没来"）。
    #:
    #: 为什么必须有这个字段：翰博尔是「智慧教室/录播」系统，只覆盖装了设备的
    #: 教室。教务选课名单里的课（思政/体育/大学英语…）大多不在其中 ——
    #: 而名单一录进来，「课表里有课 + 没有签到」就会被判成整日无签到，
    #: 上千名学生会被集体误报成「严重异常」。
    #:
    #: 规则：由教务名单建立的课（拿不到录播计划）为 ``False``；
    #: 翰博尔来的课为 ``True``（它本身就是考勤源）。
    #: ``False`` 的课照常显示（页面置灰），但**不参与异常判定与缺勤反推**。
    covered: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), index=True
    )
    detail_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class StudentCourse(Base):
    """学生选课/应到关系：从考勤详情里观测到即落库，用于判断「某天是否有课」。"""

    __tablename__ = "student_courses"
    __table_args__ = (
        UniqueConstraint("student_no", "schedule_guid", name="uq_student_course"),
        Index("ix_student_course_date", "student_no", "class_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("students.student_no", ondelete="CASCADE"), index=True
    )
    schedule_guid: Mapped[str] = mapped_column(
        String(64), ForeignKey("courses.schedule_guid", ondelete="CASCADE"), index=True
    )
    class_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AttendanceEvent(Base):
    """归一化后的签到事实 —— 同时充当持久化消息总线（outbox）。

    唯一键 (source, schedule_guid, student_no, class_date) 保证重复采集幂等。
    """

    __tablename__ = "attendance_events"
    __table_args__ = (
        UniqueConstraint(
            "source", "schedule_guid", "student_no", "class_date", name="uq_attendance_event"
        ),
        Index("ix_event_student_date", "student_no", "class_date"),
        Index("ix_event_date", "class_date"),
        Index("ix_event_course", "schedule_guid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    schedule_guid: Mapped[str] = mapped_column(String(64), index=True)
    student_no: Mapped[str] = mapped_column(String(32), index=True)
    user_type: Mapped[int] = mapped_column(Integer, default=2)  # 1=老师 2=学生
    class_date: Mapped[date] = mapped_column(Date, index=True)
    attend_status: Mapped[str] = mapped_column(String(16), default="")
    sign_in_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    node_no: Mapped[str] = mapped_column(String(32), default="")
    classroom_no: Mapped[str] = mapped_column(String(64), default="")
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AttendanceDaily(Base):
    """按「学生 × 日期」聚合的判定结果缓存，供仪表盘与连续异常统计使用。"""

    __tablename__ = "attendance_daily"
    __table_args__ = (
        UniqueConstraint("student_no", "class_date", name="uq_daily"),
        Index("ix_daily_date_state", "class_date", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_no: Mapped[str] = mapped_column(String(32), index=True)
    class_date: Mapped[date] = mapped_column(Date, index=True)
    has_class: Mapped[bool] = mapped_column(Boolean, default=False)
    has_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    has_leave: Mapped[bool] = mapped_column(Boolean, default=False)
    session_count: Mapped[int] = mapped_column(Integer, default=0)
    missed_count: Mapped[int] = mapped_column(Integer, default=0)
    present_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    day_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    all_missed: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), default="")
    last_sign_in_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Alert(Base):
    """告警记录与去重。"""

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint(
            "student_no", "kind", "first_date", "last_date", name="uq_alert_window"
        ),
        Index("ix_alert_student_time", "student_no", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_no: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(16), default="")
    consecutive_days: Mapped[int] = mapped_column(Integer, default=0)
    first_date: Mapped[date] = mapped_column(Date)
    last_date: Mapped[date] = mapped_column(Date)
    detail: Mapped[str] = mapped_column(Text, default="")
    #: 结构化上下文（涉及课程、最近缺勤日期等），JSON 文本。
    #: 让邮件能写清「缺了哪几节课」，而不是只给一个学号。
    detail_json: Mapped[str] = mapped_column(Text, default="")
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email_error: Mapped[str] = mapped_column(Text, default="")
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class CollectRun(Base):
    """采集任务运行记录，用于健康度展示与排障。"""

    __tablename__ = "collect_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    job: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    courses_seen: Mapped[int] = mapped_column(Integer, default=0)
    events_upserted: Mapped[int] = mapped_column(Integer, default=0)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    """运行期可改的配置（覆盖 .env 默认值），例如阈值、收件人。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MajorAlias(Base):
    """专业代码 → 专业名称 映射（信息化处给的名册若只有代码，用它转中文）。"""

    __tablename__ = "major_aliases"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")


class ClassAlias(Base):
    """班级别名：教务的班名 ↔ 考勤系统的班名。

    为什么需要它：教务名册写「2024数字媒体技术（中外合作办学）1班」，
    翰博尔写「数字媒体技术241班」。对不上时系统**不报错**，只是永远发现
    不了旷课学生 —— 最危险的那种静默失效。

    ``app/services/roster.py`` 的 ``class_key()`` 已经能自动折算出实测的
    写法差异；本表是留给折不出来的那些（某个班根本不带专业名之类）的兜底，
    免得只能改代码。``alias`` 与 ``canonical`` 都写**原始班名**即可，
    比对时两边都会先归一化。
    """

    __tablename__ = "class_aliases"

    alias: Mapped[str] = mapped_column(String(128), primary_key=True)
    canonical: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CounselorContact(Base):
    """辅导员联系人：一个班 → 最多 3 个收件邮箱。

    为什么按**班**配而不是按学生配：辅导员是管班的，一个班几十号人配一遍
    既费事又会漏；班级归属本来就在名册里（``students.class_name``）。

    ``class_key`` 存的是 :func:`app.services.roster.class_key` 折算出来的
    ``"年|专业|班号"`` 键（例如 ``"2024|数字媒体技术|1"``），不是原始班名 ——
    因为教务写「2024数字媒体技术（中外合作办学）1班」、翰博尔写
    「数字媒体技术241班」，直接按原始名配会对不上，**而且不会报错**，
    只会永远发不出去（本项目最怕的静默失效）。

    ``slot1/2/3`` 对应后台的三个收件位置。空字符串表示该位置没配人，
    不是"发错人"，读取时跳过即可。
    """

    __tablename__ = "counselor_contacts"

    class_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: 原始班名，仅供后台显示（键可能已归一化，老师认不出）
    class_label: Mapped[str] = mapped_column(String(128), default="")
    slot1: Mapped[str] = mapped_column(String(128), default="")
    slot2: Mapped[str] = mapped_column(String(128), default="")
    slot3: Mapped[str] = mapped_column(String(128), default="")
    #: 可选的称呼/备注，例如「示示例辅导员（1、2班）」
    note: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def emails(self) -> list[str]:
        """非空的收件邮箱（保持 slot1→3 顺序，去重）。"""
        out: list[str] = []
        for raw in (self.slot1, self.slot2, self.slot3):
            v = str(raw or "").strip()
            if v and v not in out:
                out.append(v)
        return out


class StudentMark(Base):
    """手动标记的**流水**。

    为什么不能只留 ``students`` 上那几个字段：那是"当前标记"，重复标记会覆盖，
    上一次的原因就没了。而这是一个**要能追责**的操作（"这学生谁标的、凭什么"），
    所以每次标记/撤销都单独记一条，只增不改。

    页面上的「当前标记」仍然读 ``students``（判定链路要按标记日切割历史，
    读单行最省事）；本表只服务"查历史"。
    """

    __tablename__ = "student_marks"
    __table_args__ = (Index("ix_mark_student_time", "student_no", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_no: Mapped[str] = mapped_column(String(32), index=True)
    #: mark = 标记；unmark = 撤销
    action: Mapped[str] = mapped_column(String(16), default="mark", index=True)
    state: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


__all__ = [
    "Student",
    "Course",
    "StudentCourse",
    "AttendanceEvent",
    "AttendanceDaily",
    "Alert",
    "CollectRun",
    "AppSetting",
    "MajorAlias",
    "ClassAlias",
    "CounselorContact",
    "StudentMark",
]
