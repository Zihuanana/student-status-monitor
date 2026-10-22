"""应用配置：全部通过环境变量 / .env 注入。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:  # 只用于注解，运行期不需要真的 import re
    import re

# app/core/config.py → 上溯三级才是项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("APP_ENV_FILE", str(BASE_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 运行模式 ──────────────────────────────────────────
    data_mode: str = "mock"  # mock | live
    debug: bool = False
    log_level: str = "INFO"

    # ── 数据库 ────────────────────────────────────────────
    #: 隐藏路径下的前端产物也让后端提供（生产由 nginx 反代过来）
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'monitor.db'}"

    # ── 采集调度 ──────────────────────────────────────────
    poll_interval_seconds: int = 300
    poll_lookback_minutes: int = 30
    backfill_window_hours: int = 6
    collect_concurrency: int = 6
    http_timeout_seconds: int = 20
    http_max_retries: int = 3
    collect_enabled: bool = True
    #: 是否启动后台常驻轮询循环。与 COLLECT_ENABLED 分开：
    #: 关闭它仍可用「手动采集」按钮 / CLI 拉数据（本地测试与排障需要）。
    run_background_jobs: bool = True
    #: 数据保留天数。采集是持续轮询，超期数据会被每日清理，避免 SQLite 无限增长。
    #: 默认 400 天可覆盖一个完整学年（含跨年课表）。
    retention_days: int = 400

    # ── 判定规则 ──────────────────────────────────────────
    severe_consecutive_days: int = 3
    count_in_progress_day: bool = False
    leave_counts_as_present: bool = True
    present_statuses: str = "出勤,迟到,早退,迟到早退"
    #: 一节课缺勤人数超过该比例时不做「应到未签到」推断。
    #: 这是防止「选课名单不准」被放大成大规模误报的保险丝；
    #: 阈值太紧会让小班遇到集体缺勤时整批漏判，故默认放宽到 0.8。
    infer_max_ratio: float = 0.8
    #: 没有班级名单时，用**历史上观测到上过该课的学生**作为应到集合来反推缺勤。
    #: 默认 **False**，因为它有一个致命局限：**发现不了从来没来过的学生**
    #: —— 而那正是本系统最该抓的人（学生一次都没出现，就永远不会进入这个集合）。
    #: 需要权威应到名单时，用 ``import enrollments 选课名单.xlsx``，
    #: 它建立的是"谁选了这门课"的事实关系，不受此开关影响。
    infer_from_history: bool = False

    # ── 四级预警（2026-10-13 按学校口径）────────────────────
    #: ① 学生无记录预警：连续多少个「有效统计日」无有效考勤记录即提醒。
    #: 有效统计日 = 当日有课 **且** 相关课程确实产生过考勤数据 ——
    #: 课没有考勤数据的日子不算（那属于覆盖不足，不是学生缺勤）。
    no_record_days: int = 3
    #: ② 监测覆盖不足提醒的统计周期（天）：这段时间内有课、但相关课程
    #: 在各考勤平台都没有可用数据 → 提示示例辅导员用其它方式核实。
    monitor_coverage_days: int = 7
    #: ③ 长期缺少记录提醒的回看窗口（自然日）。
    long_term_missing_days: int = 14
    #: ③ 判定「记录持续偏少」的出勤率上限：有效统计日里有过记录的比例
    #: 低于该值即提醒。默认 30%（明显低于正常到课率，又不至于把偶发缺勤算进来）。
    long_term_missing_ratio: float = 0.3
    #: ③ 至少要有这么多「有效统计日」才下结论。
    #: 只有 1 天看得到、恰好那天没记录，那是偶发、不是"长期"
    #: （实测踩过：新学期头几天全班会被集体误报）。
    long_term_missing_min_days: int = 3
    #: ③ 同时要求"有记录的天数"不超过该值，避免长期缺勤的人因窗口很长而被漏掉。
    long_term_missing_max_records: int = 3

    # ── 隐藏路径与口令 ────────────────────────────────────
    app_base_path: str = "3425jh3g4ryugu34gurg3qk4vjhv4rhq3vrkq3v4kgvqky3ugry3qgy4rgug4k3yuvewehje"
    gate_password: str = "2026zchnxsztjcxt"
    session_secret: str = "change-me"
    session_ttl_hours: int = 12
    cookie_name: str = "smon_session"
    #: 会话 Cookie 是否只在 HTTPS 下传输。
    #: 生产环境（nginx 终止 TLS）应设为 true；用 http://IP:端口 直连调试时须设为 false，
    #: 否则浏览器/客户端不会回传 Cookie，会一直 401。
    session_cookie_secure: bool = False
    #: 允许的 Host 头（逗号分隔），留空表示不校验
    trusted_hosts: str = ""

    # ── 邮件告警 ──────────────────────────────────────────
    alert_email_enabled: bool = False
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    alert_recipients: str = ""
    alert_cooldown_hours: int = 24
    alert_severe_immediate: bool = True
    #: 普通「异常」的每日汇总发信时刻（24 小时制整点）。
    #: 设为负数则关闭定时汇总。默认 18 点——课程基本结束、考勤数据已回传。
    digest_hour: int = 18

    # ── 考勤源 1：翰博尔 ──────────────────────────────────
    hb_base_url: str = ""
    hb_access_key: str = ""
    hb_access_key_secret: str = ""
    hb_app_source: str = ""
    hb_enabled: bool = True
    #: 是否校验考勤平台的 TLS 证书。**默认校验**。
    #: 内网部署时若学校出口有 HTTPS 解密/审计设备，证书会被替换成自签的，
    #: 表现为 `SSLCertVerificationError`，采集一条数据都拿不到。
    #: 这种情况先找网络管理员要根证书；实在拿不到才设 HB_VERIFY_TLS=false
    #: （只影响对考勤平台的请求，且会失去中间人防护，务必知悉）。
    hb_verify_tls: bool = True
    #: 校区作用域关键词（逗号分隔）。翰博尔平台是**多校区共用**的，接口没有
    #: 校区过滤参数，只能拿「节次名」这个天然带校区的字段来切：海南校区的
    #: 节次叫「海南第一二节」，其它校区叫「第一二节」。
    #: 留空 = 不过滤，全平台数据都收（仅用于排障）。
    hb_campus_keywords: str = "海南"
    #: 学号作用域开关。上面那个关键词切的是**课程**，这里切的是**学生**：
    #: 课程切分解决"哪些课是我们的"，学号切分解决"哪些人是我们的"。
    #: 两道闸门都要有 —— 课程切分依赖上游的节次命名，一旦平台改名就会
    #: 静默失效，而学号格式是学校定的、不会因为平台改版而变。
    hb_student_id_scope_enabled: bool = True
    #: 本院学号的完整形态（正则，已锚定首尾）。
    #: 段落：年份(4) + 学院码 ``29``（海南国际学院） + 专业族 ``0x``
    #: + ``3`` + 序号(3) + **可选**尾字母。
    #:
    #: 三个实际坑，都体现在这条正则里：
    #: 1. 前缀写成可选组 ``(cuc)?`` 并加 ``(?i)``：接口返回 ``cuc2023…``，
    #:    库里存剥掉前缀的 ``2023…``（``normalize_student_no`` 按大小写不敏感剥）。
    #: 2. 尾字母必须写成 ``[0-9A-Z]?``（可选）：上线库里实测有 96 名本院学生
    #:    学号末尾是数字或干脆没有尾字母（``202400000001``）。把它们排除掉
    #:    不是"过滤别院"，而是**删掉本院真人**。
    #: 3. 真正区分学院的是第 5~6 位的 ``29``：实测全校 5957 人里，
    #:    学院码为 29 的一共 1033 人，其余 4924 人是 202420/202311/202408…
    #:    等别的学院。
    #: **不要**放宽成 ``\w`` 之类：这条规则会真的删数据
    #: （``app.cli students --purge``）。
    hb_student_id_pattern: str = r"(?i)^(cuc)?202\d290\d3\d{3}[0-9A-Z]?$"

    # ── 考勤源 2 / 3 ──────────────────────────────────────
    src2_enabled: bool = False
    src2_base_url: str = ""
    src2_access_key: str = ""
    src2_access_key_secret: str = ""

    src3_enabled: bool = False
    src3_base_url: str = ""
    src3_access_key: str = ""
    src3_access_key_secret: str = ""

    # ── 中传教务接口（主轴数据源）────────────────────────────
    #
    # 架构定位（2026-10 确定的方针）：
    #   **教务是主轴** —— 学生的「应到」课表全部由这里生成；
    #   畅课_签到 / 翰博尔 这些「实到」签到源再适配到主轴上来。
    #
    # 六个数据源（前五个是教务，最后是签到）：
    #   ① 公共_校历            → 教学开始日期（第一周周一）
    #   ② 本科生院_本专科生基本信息 → 姓名 学号 专业 年级 班号
    #   ③ 本研_学生选课         → 学号 学年学期 选课类别 教学班编码 课程编码
    #   ④ 本研_课表            → 课程编码 课序号 名称 周次 节次 星期 教室 **记录id**
    #   ⑤ 畅课_签到            → 记录id 学号 教学班编码 签到状态 签到时间 上课时间
    #
    # 为什么「记录id」值得单独说：它把课表行和签到记录**精确**连起来
    #   （一个记录id = 一个课次 = 周次×星期×节次，n 个学号共享它），
    #   于是不需要靠"上课时间反推节次"这种猜测式对齐。
    #   实测确认课表接口能返回它，所以课次主键优先用它。
    cuc_enabled: bool = False
    cuc_base_url: str = ""
    cuc_token: str = ""
    cuc_app_key: str = ""
    cuc_app_secret: str = ""
    #: 附加请求头，格式 ``头名=值``，多个用英文分号分隔。
    #: 对方常要求带 appId / 渠道号之类，做成配置免得改代码。
    cuc_extra_headers: str = ""
    cuc_verify_tls: bool = True
    cuc_http_timeout_seconds: float = 30.0
    cuc_http_max_retries: int = 2

    #: 各数据源的路径（相对 base_url）。留空 = 该源不启用。
    cuc_path_calendar: str = ""
    cuc_path_students: str = ""
    cuc_path_enrollments: str = ""
    cuc_path_timetable: str = ""
    cuc_path_checkins: str = ""

    #: 学年学期，格式与对方一致（例 ``2026-2027学年秋季学期``）。
    #: 留空则由校历接口推断；推断不出时该值为空，同步会报错而不是猜。
    cuc_term: str = ""

    #: 字段名映射。左边是我们的字段，右边是对方 JSON 里的键。
    #: 格式 ``我们的字段=对方的键``，多个用英文分号分隔。
    #: 例：``student_no=sno;name=xm;major=zymc;class_name=bh``
    #: 留空则用 adapters/cuc.py 里的候选键猜测（多数情况够用）。
    cuc_fields_calendar: str = ""
    cuc_fields_student: str = ""
    cuc_fields_enrollment: str = ""
    cuc_fields_timetable: str = ""
    cuc_fields_checkin: str = ""

    #: demo 模式只拉这个学号的数据（填自己的学号，用于核对字段）。
    #: 留空则 demo 直接报错退出 —— 防止误触发全校数据拉取。
    cuc_demo_student_no: str = ""

    #: 分页
    cuc_page_size: int = 500
    cuc_max_pages: int = 200

    # ── 学号切片与专业名 ──────────────────────────────────
    student_id_year_slice: str = "0:4"
    student_id_major_slice: str = "6:9"
    #: 专业代码 → 专业名称。学号里只有代码（本院是 013/023/033），
    #: 不映射的话页面上全是「2023级专业013」，老师看不懂。
    #: 格式 ``代码=名称``，多个用逗号分隔。启动时同步进 major_aliases 表。
    major_aliases: str = "013=智能科学与技术,023=视觉传达设计,033=数字媒体技术"
    #: 学期**第 1 周的周一**（ISO 日期）。教务处课表里的「5-7周」要靠它
    #: 才能折算成具体日期。**不要写死在代码里** —— 每学期都会变，
    #: 写死就会静默把课次算到错误的日期上（表现为"那天明明有课却显示无课"）。
    #: 2026 秋学期：老师确认 2026-10-05 ~ 10-11 是**第 5 周**，
    #: 所以第 1 周周一 = 2026-08-31。
    #: ⚠ 不要拿"翰博尔首批课次（09-07）"当第 1 周 —— 那其实是第 2 周，
    #: 按它折算会把所有课次整体推后一周（计网的周四 3-4 节就会从
    #: 10-08 跑到 10-15）。
    semester_week1_monday: str = "2026-08-31"

    # ─────────────────────────────────────────────────────
    @field_validator("app_base_path")
    @classmethod
    def _strip_slashes(cls, v: str) -> str:
        return v.strip().strip("/")

    @field_validator("hb_student_id_pattern")
    @classmethod
    def _valid_student_id_pattern(cls, v: str) -> str:
        """写错正则要在**启动时**就报，不能等到采集删数据那一刻。"""
        text = (v or "").strip()
        if not text:
            return ""
        import re

        try:
            re.compile(text)
        except re.error as exc:
            raise ValueError(
                f"HB_STUDENT_ID_PATTERN 不是合法正则：{exc}；当前值 {text!r}"
            ) from exc
        return text

    @property
    def present_status_set(self) -> set[str]:
        return {s.strip() for s in self.present_statuses.split(",") if s.strip()}

    @property
    def recipient_list(self) -> list[str]:
        raw = self.alert_recipients.replace(";", ",")
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        raw = self.trusted_hosts.replace(";", ",")
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def campus_keyword_list(self) -> list[str]:
        raw = self.hb_campus_keywords.replace("，", ",").replace("、", ",")
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def semester_week1_date(self):
        """解析成 ``date``。写错格式时抛错（宁可启动失败，也不要静默算错日期）。"""
        from datetime import date as _date

        text = (self.semester_week1_monday or "").strip()
        if not text:
            return None
        try:
            return _date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"SEMESTER_WEEK1_MONDAY 需要 ISO 日期（如 2026-09-07），当前值 {text!r}"
            ) from exc

    @property
    def student_id_like(self) -> "re.Pattern[str] | None":
        """编译好的学号形态正则。``None`` = 不过滤。

        关闭开关或正则留空都返回 ``None`` —— 调用方只需判空，
        不必各自去读开关（少一处漏判就少一次误删）。
        """
        import re

        if not self.hb_student_id_scope_enabled:
            return None
        text = (self.hb_student_id_pattern or "").strip()
        if not text:
            return None
        try:
            return re.compile(text)
        except re.error:
            # 正常情况下 __init__ 的校验器已经挡住了，这里只是兜底：
            # 宁可不过滤，也不要在采集线程里抛异常打断整轮采集。
            return None

    @property
    def major_alias_map(self) -> dict[str, str]:
        """把 ``013=智能科学与技术,023=视觉传达设计`` 解析成字典。"""
        raw = self.major_aliases.replace("，", ",").replace("、", ",")
        out: dict[str, str] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            code, _, name = item.partition("=")
            code, name = code.strip(), name.strip()
            if code and name:
                out[code] = name
        return out

    @property
    def is_mock(self) -> bool:
        return self.data_mode.strip().lower() != "live"

    def slice_pair(self, which: str = "year") -> tuple[int, int]:
        """把 '0:4' 这样的切片字符串解析成 (start, stop)。"""
        raw = self.student_id_year_slice if which == "year" else self.student_id_major_slice
        try:
            a, _, b = raw.partition(":")
            return int(a or 0), int(b) if b else 0
        except ValueError:
            return (0, 4) if which == "year" else (4, 6)

    def ensure_dirs(self) -> None:
        if self.database_url.startswith("sqlite"):
            path = self.database_url.split("///", 1)[-1]
            if path and path != ":memory:":
                p = Path(path)
                if not p.is_absolute():
                    p = BASE_DIR / p
                p.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
