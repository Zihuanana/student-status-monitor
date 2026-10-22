"""读取信息化处给的表格文件（CSV / XLSX），不引入第三方依赖。

为什么自己解析 XLSX
-------------------
XLSX 本质就是一个 zip 包（OpenXML），用标准库 ``zipfile`` + ``xml.etree``
即可读出单元格，无需 pandas / openpyxl —— 与项目「服务器内存紧张、依赖尽量少」
的取舍一致，也避免部署时再装几十 MB 的包。

CSV 的坑：国内教务系统导出的 CSV 多半是 **GBK/GB18030** 编码，
还可能带 UTF-8 BOM。这里按 UTF-8-SIG → GB18030 → UTF-8 的顺序尝试，
并自动嗅探分隔符（逗号 / 制表符 / 分号）。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────

_ENCODINGS = ("utf-8-sig", "gb18030", "utf-8")


def sniff_text(data: bytes) -> tuple[str, str]:
    """返回 (文本, 编码名)。"""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace"), "utf-8(replace)"


def read_csv_rows(path: Path) -> tuple[list[list[str]], str]:
    """读取 CSV，返回 (行, 编码说明)。"""
    raw = path.read_bytes()
    text, enc = sniff_text(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [[(c or "").strip() for c in row] for row in reader]
    rows = [r for r in rows if any(c for c in r)]
    return rows, f"CSV/{enc}/分隔符={delimiter!r}"


# ─────────────────────────────────────────────────────────────
# XLSX
# ─────────────────────────────────────────────────────────────

_CELL_RE = re.compile(r"([A-Z]+)(\d+)")
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def col_to_index(ref: str) -> int:
    """把 'A1' / 'AB12' 的列字母部分转成 0 基下标。"""
    letters = _CELL_RE.match(ref)
    if not letters:
        return 0
    idx = 0
    for ch in letters.group(1):
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = __import__("xml.etree.ElementTree", fromlist=["ET"]).fromstring(
        zf.read("xl/sharedStrings.xml")
    )
    out: list[str] = []
    for si in root.findall(f"{_NS}si"):
        # 一个 si 可能由多个 <t> 组成（富文本）
        out.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    return out


def _first_sheet_path(zf: zipfile.ZipFile) -> str:
    """找出第一个工作表在包内的路径。"""
    names = zf.namelist()
    if "xl/workbook.xml" in names:
        import xml.etree.ElementTree as ET

        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = wb.find(f"{_NS}sheets")
        if sheets is not None:
            for sheet in sheets:
                rid = sheet.get(f"{_REL_NS}id")
                target = None
                if rid and "xl/_rels/workbook.xml.rels" in names:
                    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
                    for rel in rels:
                        if rel.get("Id") == rid:
                            target = rel.get("Target")
                            break
                if target:
                    target = target.lstrip("/")
                    if not target.startswith("xl/"):
                        target = "xl/" + target
                    if target in names:
                        return target
    # 兜底：直接找第一个 worksheets/sheet*.xml
    for n in sorted(names):
        if n.startswith("xl/worksheets/") and n.endswith(".xml"):
            return n
    raise ValueError("XLSX 中找不到工作表")


def read_xlsx_rows(path: Path) -> tuple[list[list[str]], str]:
    """读取 XLSX 的第一个工作表，返回 (行, 说明)。"""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        sheet_path = _first_sheet_path(zf)
        root = ET.fromstring(zf.read(sheet_path))

    rows: list[list[str]] = []
    for row in root.iter(f"{_NS}row"):
        cells: dict[int, str] = {}
        for c in row.findall(f"{_NS}c"):
            ref = c.get("r") or ""
            idx = col_to_index(ref) if ref else len(cells)
            ctype = c.get("t")
            value = ""
            if ctype == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
            else:
                v = c.find(f"{_NS}v")
                if v is not None and v.text is not None:
                    if ctype == "s":
                        try:
                            value = shared[int(v.text)]
                        except (ValueError, IndexError):
                            value = ""
                    else:
                        value = v.text
            cells[idx] = (value or "").strip()
        if not cells:
            continue
        width = max(cells) + 1
        rows.append([cells.get(i, "") for i in range(width)])

    rows = [r for r in rows if any(c for c in r)]
    return rows, f"XLSX/{Path(sheet_path).name}"


def read_table(path: Path) -> tuple[list[list[str]], str]:
    """按扩展名选择解析器。"""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx_rows(path)
    if suffix == ".xls":
        raise ValueError(
            "不支持旧版 .xls 二进制格式；请在 Excel/WPS 中「另存为 .xlsx 或 CSV」后重试"
        )
    return read_csv_rows(path)


# ─────────────────────────────────────────────────────────────
# 表头识别
# ─────────────────────────────────────────────────────────────

#: 字段 → 可能的表头写法（统一转小写、去空白后比较）
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "student_no": ("学号", "学生学号", "学籍号", "userNo", "stuNo", "studentno", "编号", "工号"),
    "name": ("姓名", "学生姓名", "名字", "name", "studentname"),
    "grade": ("年级", "grade", "入学年份", "年级名称"),
    "major_code": ("专业代码", "专业号", "majorcode", "专业编码"),
    "major_name": ("专业", "专业名称", "majorname", "专业(名称)"),
    "class_name": ("班级", "班级名称", "行政班", "classname", "class", "教学班"),
    "org_name": ("学院", "院系", "系别", "orgname", "学院名称", "organization",
                 # 教务处导出用「所在学院」——本项目真实导出的表头就是它
                 "所在学院", "所属学院", "所属院系"),
    # 课表相关
    "course_name": ("课程", "课程名称", "科目", "coursename", "课程名"),
    "course_no": ("课程号", "课程编号", "课程代码", "courseno", "课程号（教务）"),
    "teacher_name": ("教师", "任课教师", "老师", "teachername"),
    "weekday": ("星期", "周几", "weekday", "星期几"),
    "node_name": ("节次", "节", "node", "nodename"),
    "start_time": ("开始时间", "上课时间", "starttime", "起始时间"),
    "end_time": ("结束时间", "下课时间", "endtime", "截止时间"),
    "weeks": ("周次", "上课周次", "weeks", "周数"),
    "classroom": ("教室", "上课地点", "classroom", "地点", "教室名称"),
}


def normalize_header(text: str) -> str:
    return re.sub(r"[\s　()（）\[\]【】:：*]+", "", str(text or "")).lower()


def detect_columns(header: list[str]) -> dict[str, int]:
    """把表头行映射成 {字段名: 列下标}。"""
    normalized = [normalize_header(h) for h in header]
    mapping: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            key = normalize_header(alias)
            if key in normalized:
                mapping[field] = normalized.index(key)
                break
    return mapping


def looks_like_header(row: list[str]) -> bool:
    """判断某行是否为表头。"""
    detected = detect_columns(row)
    return "student_no" in detected or "course_name" in detected


def find_header_row(rows: list[list[str]], max_scan: int = 10) -> int:
    """在开头若干行里找表头（教务导出常在前面放标题行）。"""
    for i, row in enumerate(rows[:max_scan]):
        if looks_like_header(row):
            return i
    return 0


def cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx]).strip()


def iter_data_rows(rows: list[list[str]]) -> Iterator[list[str]]:
    """跳过表头与全空行。"""
    if not rows:
        return
    start = find_header_row(rows) + 1
    for row in rows[start:]:
        if any(c.strip() for c in row):
            yield row


__all__ = [
    "read_table",
    "detect_columns",
    "find_header_row",
    "iter_data_rows",
    "cell",
    "normalize_header",
    "looks_like_header",
    "sniff_text",
]
