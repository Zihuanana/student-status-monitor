"""API 出入参模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class SessionOut(BaseModel):
    authenticated: bool
    data_mode: str = "mock"
    app_base_path: str = ""
    version: str = "0.1.0"
    now: str = ""


class StudentCard(BaseModel):
    student_no: str
    name: str
    grade: str = ""
    major_code: str = ""
    major_name: str = ""
    class_name: str = ""
    group: str = ""
    state: str
    level: str
    reason: str = ""
    consecutive_missed_days: int = 0
    last_class_date: str | None = None
    last_sign_in_time: str | None = None
    # ── 老师手动标记 ──
    #: 是否示示例辅导员手动标记过（**与当前状态无关**：标记后又异常时仍为 True）
    manual_marked: bool = False
    #: 标记时刻，精确到分钟（卡片上直接显示）
    manual_marked_at: str | None = None
    manual_reason: str = ""
    manual_by: str = ""


class MarkIn(BaseModel):
    """手动标记入参。reason 必填 —— 这个操作会被记录，也要能被追问。"""

    state: str = Field(default="活跃", max_length=16)
    reason: str = Field(min_length=1, max_length=255)
    by: str = Field(default="老师", max_length=64)


class GroupOut(BaseModel):
    key: str
    grade: str = ""
    major: str = ""
    total: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    students: list[StudentCard] = Field(default_factory=list)


class OverviewOut(BaseModel):
    generated_at: str
    data_mode: str
    totals: dict[str, int]
    counts: dict[str, int]
    groups: list[GroupOut]


class SessionCell(BaseModel):
    schedule_guid: str
    course_name: str = ""
    node_name: str = ""
    classroom: str = ""
    start: str = ""
    end: str = ""
    state: str
    signed: bool = False
    sign_in_time: str | None = None
    #: 这节课我们**能不能看到出勤**。``False`` = 翰博尔覆盖不到这间教室
    #: （教务名单里有课、录播系统看不到），前端要置灰，并说明"不参与判定"。
    covered: bool = True


class DayOut(BaseModel):
    date: str
    weekday: str
    is_today: bool = False
    has_class: bool = False
    state: str = ""
    #: 当天有多少节课没有考勤源覆盖（看不到"来没来"）
    no_source_count: int = 0
    sessions: list[SessionCell] = Field(default_factory=list)


class WeekOut(BaseModel):
    student_no: str
    name: str
    week_start: str
    week_end: str
    week_label: str
    state: str
    level: str
    reason: str = ""
    manual_marked: bool = False
    #: 标记时刻，精确到分钟（卡片上直接显示）
    manual_marked_at: str | None = None
    manual_reason: str = ""
    manual_by: str = ""
    days: list[DayOut] = Field(default_factory=list)


class CollectStatusOut(BaseModel):
    last_run_at: str | None = None
    last_run_ok: bool | None = None
    last_run_message: str = ""
    total_events: int = 0
    total_students: int = 0
    total_courses: int = 0
    sources: list[str] = Field(default_factory=list)
    today_alerts: int = 0
    today_severe: int = 0
    polling: bool = False


class AlertOut(BaseModel):
    id: int
    student_no: str
    kind: str
    state: str
    consecutive_days: int
    first_date: str
    last_date: str
    detail: str = ""
    email_sent: bool = False
    email_sent_at: str | None = None
    email_error: str = ""
    created_at: str


class SettingsOut(BaseModel):
    severe_consecutive_days: int
    count_in_progress_day: bool
    leave_counts_as_present: bool
    poll_interval_seconds: int
    alert_email_enabled: bool
    alert_recipients: list[str]
    data_mode: str
    collect_enabled: bool = True
    run_background_jobs: bool = True
    retention_days: int = 400
    digest_hour: int = 18
    alert_severe_immediate: bool = True


class SettingsIn(BaseModel):
    severe_consecutive_days: int | None = Field(default=None, ge=2, le=10)
    count_in_progress_day: bool | None = None
    leave_counts_as_present: bool | None = None


class SimpleResult(BaseModel):
    ok: bool
    message: str = ""


class CounselorClassOut(BaseModel):
    """后台「辅导员联系人」里的一行：一个班 + 它的收件位置。"""

    class_key: str
    class_name: str
    students: int = 0
    slot1: str = ""
    slot2: str = ""
    slot3: str = ""
    note: str = ""
    updated_at: str | None = None

    @property
    def emails(self) -> list[str]:
        return [s for s in (self.slot1, self.slot2, self.slot3) if s.strip()]


class CounselorClassIn(BaseModel):
    """保存一个班示示例辅导员邮箱（最多 3 个位置）。"""

    class_name: str = Field(min_length=1, max_length=128)
    slot1: str = Field(default="", max_length=128)
    slot2: str = Field(default="", max_length=128)
    slot3: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=255)


class NotifyResult(BaseModel):
    """一次通知分派的结果（供后台显示"发给了谁"）。"""

    ok: bool
    message: str = ""
    sent: int = 0
    recipients: list[str] = Field(default_factory=list)
    unassigned_classes: list[str] = Field(default_factory=list)

