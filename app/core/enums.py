"""领域枚举：签到状态、考勤源、学生状态。"""

from __future__ import annotations

from enum import Enum


class Source(str, Enum):
    """考勤软件来源。后续新增两种考勤软件时在此扩展。"""

    HANBOER = "hanboer"  # 翰博尔 智慧课堂
    SRC2 = "src2"  # 待信息化处提供
    SRC3 = "src3"  # 待信息化处提供

    @property
    def label(self) -> str:
        return {
            Source.HANBOER: "翰博尔智慧课堂",
            Source.SRC2: "考勤系统二",
            Source.SRC3: "考勤系统三",
        }[self]


class AttendStatus(str, Enum):
    """接口返回的原始考勤状态（翰博尔口径）。

    正常、缺勤、迟到、早退、迟到早退、请假
    """

    PRESENT = "出勤"
    ABSENT = "缺勤"
    LATE = "迟到"
    EARLY_LEAVE = "早退"
    LATE_AND_EARLY = "迟到早退"
    LEAVE = "请假"
    UNKNOWN = "未知"

    @classmethod
    def parse(cls, raw: str | None) -> "AttendStatus":
        if not raw:
            return cls.UNKNOWN
        text = str(raw).strip()
        for member in cls:
            if member.value == text:
                return member
        # 容错：接口可能返回「正常」等别名
        alias = {"正常": cls.PRESENT, "已签到": cls.PRESENT, "旷课": cls.ABSENT}
        return alias.get(text, cls.UNKNOWN)

    @property
    def is_present(self) -> bool:
        """是否算「已到」（含迟到/早退，按配置可调）。"""
        from app.core.config import settings

        if self in (AttendStatus.PRESENT, AttendStatus.UNKNOWN):
            return self is AttendStatus.PRESENT
        return self.value in settings.present_status_set


class SessionState(str, Enum):
    """单个学生「一节课」的签到结论。"""

    PENDING = "进行中"  # 课还没结束，尚不能判定
    PRESENT = "已签到"
    MISSED = "未签到"
    LEAVE = "请假"
    UPCOMING = "未开始"


class StudentState(str, Enum):
    """学生卡片状态（对外展示的 5 种）。"""

    ACTIVE = "活跃"
    NO_CLASS = "无课"
    TODAY_NOT_SIGNED = "今日未签到"
    ABNORMAL = "异常"
    SEVERE = "严重异常"

    @property
    def level(self) -> str:
        """风险等级：ok / warn / danger。"""
        if self is StudentState.SEVERE:
            return "danger"
        if self is StudentState.ABNORMAL:
            return "warn"
        return "ok"

    @property
    def priority(self) -> int:
        """判定优先级，数值越大越优先。"""
        return {
            StudentState.SEVERE: 50,
            StudentState.ABNORMAL: 40,
            StudentState.TODAY_NOT_SIGNED: 30,
            StudentState.ACTIVE: 20,
            StudentState.NO_CLASS: 10,
        }[self]


class AlertKind(str, Enum):
    """告警类型（四级预警，2026-10-13 按学校口径改造）。

    前两种是**学生**侧的（学生确实缺勤），后两种是**数据/系统**侧的
    （我们还判断不了这个学生，或数据本身有问题）。这个区分很重要：

    - 学生侧 → 提示示例辅导员联系学生；
    - 数据侧 → 提醒管理员修数据（课表缺失、接口失败），
      **绝不能混进学生异常名单**，否示示例辅导员会去追问一个根本没问题的学生。
    """

    #: ① 学生无记录预警：连续 3 个"有效统计日"均无有效考勤记录
    #: （有效统计日 = 该生当日有课，且相关课程在各考勤平台确实产生过考勤数据）
    NO_RECORD = "学生无记录预警"
    #: ③ 长期缺少记录提醒：最近 14 个自然日内有课且课程有可用考勤数据，
    #: 但个人有效考勤记录持续偏少（未达连续 3 个有效统计日）
    LONG_TERM_MISSING = "长期缺少记录提醒"
    #: ② 监测覆盖不足提醒：7 天统计周期内有课，但相关课程在各平台均无可用考勤数据
    #: → 系统看不到这个学生，**不判他缺勤**
    LOW_COVERAGE = "监测覆盖不足提醒"
    #: ④ 数据异常提醒：课表数据缺失、课程信息无法匹配、接口同步失败、考勤数据异常
    DATA_ANOMALY = "数据异常提醒"

    # ── 旧口径（保留，仅用于读取历史告警行）──────────────────
    #: 旧版按"最近一个有课日整日无签到"生成的告警。新规则下不再产生，
    #: 但库里已有这类记录，页面/查询仍要能显示，所以枚举值不能删。
    ABNORMAL = "异常"
    #: 旧版的"严重异常"。与 NO_RECORD 同义（连续 ≥3 个有课日整日无签到），
    #: 新代码统一用 NO_RECORD。
    SEVERE = "严重异常"

    @property
    def is_student_side(self) -> bool:
        """是否属于"学生确实缺勤"这一类（对示示例辅导员要联系学生）。"""
        return self in (AlertKind.NO_RECORD, AlertKind.LONG_TERM_MISSING,
                        AlertKind.SEVERE, AlertKind.ABNORMAL)

    @property
    def is_data_side(self) -> bool:
        """是否属于"数据/系统问题"这一类（对应管理员要修数据）。"""
        return self in (AlertKind.LOW_COVERAGE, AlertKind.DATA_ANOMALY)

