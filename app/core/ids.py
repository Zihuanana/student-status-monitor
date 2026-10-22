# -*- coding: utf-8 -*-
"""学号规范化。

翰博尔的 ``userNo`` 形如 ``202400000001N`` —— ``cuc`` 是平台账号前缀，
不是学号的一部分（接口同时返回的 ``no`` 字段就是去掉 ``cuc`` 之后的值：
``202400000001N``）。页面上带着 ``cuc`` 既难看，也让人误以为学号长这样，
所以统一在入库前剥掉。

**只在最外层剥一次**，并且大小写不敏感：``CUC2023...`` 和 ``cuc2023...``
都归一成同一个学号，避免同一名学生在库里出现两行。
"""

from __future__ import annotations

import re

#: 平台账号前缀。锚定在开头，只剥一次，避免把学号中间出现的 "cuc" 也吃掉。
_PREFIX = re.compile(r"^cuc", re.IGNORECASE)


def normalize_student_no(raw: str | None) -> str:
    """剥掉 ``cuc`` 前缀并去空白。

    >>> normalize_student_no("202400000001N")
    '202400000001N'
    >>> normalize_student_no("  202400000001N ")
    '202400000001N'
    >>> normalize_student_no("202400000001N")
    '202400000001N'
    """
    # 去掉前缀后还要再 strip 一次：'cuc 2023...' 这种带空格的写法，
    # 剥掉 cuc 会留下一个前导空格。
    return _PREFIX.sub("", (raw or "").strip()).strip()


def needs_normalizing(raw: str | None) -> bool:
    """这个学号是否还带着前缀（迁移脚本用来挑出要改的行）。"""
    text = (raw or "").strip()
    return bool(text) and text != normalize_student_no(text)
