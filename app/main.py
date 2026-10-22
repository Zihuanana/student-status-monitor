"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from app import __version__
from app.api import api_router
from app.core.config import BASE_DIR, settings
from app.core.db import init_db
from app.core.logging_conf import get_logger, setup_logging

log = get_logger("app")

WEB_DIST = BASE_DIR / "web" / "dist"


def _sync_major_aliases() -> None:
    """把配置里的专业代码→名称补进库（库里有名的以库为准）。

    学号里只有代码（013/023/033），不映射的话页面全显示「2023级专业013」。
    """
    from app.core.db import SessionLocal
    from app.services.ingest import sync_major_aliases

    db = SessionLocal()
    try:
        added = sync_major_aliases(db)
        if added:
            log.info("已同步 %d 条专业别名（MAJOR_ALIASES）", added)
    except Exception as exc:  # noqa: BLE001 - 别名只是显示问题，不该挡住启动
        log.warning("专业别名同步失败：%s", exc)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    _sync_major_aliases()
    log.info("学生状态监测系统 v%s 启动，模式=%s", __version__, settings.data_mode)
    if not settings.gate_password:
        log.error("GATE_PASSWORD 为空，任何人都能进入系统，请立即设置！")
    if settings.session_secret in ("", "change-me"):
        log.error("SESSION_SECRET 仍为默认值，会话可被伪造，请立即改成随机字符串！")
    if not settings.is_mock:
        from app.services.scope import describe_scope, describe_student_scope

        if settings.campus_keyword_list:
            log.info("校区作用域：%s（HB_CAMPUS_KEYWORDS）", describe_scope())
        else:
            # 翰博尔是多校区共用平台，接口没有校区参数。不设关键词就会把
            # 全校所有校区的学生一起采进来（实测混入 7000+ 非本院学生）。
            log.warning(
                "HB_CAMPUS_KEYWORDS 为空 —— 将采集翰博尔平台上**所有校区**的数据。"
                "若这不是本意，请设置该关键词（如 海南）并跑 python -m app.cli scope --purge"
            )

        # 第二道闸门。为什么不靠上面那道就够了：关键词切的是**课程**，
        # 靠的是上游给节次起名时带上了「海南」二字 —— 平台改个名就静默失效，
        # 而实测它确实失效过（线上 5957 人里只有 1129 人是本院的）。
        # 学号形态是学校定的，不随平台改版变化。
        if settings.student_id_like is not None:
            log.info(
                "学号作用域：%s（HB_STUDENT_ID_PATTERN）", describe_student_scope()
            )
        else:
            log.warning(
                "HB_STUDENT_ID_PATTERN 为空或已关闭 —— 任何学号都会入库。"
                "若这不是本意，请设置该正则（本院为 (?i)^(cuc)?202\\d290\\d3\\d{3}[0-9A-Z]?$）"
                "并跑 python -m app.cli students --purge"
            )

    task: asyncio.Task | None = None
    from app.api.admin import POLLING_ACTIVE

    if settings.collect_enabled and settings.run_background_jobs:
        from app.services.collect import collect_loop

        task = asyncio.create_task(collect_loop())
        POLLING_ACTIVE["value"] = True
        log.info("采集轮询已启动，间隔 %ss", settings.poll_interval_seconds)
    elif not settings.collect_enabled:
        log.warning("采集已关闭（COLLECT_ENABLED=false），仅提供查询与重算")
    else:
        log.warning("后台轮询未启动（RUN_BACKGROUND_JOBS=false），可手动触发采集")

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        POLLING_ACTIVE["value"] = False
        log.info("应用已停止")


app = FastAPI(
    title="学生状态监测系统",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

if settings.debug:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """内联图标（生产环境 /favicon.ico 由 nginx 处理，这里主要服务本机调试）。"""
    svg = (
        b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
        b"<rect width='16' height='16' rx='2' fill='#245edc'/></svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


# ─────────────────────────────────────────────────────────────
# 前端静态资源
# ─────────────────────────────────────────────────────────────
def _dist_dir() -> Path | None:
    return WEB_DIST if (WEB_DIST / "index.html").exists() else None


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str, request: Request):
    """把前端挂载到隐藏子路径下；非该路径下一律 404。

    生产环境由 nginx 把整个隐藏路径反代到这里（见 deploy/nginx 片段），
    因此应用外壳本身也受口令保护 —— 未登录者拿不到任何前端代码。
    """
    base = settings.app_base_path
    rel = full_path.strip("/")

    if not rel.startswith(base):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    dist = _dist_dir()
    if dist is None:
        return JSONResponse(
            {
                "detail": "前端尚未构建",
                "hint": "在 web/ 目录执行 npm install && npm run build",
            },
            status_code=503,
        )

    sub = rel[len(base):].strip("/")
    candidate = (dist / sub).resolve() if sub else None
    if candidate is not None:
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            # 路径穿越（如 ../..）一律拒绝
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if candidate.is_file():
            resp = FileResponse(candidate)
            # 带内容哈希的构建产物可长期缓存；
            # 其余静态资源（logo 等）与 index.html 都不缓存，便于更新。
            if sub.startswith("assets/"):
                resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
            else:
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            return resp

    index = dist / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "前端产物缺失"}, status_code=503)
    resp = FileResponse(index)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp
