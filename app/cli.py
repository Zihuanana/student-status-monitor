"""命令行工具。

    python -m app.cli seed [--students 1200] [--days 20]   # 生成 mock 数据
    python -m app.cli serve [--host 0.0.0.0] [--port 8000] [--reload]
    python -m app.cli collect                              # 手动跑一轮采集
    python -m app.cli rebuild [--days 60]                  # 用库内数据重算判定
    python -m app.cli sign --path /attendance/... [--param k=v ...]  # 生成带签名的调试 URL
    python -m app.cli state [--limit 20]                   # 打印状态分布
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime

from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.core.logging_conf import get_logger, setup_logging

log = get_logger("cli")

#: ``students --purge`` 的防误删阈值：删除人数达到这个绝对量、且占比达到
#: ``BIG_PURGE_RATIO`` 时，要求额外加 ``--yes``。
#: 依据：线上库首跑时要删 4924/5957 = 83%，而"正则写错一位"与"上游真灌进来
#: 一堆外院学生"在报告上长得一模一样。
BIG_PURGE_MIN = 100
BIG_PURGE_RATIO = 0.3


def cmd_seed(args: argparse.Namespace) -> int:
    from app.adapters.mock_source import MAJORS, make_students
    from app.models import MajorAlias, Student
    from app.services.collect import Collector
    from app.services.ingest import upsert_students
    from app.services.scope import describe_student_scope, student_id_in_scope

    init_db()
    db = SessionLocal()
    try:
        students = make_students(args.students)

        # 先自查学号作用域：生成的学号若过不了闸门，后面会被静默挡掉，
        # 表现为"seed 说成功、页面 0 人"—— 这是最难查的一类失效。
        # 实测踩过：学号曾写成 13 位，全员被挡。
        # 所以在写库前就报出来，而不是等页面空了再回头找。
        out_of_scope = [
            s.student_no for s in students if not student_id_in_scope(s.student_no)
        ]
        if out_of_scope:
            log.error(
                "生成的模拟学号有 %d 个不在学号作用域内（例：%s），"
                "它们会被学号闸门静默挡掉，页面上看不到。\n"
                "  作用域规则：%s\n"
                "  这通常是 mock 生成器的 bug（学号位数/专业码不对），"
                "或 HB_STUDENT_ID_PATTERN 被改动过。请先修好再 seed。",
                len(out_of_scope),
                "、".join(out_of_scope[:3]),
                describe_student_scope(),
            )
            return 1

        n = upsert_students(
            db,
            [
                {
                    "student_no": s.student_no,
                    "name": s.name,
                    "grade": s.grade,
                    "major_code": s.major_code,
                    "major_name": s.major_name,
                    "class_name": s.class_name,
                    "org_name": s.org_name,
                    "enroll_year": int(s.grade) if s.grade.isdigit() else None,
                }
                for s in students
            ],
        )
        # 专业代码 → 名称映射：真实名册只给代码时也能显示中文专业名
        for code, name in MAJORS:
            if db.get(MajorAlias, code) is None:
                db.add(MajorAlias(code=code, name=name))
        db.commit()
        log.info("已导入 %d 名学生，专业映射 %d 条", n, len(MAJORS))

        result = asyncio.run(
            Collector(mock_student_count=args.students).run_once(
                db, job="seed", history_days=args.days
            )
        )
        log.info("首轮模拟采集：%s", result["message"])
        collect_ok = bool(result.get("ok"))

        # 让历史课时也带上专业名（mock 课表用班号区分，这里按学号补齐）
        from sqlalchemy import select as _select

        for stu in db.execute(_select(Student)).scalars():
            if not stu.major_name and stu.major_code:
                alias = db.get(MajorAlias, stu.major_code)
                if alias:
                    stu.major_name = alias.name
        db.commit()

        # 落库后复核：学生真的进去了吗？
        if not collect_ok:
            log.error("首轮采集失败，模拟数据不完整：%s", result["message"])
            return 1
    finally:
        db.close()
    print(f"✅ mock 数据已生成（{n} 名学生）。启动服务：python -m app.cli serve")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    init_db()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from app.services.collect import Collector

    init_db()
    db = SessionLocal()
    try:
        # force=True：人工执行 pp.cli collect 时不看 COLLECT_ENABLED，
        # 那个开关只该拦住**自动**轮询。
        result = asyncio.run(Collector().run_once(db, job="cli", force=True))
        print(f"{'✅' if result['ok'] else '❌'} {result['message']}")
        return 0 if result["ok"] else 1
    finally:
        db.close()


def cmd_rebuild(args: argparse.Namespace) -> int:
    from app.services.collect import Collector
    from app.services.ingest import recompute_student_identifiers

    init_db()
    db = SessionLocal()
    try:
        # 先按当前的学号切片规则重新解析年级/专业 —— 改了
        # STUDENT_ID_MAJOR_SLICE 之后只重算天记录是不够的，
        # 学生行上的 grade/major_code 会一直是旧值。
        touched = recompute_student_identifiers(db)
        if touched:
            print(f"🔁 按当前切片规则刷新了 {touched} 名学生的年级/专业")
        n = Collector.rebuild_all(db, days=args.days)
        print(f"✅ 已重算 {n} 条「学生×日期」记录")
        return 0
    finally:
        db.close()


def cmd_sign(args: argparse.Namespace) -> int:
    from app.adapters.hanboer import build_signed_url

    if not settings.hb_base_url:
        print("❌ 未配置 HB_BASE_URL", file=sys.stderr)
        return 1
    params: dict[str, str] = {}
    for item in args.param or []:
        if "=" not in item:
            print(f"❌ 参数格式应为 k=v：{item}", file=sys.stderr)
            return 1
        k, _, v = item.partition("=")
        params[k] = v
    url = build_signed_url(settings.hb_base_url, args.path, params)
    print(url)
    print("\n（该 URL 的 sign 有效期 15 分钟，可直接在浏览器打开核对返回）", file=sys.stderr)
    return 0


def cmd_rekey_students(args: argparse.Namespace) -> int:
    """把学号里残留的平台前缀（cuc）去掉。

    学号是主键，改一处要连带 4 张子表；而示示例辅导员的手动标记、名册信息都挂在
    students 行上，不能靠"清库重来"解决 —— 那些是接口重建不出来的人工数据。
    """
    from app.services.rekey import find_stale_student_nos, rekey_students

    init_db()
    db = SessionLocal()
    try:
        mapping = find_stale_student_nos(db)
        if not mapping:
            print("✅ 库内学号已经是规范形式，无需处理")
            return 0

        print(f"发现 {len(mapping)} 个带前缀的学号，例如：")
        for old, new in list(mapping.items())[:5]:
            print(f"   {old}  →  {new}")
        if len(mapping) > 5:
            print(f"   … 其余 {len(mapping) - 5} 个")

        result = rekey_students(db, dry_run=args.dry_run)
        print()
        for k, v in result.items():
            print(f"   {k:<18} {v}")
        if args.dry_run:
            print("\n（--dry-run：未写库。去掉该参数即真正执行）")
        else:
            print("\n建议接着跑一次 rebuild，让判定与新的学号对齐：")
            print("   python -m app.cli rebuild")
        return 0
    finally:
        db.close()


def cmd_marks(args: argparse.Namespace) -> int:
    """查手动标记的流水（谁、何时、为什么）。"""
    from sqlalchemy import select

    from app.models import Student
    from app.services.marks import mark_history

    init_db()
    db = SessionLocal()
    try:
        stmt = select(Student).where(Student.manual_state != "")
        if args.student_no:
            stmt = select(Student).where(Student.student_no == args.student_no)
        rows = list(db.execute(stmt).scalars())
        if not rows:
            print("没有处于标记状态的学生")
            return 0

        for stu in rows:
            print(f"\\n{stu.name}（{stu.student_no}）  当前标记："
                  f"{stu.manual_marked_at:%Y-%m-%d %H:%M} {stu.manual_by}：{stu.manual_reason}")
            hist = mark_history(db, stu.student_no, limit=args.limit)
            if not hist:
                print("   （无流水：这条标记是在加流水功能之前打的，"
                      "可加 --backfill 补一条）")
            for h in hist:
                tag = "标记" if h["action"] == "mark" else "撤销"
                print(f"   {h['at']}  {tag}  {h['by']}：{h['reason']}")

        if args.backfill:
            from app.models import StudentMark

            added = 0
            for stu in rows:
                if mark_history(db, stu.student_no, limit=1):
                    continue
                db.add(StudentMark(
                    student_no=stu.student_no,
                    action="mark",
                    state=stu.manual_state,
                    reason=stu.manual_reason or "",
                    by=stu.manual_by or "",
                    created_at=stu.manual_marked_at,
                ))
                added += 1
            db.commit()
            print(f"\\n已为 {added} 条历史标记补上流水")
        return 0
    finally:
        db.close()


def cmd_state(args: argparse.Namespace) -> int:
    from app.services.ingest import dashboard_rows

    init_db()
    db = SessionLocal()
    try:
        rows = dashboard_rows(db)
        if not rows:
            print("库内没有学生数据，请先执行：python -m app.cli seed")
            return 1
        counter = Counter(r["state"] for r in rows)
        print(f"共 {len(rows)} 名学生，生成时间 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        for state, n in counter.most_common():
            bar = "█" * max(1, int(n / max(1, len(rows)) * 40))
            print(f"  {state:<8} {n:>5}  {bar}")
        print()
        abnormal = [r for r in rows if r["level"] in ("warn", "danger")]
        abnormal.sort(key=lambda r: (-r["consecutive_missed_days"], r["student_no"]))
        print(f"异常学生（展示前 {args.limit} 条）：")
        for r in abnormal[: args.limit]:
            print(
                f"  [{r['state']}] {r['student_no']} {r['name']:<8} {r['group']:<24} "
                f"连续{r['consecutive_missed_days']}天  上次签到={r['last_sign_in_time'] or '—'}"
            )
        return 0
    finally:
        db.close()


def cmd_timetable(args: argparse.Namespace) -> int:
    """从考勤数据**反推**某个学生/某个班的课表。

    为什么需要
    ----------
    老师反馈"课表不对"。要判断到底是**我们抓漏了**还是**上游本来就没有**，
    最直接的办法是把考勤数据里每个学生出现过的课还原成一张周课表，
    然后拿它跟真实课表逐格对照。

    这个命令**只能反推出翰博尔里已经有的课** —— 上游没有的课它变不出来。
    它的价值是让"缺了什么"变成看得见的具体格子，而不是一句感觉。
    """
    from collections import defaultdict

    from sqlalchemy import select

    from app.models import AttendanceEvent, Course, Student

    WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    SLOT_ORDER = ["08:00", "10:10", "14:00", "15:50", "18:30", "20:20"]

    init_db()
    db = SessionLocal()
    try:
        # ── 选人 ──
        stmt = select(Student)
        if args.student_no:
            stmt = stmt.where(Student.student_no == args.student_no)
        elif args.name:
            stmt = stmt.where(Student.name == args.name)
        elif args.grade:
            stmt = stmt.where(Student.grade == args.grade)
            if args.major:
                stmt = stmt.where(Student.major_code == args.major)
        else:
            print("请指定 --student-no / --name / --grade（可加 --major）", file=sys.stderr)
            return 1

        students = list(db.execute(stmt).scalars())
        if not students:
            print("没找到符合条件的学生", file=sys.stderr)
            return 1
        if len(students) > 1 and not args.grade:
            print(f"匹配到 {len(students)} 人，请用 --student-no 精确指定：", file=sys.stderr)
            for s in students[:20]:
                print(f"   {s.student_no}  {s.name}", file=sys.stderr)
            return 1

        nos = [s.student_no for s in students]
        label = (
            f"{students[0].name}（{students[0].student_no}）"
            if len(nos) == 1
            else f"{args.grade}级{('专业' + args.major) if args.major else ''} 共 {len(nos)} 人"
        )

        # ── 汇总这些人在 课×星期×时间 上的出现次数 ──
        grid: dict[tuple[int, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        course_people: dict[str, set[str]] = defaultdict(set)
        course_meta: dict[str, Course] = {}

        rows = db.execute(
            select(AttendanceEvent.student_no, Course)
            .join(Course, Course.schedule_guid == AttendanceEvent.schedule_guid)
            .where(AttendanceEvent.student_no.in_(nos))
        ).all()

        for student_no, course in rows:
            if course.class_date is None or course.start_time is None:
                continue
            wd = course.class_date.weekday()
            slot = course.start_time.strftime("%H:%M")
            grid[(wd, slot)][course.course_name] += 1
            course_people[course.course_name].add(student_no)
            course_meta[course.course_name] = course

        if not grid:
            print(f"{label}：考勤数据里没有任何课程记录")
            return 1

        # ── 打印周课表 ──
        print(f"\n{label} 的课表（**从考勤数据反推**，不含翰博尔没有的课）")
        print("=" * 78)
        slots = [s for s in SLOT_ORDER if any((w, s) in grid for w in range(7))]
        slots += sorted({s for (_, s) in grid if s not in slots})

        head = "节次    " + "".join(f"{d:<12}" for d in WEEKDAYS[: args.days_shown])
        print(head)
        print("-" * len(head))
        for slot in slots:
            cells = []
            for w in range(args.days_shown):
                names = grid.get((w, slot))
                if not names:
                    cells.append("·".ljust(12))
                else:
                    # 一格塞多门课时只显示第一门，完整清单在下面
                    first = sorted(names)[0]
                    short = first if len(first) <= 9 else first[:8] + "…"
                    extra = f"+{len(names) - 1}" if len(names) > 1 else ""
                    cells.append(f"{short}{extra}".ljust(12))
            print(f"{slot:<8}" + "".join(cells))

        # ── 该生/该班出现过的全部课程 ──
        print()
        print(f"共反推出 {len(course_people)} 门课：")
        for name in sorted(course_people, key=lambda n: -len(course_people[n])):
            c = course_meta[name]
            n = len(course_people[name])
            share = f"{n}/{len(nos)} 人" if len(nos) > 1 else ""
            print(
                f"   {name:<24} {c.building_name}{c.classroom_name:<8} "
                f"节次={c.node_name or '—':<8} {share}"
            )

        # ── 与"正常课表应该有多少"做个体检 ──
        distinct = len(course_people)
        print()
        print("─" * 78)
        warn = []
        if distinct < 5:
            warn.append(f"只反推出 {distinct} 门课（正常本科生一学期 8~12 门）")
        if not any(
            k in name
            for name in course_people
            for k in ("思政", "马克思", "近代史", "毛泽东", "形势", "体育", "军事", "心理健康")
        ):
            warn.append("没有任何思政/体育/军事理论等公共必修课")
        if not any("英语" in name for name in course_people):
            warn.append("没有大学英语类课程")
        if warn:
            print("⚠ 这份课表看起来**不完整**：")
            for w in warn:
                print(f"     · {w}")
            print(
                "   注意：本命令只能反推翰博尔里**已有**的课；上游没有的课它变不出来。\n"
                "   实测本校一个班只能反推出 2~6 门，全学院人均 2.1 门 —— 缺的是上游数据，\n"
                "   不是抓取逻辑。详见 docs/待办任务.md 任务 1。"
            )
        else:
            print("✅ 课表结构看起来基本完整")
        return 0
    finally:
        db.close()


def cmd_scope(args: argparse.Namespace) -> int:
    """查看/清理「校区作用域」。

    翰博尔是多校区共用平台，接口没有校区过滤参数，全平台的课一次性返回。
    用 HB_CAMPUS_KEYWORDS（默认「海南」）按节次名等字段切开，
    本命令用来核对切得对不对、并清理已经混进来的别的校区的数据。
    """
    from collections import Counter

    from sqlalchemy import select

    from app.models import Course
    from app.services.scope import (
        course_haystack,
        describe_scope,
        purge_out_of_scope,
        scope_keywords,
        text_in_scope,
    )

    init_db()
    db = SessionLocal()
    try:
        kws = scope_keywords()
        print(f"作用域关键词：{describe_scope()}")
        if not kws:
            print("⚠ 未配置 HB_CAMPUS_KEYWORDS —— 全平台所有校区的数据都会被采集。")
            return 1

        inside: list[Course] = []
        outside: list[Course] = []
        for row in db.execute(select(Course)).scalars():
            (inside if text_in_scope(course_haystack(row), kws) else outside).append(row)

        print(f"库内课程 {len(inside) + len(outside)} 门："
              f"作用域内 {len(inside)}，作用域外 {len(outside)}\n")

        for label, rows in (("作用域内", inside), ("作用域外（将被清理）", outside)):
            if not rows:
                continue
            print(f"── {label} · 节次分布 ──")
            for name, n in Counter(r.node_name or "(空)" for r in rows).most_common(12):
                print(f"   {n:>5}  {name}")
            print(f"── {label} · 楼栋分布 ──")
            for name, n in Counter(r.building_name or "(空)" for r in rows).most_common(12):
                print(f"   {n:>5}  {name}")
            print()

        if not args.purge:
            if outside:
                print(f"要把这 {len(outside)} 门及其考勤数据删掉，加 --purge 重跑。")
            return 0

        print(f"开始清理 {len(outside)} 门作用域外课程…")
        result = purge_out_of_scope(db, drop_students=args.drop_students)
        for k, v in result.items():
            print(f"   {k:<16} {v}")
        print("\n清理完毕。接着跑一次 rebuild 让判定结果与新数据对齐：")
        print("   python -m app.cli rebuild")
        return 0
    finally:
        db.close()


def cmd_backfill(args: argparse.Namespace) -> int:
    """回补历史考勤数据。

    首次接入真实数据时必须跑：接口默认只给当天的课，
    没有历史就分不清"连续三天"和"只缺一天"（全员都会被算成异常）。
    """
    from app.services.backfill import run_backfill

    init_db()
    db = SessionLocal()
    try:
        print(f"开始回补最近 {args.days} 天的课程与考勤明细…")
        print("（每天 1 次汇总请求 + 每门课 1 次明细请求，请耐心等待）\n")
        result = run_backfill(db, days=args.days)
        print()
        for k, v in result.items():
            print(f"  {k:<14} {v}")
        return 0
    finally:
        db.close()


def cmd_prune(args: argparse.Namespace) -> int:
    from app.services.maintenance import checkpoint, db_stats, prune

    init_db()
    db = SessionLocal()
    try:
        print("清理前：", db_stats(db))
        result = prune(db, retention_days=args.days)
        checkpoint(db)
        print("已删除：", {k: v for k, v in result.items() if k != "cutoff"}, f"（早于 {result['cutoff']}）")
        print("清理后：", db_stats(db))
        return 0
    finally:
        db.close()


def cmd_cuc_probe(args: argparse.Namespace) -> int:
    """探测中传接口的响应结构（**只输出键名与类型，不输出任何数据值**）。

    联调第一步：我们还不知道对方的 JSON 字段叫什么。这个命令把每个数据源
    的响应信封结构打印出来，可以直接贴给对方或贴到对话里 ——
    因为值一律被替换成类型标记，不含任何学生信息。
    """
    from app.adapters.cuc import CucClient, CucError

    if not settings.cuc_enabled:
        print("❌ CUC_ENABLED=false，先在 .env 里打开", file=sys.stderr)
        return 1
    if not settings.cuc_base_url:
        print("❌ CUC_BASE_URL 未配置", file=sys.stderr)
        return 1

    paths = {
        "① 公共_校历": settings.cuc_path_calendar,
        "② 本科生院_本专科生基本信息": settings.cuc_path_students,
        "③ 本研_学生选课": settings.cuc_path_enrollments,
        "④ 本研_课表": settings.cuc_path_timetable,
        "⑤ 畅课_签到": settings.cuc_path_checkins,
    }

    client = CucClient()
    print(f"接口根地址：{settings.cuc_base_url}")
    print(f"学年学期  ：{settings.cuc_term or '（未配置）'}")
    print()

    failed = 0
    for label, path in paths.items():
        print(f"── {label} ─────────────────────────────")
        if not path:
            print("  （未配置路径，跳过）\n")
            continue
        try:
            info = client.probe(path, {"xnxq": settings.cuc_term} if settings.cuc_term else None)
        except CucError as exc:
            print(f"  ✗ {exc}\n")
            failed += 1
            continue
        print(f"  HTTP {info['status']}  {info['content_type']}  {info['bytes']} 字节")
        if not info.get("json"):
            print(f"  响应不是 JSON，开头：{info.get('body_head', '')}")
            failed += 1
            print()
            continue
        print(f"  数据行数：{info.get('row_count', '（未识别）')}")
        print("  信封结构：")
        print(_indent_json(info.get("envelope")))
        if info.get("row_keys"):
            print("  单行字段（键名 → 类型）：")
            print(_indent_json(info["row_keys"]))
        if info.get("rows_error"):
            print(f"  ⚠ 取数据行失败：{info['rows_error']}")
        print()

    if failed:
        print(f"有 {failed} 个数据源探测失败，先修这些再往下走", file=sys.stderr)
        return 1
    print("全部探测成功。请确认上面每个「单行字段」里能找到我们需要的键名；")
    print("若键名与预期不同，在 .env 里用 CUC_FIELDS_* 显式指定，不用改代码。")
    return 0


def _indent_json(obj) -> str:
    import json as _json

    text = _json.dumps(obj, ensure_ascii=False, indent=2)
    return "\n".join("    " + line for line in text.splitlines())


def cmd_cuc_demo(args: argparse.Namespace) -> int:
    """demo 模式：只拉**一个学号**的数据，用于核对字段与连接关系。

    为什么限定单个学号：真实接口一旦跑错范围就是全校数据拉取，
    所以要求显式指定自己的学号（或配 CUC_DEMO_STUDENT_NO），
    留空直接报错退出。
    """
    from app.adapters.cuc import CucClient, CucError

    sid = (args.student_no or settings.cuc_demo_student_no or "").strip()
    if not sid:
        print(
            "❌ 需要指定学号：\n"
            "     python -m app.cli cuc-demo --student-no 202400000001N\n"
            "   或在 .env 里配 CUC_DEMO_STUDENT_NO\n"
            "  （demo 只拉一个人的数据，防止误触发全校拉取）",
            file=sys.stderr,
        )
        return 1

    if not settings.cuc_enabled:
        print("❌ CUC_ENABLED=false，先在 .env 里打开", file=sys.stderr)
        return 1

    client = CucClient()
    print(f"demo 学号：{sid}")
    print(f"学年学期：{settings.cuc_term or '（未配置）'}")
    print()

    # ── 1. 校历 ──
    print("── ① 公共_校历 ──")
    week1 = None
    try:
        cal = client.fetch_calendar()
        week1 = cal.week1_monday
        print(f"  教学开始日期（第一周周一）：{week1}")
        print(f"  学年学期：{cal.term or settings.cuc_term}")
        monday_ok = week1.weekday() == 0
        print(f"  是否周一：{'✓' if monday_ok else '✗ 注意，已规范到本周周一'}")
    except CucError as exc:
        print(f"  ✗ {exc}")
    print()

    # ── 2. 学生信息 ──
    print("── ② 本科生院_本专科生基本信息 ──")
    try:
        students = client.fetch_students()
        me = [s for s in students if s.student_no == sid]
        print(f"  接口返回 {len(students)} 人")
        if me:
            s = me[0]
            print(f"  本人：学号={s.student_no}  姓名={s.name}  "
                  f"专业={s.major}  年级={s.grade}  班号={s.class_name}")
        else:
            print(f"  ⚠ 没在返回结果里找到 {sid}")
    except CucError as exc:
        print(f"  ✗ {exc}")
    print()

    # ── 3. 选课 ──
    print("── ③ 本研_学生选课 ──")
    my_enrollments = []
    try:
        enrollments = client.fetch_enrollments()
        my_enrollments = [e for e in enrollments if e.student_no == sid]
        print(f"  接口返回 {len(enrollments)} 条，其中本人 {len(my_enrollments)} 条")
        for e in my_enrollments[:20]:
            print(f"    {e.term} | {e.category} | 教学班={e.teaching_class} | 课程={e.course_no}")
        if len(my_enrollments) > 20:
            print(f"    …… 另有 {len(my_enrollments) - 20} 条")
    except CucError as exc:
        print(f"  ✗ {exc}")
    print()

    # ── 4. 课表 ──
    print("── ④ 本研_课表 ──")
    timetable = []
    try:
        timetable = client.fetch_timetable()
        print(f"  接口返回 {len(timetable)} 行")
        with_record = sum(1 for t in timetable if t.record_id)
        print(f"  含记录id的行：{with_record} / {len(timetable)}"
              f"  {'✓' if with_record == len(timetable) else '⚠ 有行缺记录id'}")
        no_weeks = [t for t in timetable if not t.weeks]
        if no_weeks:
            print(f"  ⚠ {len(no_weeks)} 行的周次解析为空，原始值示例："
                  f"{[t.weeks_raw for t in no_weeks[:3]]}")
    except CucError as exc:
        print(f"  ✗ {exc}")
    print()

    # ── 5. 连接验证（这是 demo 的核心）──
    print("── ⑤ 连接验证：本人选课 × 课表 ──")
    if not my_enrollments or not timetable:
        print("  数据不足，跳过")
    else:
        # 按 (课程号, 教学班) 建立课表索引
        index: dict[tuple[str, str], list] = {}
        for t in timetable:
            index.setdefault((t.course_no, t.teaching_class), []).append(t)

        matched, unmatched = [], []
        for e in my_enrollments:
            hits = index.get((e.course_no, e.teaching_class), [])
            if hits:
                matched.append((e, hits))
            else:
                unmatched.append(e)

        print(f"  匹配上：{len(matched)} 门；匹配不上：{len(unmatched)} 门")
        for e, hits in matched[:10]:
            for t in hits:
                print(f"    ✓ {t.course_name or e.course_no} "
                      f"[{e.course_no}/{e.teaching_class}] "
                      f"{t.weeks_raw} 星期{t.weekday} {t.period_span[0]}-{t.period_span[1]}节 "
                      f"{t.classroom} 记录id={t.record_id or '（无）'}")
        if unmatched:
            print("  匹配不上的（说明课表里没有这个教学班的时间，需排查）：")
            for e in unmatched[:10]:
                print(f"    ✗ 课程={e.course_no} 教学班={e.teaching_class} 类别={e.category}")

        # 展开成具体日期，验证校历用得对
        if week1:
            from app.services.teach_schedule import period_time

            print()
            print(f"  ── 按校历（第一周 {week1}）展开成具体课次 ──")
            shown = 0
            for e, hits in matched:
                for t in hits:
                    for w in t.weeks:
                        if shown >= 12:
                            break
                        from datetime import timedelta as _td

                        d = week1 + _td(days=(w - 1) * 7 + (t.weekday - 1))
                        times = period_time(t.period_span[0])
                        tt = f"{times[0]:%H:%M}-{period_time(t.period_span[1])[1]:%H:%M}" if times and period_time(t.period_span[1]) else "?"
                        print(f"    第{w:>2}周 {d} 星期{t.weekday} {tt} "
                              f"{t.course_name} {t.classroom}")
                        shown += 1
            if shown == 0:
                print("    （没有可展开的课次）")
    print()

    # ── 6. 畅课签到 ──
    print("── ⑥ 畅课_签到 ──")
    try:
        checkins = client.fetch_checkins()
        mine = [c for c in checkins if c.student_no == sid]
        print(f"  接口返回 {len(checkins)} 条，其中本人 {len(mine)} 条")
        for c in mine[:10]:
            print(f"    记录id={c.record_id} 状态={c.status} "
                  f"签到={c.checkin_time} 上课={c.class_time} 教学班={c.teaching_class}")
        if mine:
            with_rid = sum(1 for c in mine if c.record_id)
            print(f"  本人签到里含记录id的：{with_rid} / {len(mine)}")
            if with_rid == len(mine) and timetable:
                rids = {t.record_id for t in timetable if t.record_id}
                hit = sum(1 for c in mine if c.record_id in rids)
                print(f"  记录id 能在课表里找到对应课次的：{hit} / {len(mine)}"
                      f"  {'✓ 说明记录id确实能连起来' if hit == len(mine) else '⚠ 有对不上的，需看原始数据'}")
    except CucError as exc:
        print(f"  ✗ {exc}")

    print()
    print("demo 结束。以上只涉及你本人的数据。")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from app.services.maintenance import db_stats

    init_db()
    db = SessionLocal()
    try:
        for k, v in db_stats(db).items():
            print(f"  {k:<10} {v:>10,}")
        return 0
    finally:
        db.close()


def cmd_deactivate(args: argparse.Namespace) -> int:
    """停用学生（毕业 / 退学）。

    不停用会怎样：毕业生的历史事件仍在库里，判定会一直认为他「有课且未签到」，
    于是**永久告警**。所以每学年结束必须按年级停用。
    """
    from sqlalchemy import func, select, update

    from app.models import Student

    if not args.grade and not args.student_no:
        print("❌ 请至少指定 --grade 或 --student-no", file=sys.stderr)
        return 1

    init_db()
    db = SessionLocal()
    try:
        targets = []
        if args.student_no:
            targets.extend(args.student_no)
        if args.grade:
            targets.extend(
                r[0]
                for r in db.execute(
                    select(Student.student_no).where(
                        Student.grade.in_(args.grade), Student.active.is_(True)
                    )
                ).all()
            )
        targets = sorted(set(targets))
        if not targets:
            print("没有匹配的在读学生。")
            return 1

        if args.dry_run:
            print(f"将停用 {len(targets)} 名学生：{'、'.join(targets[:10])}"
                  + ("…" if len(targets) > 10 else ""))
            return 0

        db.execute(
            update(Student)
            .where(Student.student_no.in_(targets))
            .values(active=False, updated_at=datetime.now())
        )
        db.commit()

        remaining = db.execute(
            select(func.count()).select_from(Student).where(Student.active.is_(True))
        ).scalar_one()
        print(f"✅ 已停用 {len(targets)} 名学生（年级：{','.join(args.grade) or '—'}）。"
              f"当前在读 {remaining} 人。")
        print("   注意：历史事件仍保留（`app.cli prune` 会按保留期清理）；"
              "停用后不再出现在页面与告警里。")
        return 0
    finally:
        db.close()


def cmd_delete_students(args: argparse.Namespace) -> int:
    """彻底删除学生及其关联数据（仅用于清理误导入的测试数据）。"""
    from sqlalchemy import delete, select

    from app.models import (
        Alert,
        AttendanceDaily,
        AttendanceEvent,
        Student,
        StudentCourse,
    )

    if not args.student_no:
        print("❌ 请用 --student-no 指定要删除的学号（本命令不做批量猜测）", file=sys.stderr)
        return 1

    init_db()
    db = SessionLocal()
    try:
        nos = [n for n in args.student_no if db.get(Student, n) is not None]
        missing = [n for n in args.student_no if n not in nos]
        if not nos:
            print("库内没有这些学号：", "、".join(missing))
            return 1

        if args.dry_run:
            print(f"将删除 {len(nos)} 名学生及其全部关联数据：{'、'.join(nos)}")
            return 0

        # 先收集受影响的课程，稍后清理变成孤儿且已过期的课程
        guids = {
            r[0]
            for r in db.execute(
                select(AttendanceEvent.schedule_guid).where(
                    AttendanceEvent.student_no.in_(nos)
                )
            ).all()
        }
        counts = {}
        for model, column in (
            (AttendanceEvent, AttendanceEvent.student_no),
            (AttendanceDaily, AttendanceDaily.student_no),
            (StudentCourse, StudentCourse.student_no),
            (Alert, Alert.student_no),
        ):
            counts[model.__tablename__] = (
                db.execute(delete(model).where(column.in_(nos))).rowcount or 0
            )
        counts["students"] = (
            db.execute(delete(Student).where(Student.student_no.in_(nos))).rowcount or 0
        )
        db.commit()

        print(f"✅ 已删除 {'、'.join(nos)}")
        for table, n in counts.items():
            print(f"   {table:<18} -{n}")
        if missing:
            print("   （库内不存在，已跳过）：" + "、".join(missing))
        if guids:
            print(f"   涉及的 {len(guids)} 门课程未删除（可能仍有其他学生引用）；"
                  "如需清理请执行 app.cli prune")
        return 0
    finally:
        db.close()


def cmd_students(args: argparse.Namespace) -> int:
    """按学号作用域核对/清理学生：只保留本院（海南国际学院）的学生。

    为什么要有这条命令
    ------------------
    ``HB_CAMPUS_KEYWORDS`` 切的是**课程**（靠节次名里的「海南」），
    而 ``HB_STUDENT_ID_PATTERN`` 切的是**学生**（靠学号形态）。
    平台改个节次名，第一道闸门就静默失效；学号形态是学校定的，更硬。

    本命令既出报告（默认），也能真删（``--purge``）。
    """
    from collections import Counter

    from sqlalchemy import func, select

    from app.models import (
        Alert,
        AttendanceDaily,
        AttendanceEvent,
        Course,
        Student,
        StudentCourse,
    )
    from app.services.scope import (
        describe_student_scope,
        out_of_scope_students,
        purge_out_of_scope_students,
        student_id_in_scope,
    )

    init_db()
    db = SessionLocal()
    try:
        rows = list(db.execute(select(Student)).scalars())
        drop, total = out_of_scope_students(db, rows)
        print(f"学号规则：{describe_student_scope()}")
        print(f"库内学生 {total} 人：符合 {total - len(drop)}，不符合 {len(drop)}\n")

        if not rows:
            print("（库内没有学生，先跑 python -m app.cli import students <文件>）")
            return 0

        if drop:
            on_drop = set(drop)
            events = (
                db.execute(
                    select(func.count()).select_from(AttendanceEvent).where(
                        AttendanceEvent.student_no.in_(drop)
                    )
                ).scalar_one()
            )
            daily = (
                db.execute(
                    select(func.count()).select_from(AttendanceDaily).where(
                        AttendanceDaily.student_no.in_(drop)
                    )
                ).scalar_one()
            )
            alerts = (
                db.execute(
                    select(func.count()).select_from(Alert).where(Alert.student_no.in_(drop))
                ).scalar_one()
            )
            links = (
                db.execute(
                    select(func.count()).select_from(StudentCourse).where(
                        StudentCourse.student_no.in_(drop)
                    )
                ).scalar_one()
            )
            print("不符合规则的学生将连同其考勤数据一起删除：")
            print(f"   学生 {len(drop)} 人 / 签到事件 {events} 条 / 判定缓存 {daily} 条 "
                  f"/ 告警 {alerts} 条 / 选课关系 {links} 条")
            print("   按年级分布：")
            for grade, n in Counter(r.grade or "(空)" for r in rows if r.student_no in on_drop).most_common(8):
                print(f"      {grade}级  {n} 人")
            print("\n   样例（最多 10 个）：")
            for no in drop[:10]:
                print(f"      {no}")
            if len(drop) > 10:
                print(f"      … 其余 {len(drop) - 10} 个")
        if not drop:
            print("✅ 库内所有学生都符合本院学号规则，无需清理")
            return 0

        # 默认只出报告。删除是不可逆的，必须显式 --purge。
        print("\n要真正删除，加 --purge 重跑：")
        print("   python -m app.cli students --purge")
        if not args.purge:
            return 0

        # 大比例删除要二次确认。线上库第一次跑这条命令时要删掉 83% 的学生，
        # 而"学号正则写错一位"与"上游真的混进来一堆外院的人"在报告上
        # 长得一模一样 —— 靠人看一眼数字不足以区分，所以强制多敲一个 --yes。
        if len(drop) >= BIG_PURGE_MIN and total and len(drop) / total >= BIG_PURGE_RATIO:
            percent = len(drop) * 100 / total
            if not args.yes:
                print(
                    f"\n⚠ 这次会删掉 {len(drop)}/{total} 人（{percent:.0f}%）—— "
                    "比例异常高，先确认学号规则没错："
                )
                print(f"   HB_STUDENT_ID_PATTERN={settings.hb_student_id_pattern}")
                print("   确认无误后加 --yes：")
                print("   python -m app.cli students --purge --yes")
                return 1
            print(f"\n（已确认：删除 {len(drop)}/{total} 人，{percent:.0f}%）")

        result = purge_out_of_scope_students(db)
        print("\n✅ 清理完成：")
        for key in (
            "students_deleted",
            "events_deleted",
            "daily_deleted",
            "alerts_deleted",
            "marks_deleted",
            "links_deleted",
            "courses_deleted",
        ):
            print(f"   {key:<20} -{result[key]}")

        remaining = db.execute(select(func.count()).select_from(Student)).scalar_one()
        courses = db.execute(select(func.count()).select_from(Course)).scalar_one()
        stray = [
            s.student_no
            for s in db.execute(select(Student)).scalars()
            if not student_id_in_scope(s.student_no)
        ]
        print(f"\n   剩余学生 {remaining} 人 / 课程 {courses} 门")
        if stray:
            print(f"   ⚠ 仍有 {len(stray)} 人不在规则内（预期为 0）：{stray[:5]}")
        print("\n建议接着重算判定（历史快照里可能还留着被删学生的行）：")
        print("   python -m app.cli rebuild --days 60")
        return 0
    finally:
        db.close()


def cmd_teach_schedule(args: argparse.Namespace) -> int:
    """把教务课程表的「上课时间地点」补进课程行。

    为什么单独一条命令：选课名单只说明"谁选了课"，没有"哪天上课"，
    那些课就进不了周课表。教务导出的课程表里有「上课时间地点」，
    本命令把它解析成星期/节次/周次，并折算成具体课次日期。
    """
    from datetime import date as _date
    from pathlib import Path

    from app.services.teach_schedule import import_teaching_schedule

    path = Path(args.file)
    if not path.exists():
        print(f"❌ 文件不存在：{path}", file=sys.stderr)
        return 1

    week1 = settings.semester_week1_date
    if args.week1:
        try:
            week1 = _date.fromisoformat(args.week1)
        except ValueError:
            print(f"❌ --week1 需要 ISO 日期，收到 {args.week1!r}", file=sys.stderr)
            return 1
    if week1 is None:
        print("❌ 未配置 SEMESTER_WEEK1_MONDAY（或 --week1），无法折算周次", file=sys.stderr)
        return 1
    if week1.weekday() != 0:
        # 不是周一就整体偏移 7 天 —— 宁可直接拒绝，也不要静默把课次算错。
        print(
            f"⚠ SEMESTER_WEEK1_MONDAY={week1} 不是周一（是星期"
            f"{'一二三四五六日'[week1.weekday()]}），周次折算会整体偏移，请核对",
            file=sys.stderr,
        )
        return 1

    init_db()
    db = SessionLocal()
    try:
        print(f"第 1 周周一：{week1}（SEMESTER_WEEK1_MONDAY）\n")
        stat = import_teaching_schedule(db, path, week1, dry_run=args.dry_run)
        print(f"课程表行数：{stat['rows']}")
        print(f"解析出排课段：{stat['meetings']}　未解析行：{stat['unparsed']}")
        print(f"课程：匹配 {stat['matched']}　新建 {stat['created']}　共 {stat['courses']} 门")
        print(f"其中拿到具体课次时间的：{stat['with_time']} 门")
        if args.dry_run:
            print("\n（--dry-run：未写库）")
        else:
            print("\n建议接着跑 rebuild，让周课表与判定用上新的课次时间：")
            print("   python -m app.cli rebuild --days 60")
        return 0
    except Exception as exc:
        print(f"❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def cmd_fix_names(args: argparse.Namespace) -> int:
    """按教务处名单**只修正姓名**（不动选课关系、不动班级）。

    为什么需要它：采集侧原先会用翰博尔返回的 ``userName`` 覆盖姓名，
    而那是**学生自己设的账号名**（实测有「爸爸」「cxy」「Jackie」）。
    写入规则已修（名册来源的姓名不再被覆盖），这个命令负责把库里
    已经被写坏的名字按名单刷回来。
    """
    from pathlib import Path

    from app.services.roster import repair_names

    path = Path(args.file)
    if not path.exists():
        print(f"❌ 文件不存在：{path}", file=sys.stderr)
        return 1

    init_db()
    db = SessionLocal()
    try:
        result = repair_names(db, path, dry_run=args.dry_run)
        print(f"名单里能对上的学生：{result['students']} 人")
        print(f"姓名不一致：{result['fixed']} 人")
        for no, old, new in result["renamed"][:25]:
            print(f"   {no}  「{old}」 → 「{new}」")
        if len(result["renamed"]) > 25:
            print(f"   … 其余 {len(result['renamed']) - 25} 人")
        if args.dry_run:
            print("\n（--dry-run：未写库）")
        else:
            print("\n✅ 已写回，并标记为「名册来源」—— 采集不会再覆盖它们")
        return 0
    except Exception as exc:
        print(f"❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def cmd_import(args: argparse.Namespace) -> int:
    """从信息化处给的 Excel/CSV 导入学生名册或课表。"""
    from pathlib import Path

    from app.services.roster import (
        dump_report,
        format_check,
        import_enrollments,
        import_schedule,
        import_students,
        roster_check,
    )

    path = Path(args.file)
    if not path.exists():
        print(f"❌ 文件不存在：{path}", file=sys.stderr)
        return 1

    init_db()
    db = SessionLocal()
    try:
        if args.kind == "students":
            report = import_students(
                db, path, dry_run=args.dry_run, show_raw=args.show_samples
            )
        elif args.kind == "enrollments":
            report = import_enrollments(
                db, path, dry_run=args.dry_run, show_raw=args.show_samples
            )
        else:
            report = import_schedule(
                db, path, dry_run=args.dry_run, show_raw=args.show_samples
            )

        print(dump_report(report))
        if args.dry_run:
            print("\n（--dry-run：只解析未写库）")
            return 0

        print()
        check = roster_check(db)
        print(format_check(check))
        if report.warnings:
            return 0
        return 0 if (report.imported or args.dry_run) else 1
    finally:
        db.close()


def cmd_check(args: argparse.Namespace) -> int:
    """检查名册 / 课表能否对上 —— 对不上时缺勤反推会静默失效。"""
    from app.services.roster import format_check, roster_check

    init_db()
    db = SessionLocal()
    try:
        print(format_check(roster_check(db)))
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="app.cli", description="学生状态监测系统 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seed", help="生成模拟数据（1200 学生 + 课表 + 签到）")
    p.add_argument("--students", type=int, default=1200)
    p.add_argument("--days", type=int, default=20)
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("serve", help="启动 Web 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8002)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("collect", help="手动执行一轮采集")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("rebuild", help="用库内事件重算判定结果")
    p.add_argument("--days", type=int, default=60)
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("sign", help="生成翰博尔接口的带签名调试 URL")
    p.add_argument("--path", default="/attendance/api/v1/openApi/getAttendanceStatistics")
    p.add_argument("--param", action="append", help="k=v，可重复")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("state", help="打印当前状态分布")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("prune", help="清理超过保留期的数据（避免磁盘无限增长）")
    p.add_argument("--days", type=int, default=400)
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("backfill", help="回补历史考勤数据（首次接入真实数据必跑）")
    p.add_argument("--days", type=int, default=30, help="回补最近多少天")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("scope", help="查看/清理校区作用域（多校区共用平台的取舍）")
    p.add_argument("--purge", action="store_true", help="真删：清掉作用域外的课程与考勤数据")
    p.add_argument("--drop-students", action="store_true",
                   help="连同「清理后一条事件都不剩」的学生行一起删（默认保留）")
    p.set_defaults(func=cmd_scope)

    p = sub.add_parser(
        "students", help="按学号规则核对/清理学生（只保留本院：202x290x3xxx…）"
    )
    p.add_argument("--purge", action="store_true",
                   help="真删：删掉学号不合规的学生及其全部考勤数据")
    p.add_argument("--yes", action="store_true",
                   help="大比例删除（超过 100 人且占比超过 30%%）时的二次确认")
    p.set_defaults(func=cmd_students)

    p = sub.add_parser("timetable", help="从考勤数据反推某学生/某班的课表（排查课程缺失）")
    p.add_argument("--student-no", default="", help="精确指定学号")
    p.add_argument("--name", default="", help="按姓名（重名会报错，需改用学号）")
    p.add_argument("--grade", default="", help="按年级，如 2025")
    p.add_argument("--major", default="", help="配合 --grade 限定专业码，如 033")
    p.add_argument("--days-shown", type=int, default=7, help="周课表显示几天（默认 7）")
    p.set_defaults(func=cmd_timetable)

    p = sub.add_parser(
        "cuc-probe",
        help="探测中传接口响应结构（只输出键名与类型，不含任何数据值）",
    )
    p.set_defaults(func=cmd_cuc_probe)

    p = sub.add_parser(
        "cuc-demo",
        help="中传接口 demo：只拉一个学号的数据，核对字段与连接关系",
    )
    p.add_argument("--student-no", default="",
                   help="要拉取的学号（填自己的）；不填则用 CUC_DEMO_STUDENT_NO")
    p.set_defaults(func=cmd_cuc_demo)

    p = sub.add_parser("marks", help="查手动标记的流水（谁、何时、为什么）")
    p.add_argument("--student-no", default="", help="只看某个学生")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--backfill", action="store_true",
                   help="给「加流水功能之前」打的历史标记补一条流水")
    p.set_defaults(func=cmd_marks)

    p = sub.add_parser("rekey-students", help="去掉学号里残留的平台前缀（cuc）")
    p.add_argument("--dry-run", action="store_true", help="只看会改哪些，不写库")
    p.set_defaults(func=cmd_rekey_students)

    p = sub.add_parser("stats", help="打印各表行数")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "teach-schedule",
        help="导入教务课程表的「上课时间地点」（把周次折算成具体课次）",
    )
    p.add_argument("file", help="课程表路径（教务处导出的 .xls / .xlsx / .csv）")
    p.add_argument("--week1", default="",
                   help="本学期第 1 周的周一（ISO 日期，如 2026-09-07）；"
                        "默认取配置 SEMESTER_WEEK1_MONDAY")
    p.add_argument("--dry-run", action="store_true", help="只解析并预览，不写库")
    p.set_defaults(func=cmd_teach_schedule)

    p = sub.add_parser(
        "fix-names",
        help="按教务处名单只修正姓名（采集曾用账号昵称覆盖真名）",
    )
    p.add_argument("file", help="选课名单/名册（含「学号」「姓名」列）")
    p.add_argument("--dry-run", action="store_true", help="只列出会改哪些，不写库")
    p.set_defaults(func=cmd_fix_names)

    p = sub.add_parser("import", help="导入学生名册 / 选课名单 / 课表（Excel 或 CSV）")
    p.add_argument(
        "kind",
        choices=["students", "enrollments", "schedule"],
        help="students=学生名册（学号+班级）；enrollments=教务处选课名单（谁选了哪门课）；"
             "schedule=课表（含星期节次，用于判断哪天有课）",
    )
    p.add_argument("file", help="文件路径（.xlsx / .csv）")
    p.add_argument("--dry-run", action="store_true", help="只解析并预览，不写库")
    p.add_argument("--show-samples", action="store_true",
                   help="报告里的跳过样例显示原文（含学号姓名）。"
                        "默认打码，便于把输出贴出去排错而不泄露个人信息")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("check", help="校验名册与课表的班级能否对上")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("deactivate", help="停用学生（毕业/退学；不停用会永久告警）")
    p.add_argument("--grade", action="append", default=[], help="按年级停用，可重复，如 --grade 2021")
    p.add_argument("--student-no", action="append", default=[], help="按学号停用，可重复")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_deactivate)

    p = sub.add_parser("delete-students", help="彻底删除学生及其关联数据（清理误导入）")
    p.add_argument("--student-no", action="append", default=[], help="要删除的学号，可重复")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_delete_students)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
