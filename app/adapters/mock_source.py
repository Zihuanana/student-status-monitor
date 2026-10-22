"""模拟考勤源：在没有任何真实接口凭据时，生成可复现的仿真数据。

用途：
1. 前端联调、UI 走查（能稳定产出 5 种状态的学生）；
2. 状态机回归测试。

它不联网，仅按固定随机种子生成课表与签到记录。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.adapters.base import NormalizedCourse, NormalizedEvent
from app.core.enums import Source

SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
GIVEN_1 = "伟芳娜秀敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉萍红娥玲芬燕彬宇晨曦子梓一若思梦欣怡嘉佳宁安可心语若彤雨泽沐宸亦辰逸然"
GIVEN_2 = "华强伟峰宁静怡然泽宇轩辰彤欣佳琪浩博文昊天天琪雨晨阳月明志远航宁萱瑶琳玥彤"

MAJORS = [
    ("013", "智能科学与技术"),
    ("023", "视觉传达设计"),
    ("033", "数字媒体技术"),
    ("043", "人工智能"),
    ("053", "数据科学与大数据技术"),
    ("063", "网络与新媒体"),
]
GRADES = [2021, 2022, 2023, 2024]
#: 学院码（学号第 5~6 位）。本院 = 29（海南国际学院）。
MOCK_COLLEGE_CODE = "29"
#: ⚠ 学号里**没有**独立的"学院标记位"，别再往中间插一个 3。
#:
#: 真实学号是 12 位数字 + 尾字母：``2024 29 013 001 N``
#:   = 年级(4) + 学院码(2) + 专业码(3) + 序号(3) + ``N``
#: 而 ``013 001`` 会被误读成 ``013`` + 标记 ``3`` + 序号 ``01``，看起来
#: "也说得通" —— 这正是当初写成 13 位（``2024000000011N``）却没被发现的原因：
#: 它只在专业码为 03x 时**长得像**对的，但位数多一位，过不了
#: ``HB_STUDENT_ID_PATTERN``，被静默挡掉。
#: ``tests/test_mock_source.py`` 钉住了"位数必须是 12"。
#: 序号固定 3 位（每届每专业最多 999 人）。
MOCK_SEQ_WIDTH = 3
BUILDINGS = ["逸夫楼", "博学楼", "综合楼", "信息楼", "第二教学楼"]
COURSE_NAMES = [
    "高等数学", "线性代数", "大学物理", "程序设计基础", "数据结构",
    "操作系统", "计算机网络", "机器学习", "数字图像处理", "数据库原理",
    "离散数学", "概率论与数理统计", "人工智能导论", "软件工程", "编译原理",
]
NODE_NAMES = ["第一节", "第二节", "第三节", "第四节", "第五节"]
TEACHERS = ["0107t1", "0231t2", "0455t3", "0612t4", "0788t5", "0912t6"]


@dataclass(slots=True)
class MockStudent:
    student_no: str
    name: str
    grade: str
    major_code: str
    major_name: str
    class_name: str
    org_name: str


def make_students(count: int = 1200, seed: int = 20260101) -> list[MockStudent]:
    """生成学生名册。

    学号**刻意按真实形态生成**：``2024 29 013 001 N``
    = 年级(4) + 学院码(2，本院 29) + 专业码(3) + 序号(3) + 尾字母 ``N``
    （共 12 位数字 + 1 位字母）。

    这样 mock 数据能穿过 ``HB_STUDENT_ID_PATTERN`` 那道闸门，也能被
    ``STUDENT_ID_MAJOR_SLICE=6:9`` 正确切出专业码 —— 否则一开学号作用域，
    模拟学生会被全员挡掉，演示与自测直接失效。

    ⚠ **踩过的坑**（``tests/test_mock_source.py`` 钉住了，改动前先看那里）：

    1. 曾经写成 13 位（``2024000000011N``）—— 多插了一个"学院标记位"。
       来源是把 ``013 001`` 误读成 ``013`` + 标记 ``3`` + 序号 ``01``：
       它在专业码为 03x 时**长得像**对的，所以一直没被发现，
       直到 2021 级的 ``2024000000011N`` 才暴露。位数错一位就过不了正则，
       学生被静默过滤，表现是"seed 报成功、页面 0 人"。
    2. 序号必须按 **(年级, 专业)** 计数，不能只按专业计数 ——
       只按专业计数时累计会破 999，序号涨到 4 位，同样过不了正则。
       三位序号上限 999，够用（每届每专业不会超过这个数）。
    """
    rng = random.Random(seed)
    students: list[MockStudent] = []
    # 轮转分配而不是"每组固定人数再截断"：截断在 count 不能整除时会给少
    # （要 100 给 96），而且会把排在后面的专业整个切掉。
    seq: dict[tuple[int, str], int] = {}
    klass_no: dict[tuple[int, str], int] = {}
    groups = [(grade, code, mname) for grade in GRADES for code, mname in MAJORS]
    for i in range(count):
        grade, code, mname = groups[i % len(groups)]
        key = (grade, code)
        seq[key] = seq.get(key, 0) + 1
        if key not in klass_no:
            klass_no[key] = rng.randint(1, 3)
        student_no = (
            f"{grade}{MOCK_COLLEGE_CODE}{code}"
            f"{seq[key]:0{MOCK_SEQ_WIDTH}d}N"
        )
        name = rng.choice(SURNAMES) + rng.choice(GIVEN_1)
        if rng.random() < 0.55:
            name += rng.choice(GIVEN_2)
        students.append(
            MockStudent(
                student_no=student_no,
                name=name,
                grade=str(grade),
                major_code=code,
                major_name=mname,
                class_name=f"{mname}{grade % 100}{klass_no[key]}班",
                org_name="海南国际学院",
            )
        )
    return students


class MockSource:
    """按学生课表生成签到事件，并刻意制造若干异常样本。"""

    def __init__(self, seed: int = 20260101) -> None:
        self.rng = random.Random(seed + 7)
        self.source = Source.HANBOER.value
        #: 学生的「人设」：决定其签到行为
        self._persona: dict[str, str] = {}

    def persona_of(self, student_no: str, index: int) -> str:
        if student_no not in self._persona:
            # 约 6% 严重异常、8% 异常、12% 偶尔缺勤，其余正常
            bucket = index % 100
            if bucket < 6:
                persona = "severe"
            elif bucket < 14:
                persona = "abnormal"
            elif bucket < 26:
                persona = "flaky"
            elif bucket < 32:
                persona = "leave"
            else:
                persona = "good"
            self._persona[student_no] = persona
        return self._persona[student_no]

    def build_course(
        self,
        class_date: date,
        node_index: int,
        major_name: str,
        grade: str,
        klass: str,
        seq: int,
    ) -> NormalizedCourse:
        start = datetime.combine(class_date, time(8 + node_index * 2, 0))
        end = start + timedelta(minutes=95)
        building = self.rng.choice(BUILDINGS)
        room = f"{self.rng.choice('QRST')}{self.rng.randint(1, 5)}{self.rng.randint(10, 49)}"
        course = self.rng.choice(COURSE_NAMES)
        guid = f"mock-{class_date:%Y%m%d}-{node_index}-{seq:05d}"
        return NormalizedCourse(
            schedule_guid=guid,
            source=self.source,
            title=f"{room}{course}{class_date:%Y-%m-%d}",
            course_name=course,
            teacher_name=self.rng.choice(TEACHERS),
            classroom_name=room,
            classroom_no=room,
            building_name=building,
            week_name=f"第{class_date.isocalendar().week}周",
            node_name=NODE_NAMES[node_index % len(NODE_NAMES)],
            class_name=klass,
            org_name="海南国际学院",
            start_time=start,
            end_time=end,
            should_student_count=90,
        )

    def schedule_for(
        self, student: MockStudent, index: int, days: list[date]
    ) -> list[NormalizedCourse]:
        """给一个学生排课：每个有课日 1~2 节课。"""
        courses: list[NormalizedCourse] = []
        rng = random.Random(hash(student.student_no) & 0xFFFFFFFF)
        for d in days:
            if d.weekday() >= 5:  # 周末不上课
                continue
            # 每周固定节奏：约 60% 的周中日子有课
            if rng.random() > 0.6:
                continue
            n = 1 if rng.random() < 0.7 else 2
            for k in range(n):
                node_index = rng.randint(0, 4)
                courses.append(
                    self.build_course(
                        d, node_index, student.major_name, student.grade,
                        student.class_name, index + k,
                    )
                )
        return courses

    def events_for(
        self,
        student: MockStudent,
        index: int,
        courses: list[NormalizedCourse],
        now: datetime,
    ) -> list[NormalizedEvent]:
        """按人设决定每节课的签到结果。"""
        persona = self.persona_of(student.student_no, index)
        events: list[NormalizedEvent] = []
        for c in courses:
            assert c.start_time and c.end_time
            if c.start_time > now:
                continue  # 未来的课不产生记录
            status, sign_in = self._decide(persona, c, now)
            events.append(
                NormalizedEvent(
                    source=self.source,
                    schedule_guid=c.schedule_guid,
                    student_no=student.student_no,
                    class_date=c.start_time.date(),
                    attend_status=status,
                    sign_in_time=sign_in,
                    node_no=c.node_name,
                    classroom_no=c.classroom_no,
                    user_type=2,
                    raw={"mock": True},
                )
            )
        return events

    def _decide(
        self, persona: str, course: NormalizedCourse, now: datetime
    ) -> tuple[str, datetime | None]:
        assert course.start_time and course.end_time
        started = course.start_time <= now
        ended = course.end_time <= now

        if persona == "severe":
            return ("缺勤", None)
        if persona == "abnormal":
            return ("缺勤", None)
        if persona == "leave":
            return ("请假", None)
        if persona == "flaky":
            if self.rng.random() < 0.35:
                return ("缺勤", None)
        # 正常学生：课已结束则必到；进行中按概率已到
        if not started:
            return ("出勤", None)
        if not ended and self.rng.random() < 0.45:
            # 课还没结束，暂时没有签到 —— 用于制造「今日未签到」
            return ("", None)
        roll = self.rng.random()
        offset = timedelta(minutes=self.rng.randint(-5, 12))
        if roll < 0.86:
            return ("出勤", course.start_time + offset)
        if roll < 0.93:
            return ("迟到", course.start_time + timedelta(minutes=self.rng.randint(13, 25)))
        if roll < 0.97:
            return ("早退", course.start_time + offset)
        return ("请假", None)
