# -*- coding: utf-8 -*-
"""测试包。

**关键**：在导入任何测试模块之前就把数据库指向临时目录。

为什么放在这里
--------------
``app.core.config.settings`` 与 ``app.core.db`` 都是**模块级单例**，引擎在
**首次导入 app.core.db 时**就按当时的 ``DATABASE_URL`` 建好。测试发现过程按
文件名顺序导入模块，于是：

  - ``test_attendance.py``（第 1 个）会 import ``app.core.config``；
  - 此时若还没人设置 ``DATABASE_URL``，引擎就绑定到**工作区真实库**
    ``data/monitor.db``；
  - 之后再怎么替换 ``db_module.engine`` 也没用 ——
    ``init_db()`` / ``apply_light_migrations()`` 里对模块级 ``engine`` 的引用
    形成了闭包捕获（该问题已在 ``app/core/db.py`` 用 ``current_engine()`` 修掉，
    但"谁先导入"这个顺序问题仍要靠这里解决）。

本项目真出过事：``test_timeline.py`` 在工作区库上跑演练，把 1200 条演示数据
清成了 8 条。放在包 ``__init__`` 里是最早、也最不依赖顺序的时机。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / f"sm-test-{os.getpid()}" / "test.db"
_TEST_DB.parent.mkdir(parents=True, exist_ok=True)

#: 所有测试共用的隔离库路径
TEST_DB_PATH = str(_TEST_DB)

# 用 APP_ENV_FILE 指向不存在的文件，避免读到工作区 .env（它有 DATA_MODE=mock、
# 真实 DATABASE_URL 等会干扰测试的值）。
os.environ["APP_ENV_FILE"] = str(_TEST_DB.parent / "none.env")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

# 学号作用域默认**关闭**：本仓库的测试用的是 ``2024010001`` 这类占位学号，
# 它们不符合本院真实形态（202x290x3xxxN）。开着开关会让几十个与作用域
# 无关的测试集体静默失败（学生一个都写不进去），那种失败极难定位。
# 与作用域有关的测试在 tests/test_scope.py 里**显式打开**再断言。
os.environ.setdefault("HB_STUDENT_ID_SCOPE_ENABLED", "false")
