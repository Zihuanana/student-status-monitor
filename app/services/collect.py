"""采集服务：按计划轮询各考勤源，把签到数据推进分析流水线。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time as ttime, timedelta

from sqlalchemy import select, update

from app.adapters.base import NormalizedCourse, NormalizedEvent
from app.adapters.generic import build_src2, build_src3
from app.adapters.hanboer import HanboerClient
from app.adapters.mock_source import MockSource, make_students
from app.core.config import settings
from app.core.logging_conf import get_logger
from app.models import AttendanceEvent, CollectRun, Course, Student, StudentCourse
from app.services.scope import (
    course_in_scope,
    describe_scope,
    describe_student_scope,
    split_by_scope,
)

log = get_logger("service.collect")


def _apply_scope(courses: list[NormalizedCourse]) -> list[NormalizedCourse]:
    """丢掉不属于本校区的课。

    翰博尔是**多校区共用**平台，接口没有校区参数（实测 orgName / buildingName /
    className / orgId 传了都被忽略），全平台的课一次性全返回。不在这里切掉，
    库里就会混进别的校区的学生 —— 实测混进来 7000+ 人，而本院只有 749 人。
    """
    kept, dropped = split_by_scope(courses)
    if dropped:
        log.info(
            "作用域过滤（关键词：%s）：保留 %d 门，丢弃 %d 门非本校区课程",
            describe_scope(), len(kept), len(dropped),
        )
    return kept


class Collector:
    """统一采集入口。

    mock 模式：由 :class:`MockSource` 现场生成数据（不联网），便于自测。
    live 模式：调用真实考勤接口。
    """

    def __init__(self, mock_student_count: int = 1200) -> None:
        self.mock: MockSource | None = None
        self._mock_students: list = []
        #: mock 模式生成多少名学生（见 _collect_mock 的说明）
        self.mock_student_count = mock_student_count
        #: 明细拉取的错误（由 _fetch_details 填，供 run_once 判断是否全挂）
        self._detail_errors: list[str] = []

    # ── 对外主入口 ────────────────────────────────────────
    async def run_once(
        self,
        db,
        job: str = "poll",
        now: datetime | None = None,
        history_days: int = 5,
        force: bool = False,
    ) -> dict:
        """执行一轮采集。任何异常都被捕获并记录，不抛出。

        ``force=True``：无视 ``COLLECT_ENABLED`` 直接采一轮。
        给**人工触发**用（``app.cli collect``、页面上的"立即采集"）——
        ``COLLECT_ENABLED`` 防的是**自动**轮询，不该把人的显式操作也挡掉。
        实测踩过：把它设成 false 后手工 `collect` 静默什么也没做，
        却打印"✅ 课程 0 门"，让人以为采集正常、只是今天没课。
        """
        from app.core.db import session_scope
        from app.services.ingest import (
            describe_dropped_ids,
            rebuild_from_events,
            start_id_scope_accounting,
            take_dropped_ids,
            upsert_courses,
            upsert_events,
            upsert_students,
        )

        now = now or datetime.now()
        run = CollectRun(source="all", job=job, started_at=now)
        db.add(run)
        db.commit()

        # 学号作用域会**静默丢弃**不合形态的学号；这里开一段记账窗口，
        # 把这轮挡掉了多少人写进采集消息与日志 —— 过滤器不可见才是真危险。
        start_id_scope_accounting()

        courses: list[NormalizedCourse] = []
        events: list[NormalizedEvent] = []
        message = ""
        ok = True
        try:
            source_errors: list[str] = []
            if settings.is_mock:
                courses, events = self._collect_mock(now, history_days=history_days)
            elif not settings.collect_enabled and not force:
                message = "采集已关闭（COLLECT_ENABLED=false）"
            else:
                # 顺序很重要：先落库课程，再抓明细。
                # 抓明细前要查库判断「这门课的明细是否已经同步过且过了回补窗口」，
                # 否则每轮都会把今天所有已结束的课重复抓一遍（约 7000 次/天无效请求）。
                stats_courses, source_errors = await self._fetch_statistics(now)
                if stats_courses:
                    courses = stats_courses
                    upsert_courses(db, stats_courses)
                    db.commit()
                events = await self._fetch_details(db, now)
                source_errors.extend(self._detail_errors)

                # ── 全部数据源失败 → 必须报告失败 ──────────────────
                # 否则"线上系统挂了"会被记成一次成功的空采集，
                # 老师看到的是"全员无异常"，实际上系统已经瞎了。
                #
                # ⚠ 判据**不能**带 `not courses and not events`（原来就是这么写的）：
                #   courses 常常是从**库里**读出来的（今天的课表已入库），
                #   所以即使接口 403 全挂，courses 也非空 —— 条件不成立 →
                #   静默记成成功。
                #   实测踩过：新服务器被平台限流 403，`app.cli collect` 却打印
                #   "✅ 课程 0 门，事件 0 条"，看不出任何异常。
                enabled_sources = 1 if (settings.hb_enabled and settings.hb_base_url) else 0
                enabled_sources += sum(
                    1 for a in (build_src2(), build_src3())
                    if getattr(a, "enabled", False)
                )
                if source_errors and (enabled_sources == 0 or len(source_errors) >= enabled_sources):
                    raise RuntimeError("全部数据源采集失败：" + "；".join(source_errors))
                if source_errors:
                    log.warning("部分数据源异常：%s", "；".join(source_errors))

            if courses and settings.is_mock:
                upsert_courses(db, courses)

            # 先把本批事件落库（幂等），再基于库内最新状态做缺勤补齐 ——
            # 顺序不能颠倒：infer 依赖「已观测到的学生」来求差集。
            if events:
                from app.services.ingest import (
                    infer_grade_from_student_no,
                    infer_major_code_from_student_no,
                )

                students = {e.student_no for e in events}
                # 接口若返回了姓名就用它（翰博尔实测返回 userName）；
                # mock 模式用内置姓名表。
                names: dict[str, str] = {}
                if settings.is_mock:
                    names = self._mock_name_map()
                else:
                    for e in events:
                        if e.student_name and e.student_no not in names:
                            names[e.student_no] = e.student_name

                upsert_students(
                    db,
                    [
                        {
                            "student_no": no,
                            "name": names.get(no, ""),
                            "grade": infer_grade_from_student_no(no),
                            "major_code": infer_major_code_from_student_no(no),
                            "major_name": "",
                            "class_name": "",
                            "org_name": "",
                        }
                        for no in students
                    ],
                )
                upsert_events(db, events)
                db.commit()

            # 关键补齐：整节课没来的学生往往不在接口返回里，
            # 用应到名册反推「应到未签到」，否则最该告警的人反而查无此人。
            # 注意这里**不能**放在 `if events:` 之内 —— 本批 events 为空
            # 恰恰是最需要补齐的情形。
            if not settings.is_mock and settings.collect_enabled:
                from app.services.infer import reconcile_absentees, roster_coverage

                with_class, total = roster_coverage(db)
                # 只有当"接口可能不返回未签到学生"时，缺勤反推才是必需的。
                # 翰博尔实测**会返回全部应到学生（含缺勤）**，此时缺名册不影响发现率，
                # 不该反复报警告（假警报会让真问题被忽略）。
                if total and not with_class and _events_look_incomplete(db):
                    log.warning(
                        "学生名册缺少班级信息，且考勤明细疑似不完整"
                        "（每人每课记录数明显少于应到人数），无法反推缺勤；"
                        "请导入含班级的名册，否则「整节课没来」的学生不会被发现"
                    )
                inferred = reconcile_absentees(
                    db,
                    now=now,
                    hours=settings.backfill_window_hours,
                    # 应到集合优先取「学生 → 课程」关系（选课名单导入建立），
                    # 班级名那条路在本项目**走不通**：翰博尔返回的 class_name
                    # 全是「公选课(2026...)」这种占位串，与名册的行政班名
                    # 对不上。名单导入之后这条路才真正有数据。
                    from_history=settings.infer_from_history,
                )
                if inferred:
                    events = list(events) + list(inferred)

            stats = rebuild_from_events(db, events, now=now)
            # ⚠ 只有在**真的采过**的时候才覆盖 message。
            #   原来无条件覆盖，于是走了「采集已关闭」分支时，
            #   "采集已关闭（COLLECT_ENABLED=false）" 被改写成
            #   "课程 0 门，事件 0 条"，CLI 打出 ✅ —— 又一次静默失效：
            #   看起来采了一轮、其实一次接口都没调。实测踩过。
            if not message:
                message = (
                    f"课程 {len(courses)} 门，事件 {len(events)} 条（新增 {stats['inserted']}），"
                    f"告警 {stats['alerts']}"
                )
            if source_errors:
                message += "；部分数据源异常：" + "；".join(source_errors)
        except Exception as exc:
            ok = False
            message = f"{type(exc).__name__}: {exc}"
            log.exception("采集失败：%s", exc)
            db.rollback()

        # 收走学号作用域的记账结果：挡掉的人必须出现在消息与日志里，
        # 否则"采集成功但少了 300 人"这种事故没有任何痕迹。
        dropped = take_dropped_ids()
        dropped_note = describe_dropped_ids(dropped)
        if dropped_note:
            message += "；" + dropped_note
            log.info("%s（规则：%s）", dropped_note, describe_student_scope())

        run.finished_at = datetime.now()
        run.ok = ok
        run.courses_seen = len(courses)
        run.events_upserted = len(events)
        run.message = message[:1000]
        db.add(run)
        db.commit()
        if not ok:
            log.error("本轮采集失败：%s", message)
        _ = session_scope  # 保留给未来在子线程写入
        return {
            "ok": ok,
            "message": message,
            "courses": len(courses),
            "events": len(events),
            "dropped_students": len(set(dropped.get("student") or [])),
            "dropped_events": len(dropped.get("event") or []),
        }

    # ── mock 模式 ─────────────────────────────────────────
    def _collect_mock(
        self, now: datetime, history_days: int = 5
    ) -> tuple[list[NormalizedCourse], list[NormalizedEvent]]:
        if self.mock is None:
            self.mock = MockSource()
            # 用 mock_student_count 而不是写死 1200：写死会让
            # `--students` 参数形同虚设（名册人数和考勤人数对不上）。
            self._mock_students = make_students(self.mock_student_count)
        assert self.mock is not None

        span = max(1, history_days)
        days = [now.date() - timedelta(days=k) for k in range(span - 1, -1, -1)]
        courses: list[NormalizedCourse] = []
        events: list[NormalizedEvent] = []
        for idx, stu in enumerate(self._mock_students):
            cs = self.mock.schedule_for(stu, idx, days)
            courses.extend(cs)
            events.extend(self.mock.events_for(stu, idx, cs, now))
        return courses, events

    def _mock_name_map(self) -> dict[str, str]:
        return {s.student_no: s.name for s in self._mock_students}

    # ── live 模式 ─────────────────────────────────────────
    async def _fetch_statistics(
        self, now: datetime
    ) -> tuple[list[NormalizedCourse], list[str]]:
        """拉取各数据源的课程列表。返回 (课程, 错误信息)。"""
        courses: list[NormalizedCourse] = []
        errors: list[str] = []

        if settings.hb_enabled and settings.hb_base_url:
            try:
                async with HanboerClient() as client:
                    day_start = datetime.combine(now.date(), datetime.min.time())
                    # 取到**今天结束**，而不是"此刻"。
                    # 只取到此刻的话，今天还没开始的课根本不会进库，
                    # 页面上的周课表就只能"上完课才看得见"。
                    # 取全天之后，今天的课一早就都在库里，未开始的显示成灰色。
                    day_end = datetime.combine(now.date(), ttime(23, 59, 59))
                    courses.extend(
                        await client.iter_statistics(start_time=day_start, end_time=day_end)
                    )
            except Exception as exc:
                errors.append(f"翰博尔·考勤汇总 {type(exc).__name__}: {exc}")
                log.error("翰博尔考勤汇总拉取失败：%s", exc)

        courses = _apply_scope(courses)
        for adapter in (build_src2(), build_src3()):
            if not getattr(adapter, "enabled", False):
                continue
            try:
                courses.extend(await adapter.iter_statistics())
            except Exception as exc:
                errors.append(f"{adapter.name}·课程列表 {type(exc).__name__}: {exc}")
                log.error("[%s] 课程列表拉取失败：%s", adapter.name, exc)

        return courses, errors

    async def _fetch_details(
        self, db, now: datetime
    ) -> list[NormalizedEvent]:
        """拉取今天课程的考勤明细（**含还没开始的课**）。

        为什么要连没开始的课一起抓：明细接口返回的是**全部应到名单**
        （含"缺勤"），所以对还没开始的课调一次，就能把
        「谁应该上这节课」提前建好选课关系 —— 页面于是能在上课前
        就把课表显示出来（未开始的格子标灰），而不是等课上完才看得见。

        抓取范围：
        - 今天未开始 / 进行中 → 每轮都抓（名单可能变，学生也可能刚签到）；
        - 已结束且在回补窗口内但未同步过 → 抓一次，兜住延迟上传；
        - 已结束且已同步过 → 跳过，避免每轮重复抓取。
        """
        window_start = now - timedelta(hours=settings.backfill_window_hours)
        events: list[NormalizedEvent] = []

        stmt = (
            select(Course)
            .where(
                Course.start_time.is_not(None),
                Course.class_date == now.date(),
                # 只有**有考勤源**的课才去抓明细。教务课表建的时段行
                # （covered=False）在翰博尔里根本不存在，拿它的 guid 去调
                # getAttendanceDetails 只会失败 —— 每轮几百次重试会把事件循环
                # 占满，网页直接 504（2026-10-12 实测踩过）。
                Course.covered.is_(True),
            )
            .order_by(Course.start_time)
        )
        todo: list[Course] = []
        for course in db.execute(stmt).scalars():
            if not course_in_scope(course):
                continue  # 库里残留的非本校区课程（历史脏数据），不抓
            if course.end_time and course.end_time < window_start:
                continue  # 太旧，已过回补窗口
            finished = bool(course.end_time and course.end_time <= now)
            if finished and course.detail_synced_at is not None:
                continue  # 已结束且同步过 → 无需重复抓取
            todo.append(course)

        synced: set[str] = set()
        #: 明细拉取的错误（供调用方判断"是不是全挂了"）。
        #: 原来这些错误**只写日志、不外传**，于是接口整体失败时
        #: 采集照样报成功、事件为 0 —— 静默失效。
        self._detail_errors = []
        if settings.hb_enabled and settings.hb_base_url and todo:
            try:
                async with HanboerClient() as client:
                    for course in todo:
                        try:
                            events.extend(
                                await client.iter_details(
                                    schedule_guid=course.schedule_guid
                                )
                            )
                            if course.end_time and course.end_time <= now:
                                synced.add(course.schedule_guid)
                        except Exception as exc:
                            self._detail_errors.append(
                                f"翰博尔·考勤详情 {type(exc).__name__}: {exc}"
                            )
                            log.error(
                                "翰博尔考勤详情拉取失败 %s：%s", course.schedule_guid, exc
                            )
            except Exception as exc:
                self._detail_errors.append(f"翰博尔·数据源不可用 {type(exc).__name__}: {exc}")
                log.error("翰博尔数据源不可用：%s", exc)

        # 源二 / 三：文档未到，按时间区间整段拉取
        for adapter in (build_src2(), build_src3()):
            if not getattr(adapter, "enabled", False):
                continue
            try:
                events.extend(await adapter.iter_details())
            except Exception as exc:
                self._detail_errors.append(f"{adapter.name}·考勤明细 {type(exc).__name__}: {exc}")
                log.error("[%s] 考勤明细拉取失败：%s", adapter.name, exc)

        if synced:
            mark_detail_synced(db, synced, now=now)
        return events

    # ── 不依赖接口的本地重算（用于排障） ──────────────────
    @staticmethod
    def rebuild_all(db, days: int = 60) -> int:
        from app.services.ingest import rebuild_daily

        now = datetime.now()
        return rebuild_daily(db, start=now.date() - timedelta(days=days), end=now.date(), now=now)


def _events_look_incomplete(db) -> bool:
    """判断考勤明细是否可能"漏人"。

    做法：抽样最近有应到人数的课程，比较「实际收到的学生记录数」
    与「课程自报的应到人数」。

    - 若接近或超过（翰博尔实测就是如此），说明接口返回全部应到学生，
      缺名册也无妨，不该报警告；
    - 若明显偏少，说明只返回有考勤动作的人，这时**必须**有班级名册才能反推缺勤。
    """
    from sqlalchemy import func

    from app.models import AttendanceEvent, Course

    samples = (
        db.query(Course.schedule_guid, Course.should_student_count)
        .filter(Course.should_student_count > 0)
        .order_by(Course.class_date.desc())
        .limit(20)
        .all()
    )
    if not samples:
        return False

    ratios = []
    for guid, should in samples:
        got = (
            db.query(func.count(AttendanceEvent.id))
            .filter(
                AttendanceEvent.schedule_guid == guid,
                AttendanceEvent.user_type == 2,
            )
            .scalar()
            or 0
        )
        if should:
            ratios.append(got / should)

    if not ratios:
        return False
    avg = sum(ratios) / len(ratios)
    # 低于 0.8 视为"可能漏人"
    return avg < 0.8


def unresolved_courses(db, now: datetime | None = None) -> list[Course]:
    """已结束但详情尚未成功同步的课程（用于回补）。

    同样只挑**有考勤源**的课：教务课表的时段行永远同步不了，
    留在候选里会每轮都去重试一遍（拖垮采集）。
    """
    now = now or datetime.now()
    stmt = (
        select(Course)
        .where(
            Course.end_time.is_not(None),
            Course.end_time <= now,
            Course.end_time >= now - timedelta(hours=settings.backfill_window_hours),
            Course.detail_synced_at.is_(None),
            Course.covered.is_(True),
        )
        .order_by(Course.end_time)
        .limit(200)
    )
    return list(db.execute(stmt).scalars())


def mark_detail_synced(db, guids: set[str], now: datetime | None = None) -> None:
    if not guids:
        return
    db.execute(
        update(Course).where(Course.schedule_guid.in_(guids)).values(detail_synced_at=now or datetime.now())
    )
    db.commit()


def pending_event_stats(db) -> dict:
    total = db.execute(select(AttendanceEvent.id)).scalars().all()
    _ = date
    return {"events": len(total)}


async def collect_loop(interval_seconds: int | None = None) -> None:
    """常驻轮询循环（由 FastAPI lifespan 启动）。

    每轮采集一次；每天顺带做一次数据清理与 WAL 合并，
    避免长期运行把服务器磁盘写满（磁盘仅约 10G 可用）。
    """
    from app.core.db import SessionLocal, init_db
    from app.services.health import alert_if_unhealthy, daily_task_due, should_check
    from app.services.maintenance import checkpoint, prune

    interval = interval_seconds or settings.poll_interval_seconds
    collector = Collector()
    init_db()
    # 首轮稍等，避免与应用启动争抢
    await asyncio.sleep(3)

    last_prune_day: date | None = None
    last_health_check: datetime | None = None
    last_digest: datetime | None = None
    while True:
        now = datetime.now()
        db = SessionLocal()
        try:
            await collector.run_once(db, job="poll", now=now)

            # 采集体检：系统"自己坏了"也要能被发现（静默失明最危险）
            if should_check(now, last_health_check):
                last_health_check = now
                try:
                    alert_if_unhealthy(db, now=now)
                except Exception as exc:
                    log.exception("采集体检失败：%s", exc)

            # 每日异常汇总：普通「异常」不即时打扰，攒到固定时刻发一封
            if daily_task_due(now, settings.digest_hour, last_digest):
                last_digest = now
                try:
                    from app.services.alerts import daily_digest

                    ok, msg = daily_digest(db, now=now)
                    log.info("每日异常汇总：%s（%s）", "已发送" if ok else "未发送", msg)
                except Exception as exc:
                    log.exception("每日异常汇总失败：%s", exc)

            if last_prune_day != now.date():
                try:
                    prune(db, retention_days=settings.retention_days, now=now)
                    checkpoint(db)
                except Exception as exc:
                    log.exception("数据清理失败：%s", exc)
                last_prune_day = now.date()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("轮询循环异常：%s", exc)
        finally:
            db.close()
        await asyncio.sleep(max(30, interval))

    _ = Student, StudentCourse
