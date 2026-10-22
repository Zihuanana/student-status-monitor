"""数据库引擎与会话。SQLite 下开启 WAL，兼顾并发读与写入。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


def _make_engine():
    url = settings.database_url
    kwargs: dict = {"future": True, "echo": False}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - 驱动回调
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务作用域：正常提交，异常回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_engine():
    """返回**当前**引擎。

    不要直接引用模块级 ``engine`` 名字 —— 本文件顶部写过
    ``engine = _make_engine()``，若在函数里以局部名引用它，就形成了闭包捕获：
    之后 ``db_module.engine = other`` 这种替换**不会生效**，
    函数仍会用旧引擎（本项目踩过：测试隔离时表被建到了工作区真实库上）。
    统一通过本函数读取，保证"替换模块属性"这一常规做法确实有效。
    """
    return engine


def init_db() -> None:
    from app import models  # noqa: F401  确保所有表已注册
    from app.core.base import Base

    Base.metadata.create_all(bind=current_engine())
    apply_light_migrations()


def apply_light_migrations() -> list[str]:
    """给已存在的表补上新增的列。

    为什么需要
    ----------
    ``Base.metadata.create_all()`` 只会**建缺失的表**，对已存在的表**不会加列**。
    因此给模型加字段后：新库测试全绿、旧库一升级就报
    ``no such column: alerts.detail_json`` —— 而且是在采集过程中报，
    表现为"采集一直失败"。

    这里只处理**新增列**这一种情况（本项目至今唯一遇到的演进）。
    更复杂的变更（改类型、改约束）需要正式的迁移工具，见下方说明。

    SQLite 的 ``ALTER TABLE ... ADD COLUMN`` 支持带常量默认值，
    因此 NOT NULL 列需要显式给 ``server_default``。
    """
    from sqlalchemy import inspect, text

    # 必须显式导入模型：Base.metadata 只有在模型模块被导入后才包含各表。
    # 依赖"调用方恰好已经导入过 app.models"是不可靠的 ——
    # 测试收集阶段就出现过 metadata.sorted_tables 为空、迁移静默不生效。
    from app import models  # noqa: F401
    from app.core.base import Base

    applied: list[str] = []
    inspector = inspect(current_engine())
    existing_tables = set(inspector.get_table_names())

    with current_engine().begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all 已经建好
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(current_engine().dialect)}'
                if column.server_default is not None:
                    ddl += f" DEFAULT {column.server_default.arg}"
                elif not column.nullable:
                    # 无显式默认值但要求非空：给类型对应的零值
                    ddl += " DEFAULT ''" if column.type.python_type is str else " DEFAULT 0"
                conn.execute(text(ddl))
                applied.append(f"{table.name}.{column.name}")

    if applied:
        import logging

        logging.getLogger("core.db").warning(
            "已为既有表补充新列：%s（请确认默认值符合预期）", ", ".join(applied)
        )
    return applied
