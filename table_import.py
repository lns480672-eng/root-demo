"""Inspect CSV and XLSX input and build a deterministic, isolated table tool."""

import base64
import binascii
import csv
import io
import json
import os
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import escape, unescape
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from accounts import ApiError, TABLE_STORAGE_BYTES, pack_entries


@dataclass(frozen=True)
class TableLimits:
    bytes: int = 1024 * 1024
    rows: int = 500
    columns: int = 20


def limits_from_env():
    defaults = TableLimits()
    values = {}
    for field, name, ceiling in (
        ("bytes", "ROOT_TABLE_MAX_BYTES", 8 * 1024 * 1024),
        ("rows", "ROOT_TABLE_MAX_ROWS", 5000),
        ("columns", "ROOT_TABLE_MAX_COLUMNS", MAX_TABLE_COLUMNS),
    ):
        raw = os.environ.get(name, str(getattr(defaults, field)))
        if not re.fullmatch(r"[0-9]{1,8}", raw) or not 1 <= int(raw) <= ceiling:
            raise ValueError(f"{name} 必须是 1–{ceiling} 的整数。")
        values[field] = int(raw)
    return TableLimits(**values)


MAX_TABLE_COLUMNS = 200
MAX_HEADER_LENGTH = 80
MAX_CELL_LENGTH = 4096
MAX_FILENAME_LENGTH = 120
MAX_XLSX_XML_BYTES = 16 * 1024 * 1024
MAX_XLSX_ELEMENTS = 250000
COLUMN_TYPES = frozenset({"text", "number", "date", "boolean", "option"})
TABLE_KEY = "root.table.v1"
SUGGESTION = "交付跟进板"
EXAMPLE_FILENAME = "示例表格 · 交付跟进表.csv"
EXAMPLE_CSV = (
    "事项,负责人,截止日,状态\n"
    "确认演示范围,小林,11 月 8 日,待完成\n"
    "准备样例数据,小周,11 月 10 日,待完成\n"
    "复核体验入口,小陈,11 月 9 日,已完成\n"
)
NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_DAY = re.compile(r"([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日")
LABEL_CONTROLS = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
# csv.reader(strict=True) still accepts a bare quote inside an unquoted field.
CSV_FIELD = re.compile(r'(?:"(?:[^"]|"")*"|[^",\r\n]*)(?:,|\r\n|\r|\n|\Z)')
BOOLEAN_VALUES = frozenset({"true", "false", "yes", "no", "是", "否"})
DONE_OPTIONS = frozenset({"完成", "已完成", "已读", "已关闭", "done", "completed", "closed"})
STATUS_OPTIONS = DONE_OPTIONS | {"未完成", "待完成", "待办", "未开始", "进行中", "未读", "open", "pending", "todo"}
STATUS_HEADERS = frozenset({"状态", "完成状态", "status"})
WORKBOOK_NAMESPACES = frozenset({
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "http://purl.oclc.org/ooxml/spreadsheetml/main",
})
RELATIONSHIP_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
    "{http://purl.oclc.org/ooxml/officeDocument/relationships}id",
)
CELL_ADDRESS = re.compile(r"([A-Z]{1,3})[1-9][0-9]{0,6}\Z")
EXCEL_ESCAPE = re.compile(r"_x([0-9a-fA-F]{4})_")


def valid_label(value, limit):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and bool(value.strip())
        and not LABEL_CONTROLS.search(value)
    )


def validate_filename(source, filename):
    if source == "example" and filename == EXAMPLE_FILENAME:
        return
    if (
        source != "upload"
        or not valid_label(filename, MAX_FILENAME_LENGTH)
        or not filename.casefold().endswith((".csv", ".xlsx"))
        or "/" in filename
        or "\\" in filename
    ):
        raise ApiError(400, "csv_format", "请选择文件名不超过 120 字符的 CSV 或 XLSX 文件，或试用示例。")


def header_names(values, limits):
    if not 1 <= len(values) <= limits.columns:
        raise ApiError(400, "csv_columns", f"请保留 1–{limits.columns} 列后重新选择表格。")
    names = [value.strip(" \u3000") if isinstance(value, str) else "" for value in values]
    if not all(valid_label(name, MAX_HEADER_LENGTH) for name in names):
        raise ApiError(400, "csv_headers", "请为每列填写不超过 80 字符的单行表头。")
    if len({name.casefold() for name in names}) != len(names):
        raise ApiError(400, "csv_headers", "表头不能重名，请修改重复的字段名。")
    return names


def validate_rows(rows, width, limits):
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, "csv_no_rows", "表头下至少需要一条记录。")
    if len(rows) > limits.rows:
        raise ApiError(413, "csv_rows", f"表格最多支持 {limits.rows} 条记录，请减少记录后重试。")
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) != width:
            raise ApiError(400, "csv_width", f"第 {number} 条记录的列数与表头不一致，请核对字段。")
        if any(not isinstance(cell, str) or len(cell) > MAX_CELL_LENGTH or "\0" in cell for cell in row):
            raise ApiError(400, "csv_cell", f"第 {number} 条记录含有无效单元格，请检查内容或将单元格缩短到 4096 字符以内。")


def is_date(value):
    try:
        if ISO_DATE.fullmatch(value):
            date.fromisoformat(value)
            return True
        if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T", value):
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        if match := MONTH_DAY.fullmatch(value):
            date(2000, int(match[1]), int(match[2]))
            return True
    except ValueError:
        pass
    return False


def infer_type(values):
    known = [value.strip().casefold() for value in values if value.strip()]
    if not known:
        return "text"
    if all(value in BOOLEAN_VALUES for value in known):
        return "boolean"
    if all(value in STATUS_OPTIONS for value in known):
        return "option"
    if all(is_date(value) for value in known):
        return "date"
    if all(NUMBER.fullmatch(value) for value in known):
        return "number"
    return "text"


def size_error(limits):
    return ApiError(413, "csv_size", f"文件不能超过 {limits.bytes:,} 字节，请缩小后重试。")


def read_csv(raw, limits):
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "csv_empty", "CSV 为空，请选择有表头和记录的文件。")
    try:
        byte_count = len(raw.encode("utf-8"))
    except UnicodeError:
        raise ApiError(400, "csv_encoding", "请选择用 UTF-8 编码保存的 CSV 文件。") from None
    if byte_count > limits.bytes:
        raise size_error(limits)
    raw = raw.removeprefix("\ufeff")
    position = 0
    while position < len(raw):
        match = CSV_FIELD.match(raw, position)
        if not match:
            raise ApiError(400, "csv_syntax", "无法解析 CSV，请检查逗号分隔和双引号配对。")
        position = match.end()
    try:
        reader = csv.reader(io.StringIO(raw, newline=""), strict=True)
        header = next((row for row in reader if row), None)
        if header is None:
            raise ApiError(400, "csv_empty", "CSV 为空，请选择有表头和记录的文件。")
        names = header_names(header, limits)
        rows = []
        for row in reader:
            if row:
                if len(rows) == limits.rows:
                    raise ApiError(413, "csv_rows", f"表格最多支持 {limits.rows} 条记录，请减少记录后重试。")
                rows.append(row)
    except csv.Error:
        raise ApiError(400, "csv_syntax", "无法解析 CSV，请检查逗号分隔和双引号配对。") from None
    return names, rows


def xlsx_error(message="无法读取 XLSX，请用 Excel 另存为 .xlsx 或 UTF-8 CSV 后重试。"):
    return ApiError(400, "xlsx_format", message)


def xml_reader(book):
    remaining = MAX_XLSX_XML_BYTES
    names = book.namelist()
    if len(names) > 4096 or len(set(names)) != len(names):
        raise xlsx_error("XLSX 包含过多文件或重复路径，请另存为精简工作簿。")

    def read(path):
        nonlocal remaining
        info = book.getinfo(path)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise xlsx_error("XLSX 使用不支持的压缩方式，请在 Excel 中另存后重试。")
        if info.file_size > remaining:
            raise ApiError(413, "xlsx_expanded_size", "XLSX 解压后过大，请拆分工作表后重试。")
        with book.open(info) as member:
            raw = member.read(info.file_size + 1)
        if len(raw) != info.file_size:
            raise xlsx_error()
        remaining -= len(raw)
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            xml = raw.decode("utf-16")
        else:
            xml = raw.decode("utf-8-sig")
        if "\0" in xml or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
            raise xlsx_error("XLSX 含有不支持的 XML 声明，请另存为新的工作簿。")
        if xml.count("<") > MAX_XLSX_ELEMENTS:
            raise ApiError(413, "xlsx_expanded_size", "XLSX 工作表结构过大，请拆分工作表后重试。")
        return ElementTree.fromstring(xml)

    return read


def relationships(read, part=""):
    directory, name = posixpath.split(part)
    rels = read(posixpath.join(directory, "_rels", name + ".rels"))
    if rels.tag != "{http://schemas.openxmlformats.org/package/2006/relationships}Relationships":
        raise xlsx_error()
    result = {}
    for item in rels:
        identifier = item.get("Id")
        if not identifier or identifier in result:
            raise xlsx_error()
        result[identifier] = item
    return result


def part_target(item, source=""):
    if item.get("TargetMode", "Internal") != "Internal":
        raise xlsx_error("工作表指向外部文件，请先将数据复制到此工作簿。")
    target = urlsplit(item.get("Target", ""))
    path = unquote(target.path)
    if target.scheme or target.netloc or target.query or target.fragment or not path or "\\" in path or "\0" in path:
        raise xlsx_error()
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), path)).lstrip("/")
    if path == ".." or path.startswith("../") or path in ("", "."):
        raise xlsx_error()
    return path


def relation_type(item):
    return item.get("Type", "").rsplit("/", 1)[-1]


def text_value(value):
    decoded = EXCEL_ESCAPE.sub(lambda match: chr(int(match[1], 16)), value)
    if decoded != value:
        decoded = decoded.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")
    return decoded


def rich_text(node, tag):
    if node is None:
        return ""
    pieces = [child.text or "" for child in node.findall(tag("t"))]
    pieces.extend(child.text or "" for child in node.findall(tag("r") + "/" + tag("t")))
    return text_value("".join(pieces))


def xlsx_formats(styles, tag):
    if styles is None:
        return []
    custom = {item.get("numFmtId"): item.get("formatCode", "")
              for item in styles.findall(tag("numFmts") + "/" + tag("numFmt"))}
    result = []
    for item in styles.findall(tag("cellXfs") + "/" + tag("xf")):
        code = item.get("numFmtId", "0")
        pattern = custom.get(code, "")
        clean = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", pattern).split(";", 1)[0].lower()
        calendar = code in {"14", "15", "16", "17", "22", "27", "28", "29", "30", "31", "34", "35", "36",
                            "50", "51", "52", "53", "54", "55", "56", "57", "58"} or bool(re.search(r"[yd]", clean))
        timed = code in {"18", "19", "20", "21", "22", "45", "46", "47"} or bool(re.search(r"[hs]", clean))
        zero_width = len(pattern) if re.fullmatch(r"0{2,20}", pattern) else 0
        result.append((calendar, timed, zero_width))
    return result


def formatted_number(value, style, date1904):
    calendar, timed, zero_width = style
    if zero_width and re.fullmatch(r"[0-9]+", value):
        return value.zfill(zero_width)
    if not calendar and not timed:
        return value
    serial = Decimal(value)
    if not serial.is_finite() or serial < 0 or serial > 3000000:
        return value
    days = int(serial)
    seconds = int(round((serial - days) * 86400))
    if calendar:
        if date1904:
            origin = datetime(1904, 1, 1)
        else:
            origin = datetime(1899, 12, 31) if days < 60 else datetime(1899, 12, 30)
        if days == 60 and not date1904:
            return value
        stamp = origin + timedelta(days=days, seconds=seconds)
        return stamp.isoformat(timespec="seconds") if timed else stamp.date().isoformat()
    return str(timedelta(seconds=seconds))


def cell_value(cell, tag, strings, formats, date1904):
    kind = cell.get("t", "n")
    raw = cell.findtext(tag("v"))
    if kind == "inlineStr":
        return rich_text(cell.find(tag("is")), tag)
    if raw is None or raw == "":
        formula = cell.findtext(tag("f"))
        if formula is not None:
            raise xlsx_error("工作表有未保存计算结果的公式，请在 Excel 中重新计算并保存，或粘贴为数值。")
        return ""
    if len(raw) > MAX_CELL_LENGTH:
        raise ApiError(400, "csv_cell", f"工作表含有超过 {MAX_CELL_LENGTH} 字符的单元格，请缩短后重试。")
    if kind == "s":
        if not re.fullmatch(r"[0-9]{1,9}", raw):
            raise xlsx_error()
        return strings[int(raw)]
    if kind == "b":
        if raw not in {"0", "1"}:
            raise xlsx_error()
        return "true" if raw == "1" else "false"
    if kind == "n":
        style = int(cell.get("s", "0"))
        if formats:
            if not 0 <= style < len(formats):
                raise xlsx_error()
            return formatted_number(raw, formats[style], date1904)
    if kind not in {"n", "str", "d", "e"}:
        raise xlsx_error()
    return text_value(raw)


def read_workbook(book, limits):
    read = xml_reader(book)
    office = next(item for item in relationships(read).values() if relation_type(item) == "officeDocument")
    workbook_path = part_target(office)
    workbook = read(workbook_path)
    namespace = workbook.tag.split("}", 1)[0].removeprefix("{")
    if namespace not in WORKBOOK_NAMESPACES:
        raise xlsx_error()

    def tag(name):
        return "{" + namespace + "}" + name

    if workbook.tag != tag("workbook"):
        raise xlsx_error()
    rels = relationships(read, workbook_path)
    for sheet in workbook.findall(tag("sheets") + "/" + tag("sheet")):
        identifier = next((sheet.get(attr) for attr in RELATIONSHIP_ID if sheet.get(attr)), None)
        item = rels.get(identifier)
        if item is None:
            raise xlsx_error("XLSX 的工作表引用不完整，请在 Excel 中另存后重试。")
        if relation_type(item) == "worksheet":
            worksheet = read(part_target(item, workbook_path))
            break
    else:
        raise xlsx_error("XLSX 没有可读取的工作表。")
    if worksheet.tag != tag("worksheet"):
        raise xlsx_error()
    related = {relation_type(item): item for item in rels.values()}
    strings = []
    if "sharedStrings" in related:
        strings_root = read(part_target(related["sharedStrings"], workbook_path))
        if strings_root.tag != tag("sst"):
            raise xlsx_error()
        strings = [rich_text(item, tag) for item in strings_root.findall(tag("si"))]
    styles = read(part_target(related["styles"], workbook_path)) if "styles" in related else None
    if styles is not None and styles.tag != tag("styleSheet"):
        raise xlsx_error()
    formats = xlsx_formats(styles, tag)
    workbook_options = workbook.find(tag("workbookPr"))
    date1904 = workbook_options is not None and workbook_options.get("date1904") in {"1", "true"}
    rows = []
    names = None
    for row in worksheet.iterfind(tag("sheetData") + "/" + tag("row")):
        values = {}
        column = 0
        for cell in row.findall(tag("c")):
            address = cell.get("r")
            if address is not None:
                match = CELL_ADDRESS.fullmatch(address)
                if not match:
                    raise xlsx_error()
                column = 0
                for letter in match[1]:
                    column = column * 26 + ord(letter) - ord("A") + 1
            else:
                column += 1
            value = cell_value(cell, tag, strings, formats, date1904)
            if value == "":
                continue
            if column in values:
                raise xlsx_error()
            if column > limits.columns:
                raise ApiError(413, "csv_columns", f"工作表最多支持 {limits.columns} 列，请减少列后重试。")
            values[column] = value
        if not values:
            continue
        if names is None:
            names = header_names([values.get(index, "") for index in range(1, max(values) + 1)], limits)
            continue
        if max(values) > len(names):
            raise ApiError(400, "csv_width", "工作表有数据列缺少表头，请补齐字段名。")
        if len(rows) == limits.rows:
            raise ApiError(413, "csv_rows", f"表格最多支持 {limits.rows} 条记录，请减少记录后重试。")
        rows.append([values.get(index, "") for index in range(1, len(names) + 1)])
    if names is None:
        raise xlsx_error("第一个工作表为空，请补充表头和记录。")
    return names, rows, sheet.get("name", "")


def read_xlsx(encoded, limits):
    if not isinstance(encoded, str) or not encoded:
        raise xlsx_error()
    if len(encoded) > 4 * ((limits.bytes + 2) // 3):
        raise size_error(limits)
    try:
        content = base64.b64decode(encoded, validate=True)
        if len(content) > limits.bytes:
            raise size_error(limits)
        with zipfile.ZipFile(io.BytesIO(content)) as book:
            return read_workbook(book, limits)
    except (binascii.Error, zipfile.BadZipFile, ElementTree.ParseError, UnicodeError,
            KeyError, IndexError, StopIteration, ValueError, InvalidOperation, OverflowError,
            RuntimeError, OSError, NotImplementedError, zlib.error):
        raise xlsx_error() from None


def inspect_table(data, limits=TableLimits()):
    if not isinstance(data, dict):
        raise ApiError(400, "csv_format", "请选择 CSV 或 XLSX 文件，或试用示例。")
    source = data.get("source")
    filename = EXAMPLE_FILENAME if source == "example" else data.get("filename")
    validate_filename(source, filename)
    result = {"source": source, "filename": filename, "suggestion": SUGGESTION}
    if filename.casefold().endswith(".xlsx"):
        names, rows, result["sheet"] = read_xlsx(data.get("xlsx"), limits)
    else:
        names, rows = read_csv(EXAMPLE_CSV if source == "example" else data.get("csv"), limits)
    validate_rows(rows, len(names), limits)
    columns = [{"name": name, "type": infer_type(values)} for name, values in zip(names, zip(*rows))]
    return {**result, "columns": columns, "rows": rows}


def build_table_tool(table, limits=TableLimits()):
    if not isinstance(table, dict):
        raise ApiError(400, "invalid_table", "请先选择表格并确认识别结果。")
    source, filename = table.get("source"), table.get("filename")
    validate_filename(source, filename)
    columns = table.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= limits.columns or any(
        not isinstance(column, dict) or type(column.get("include", True)) is not bool for column in columns
    ):
        raise ApiError(400, "csv_columns", "字段配置无效，请重新确认识别结果。")
    included = [index for index, column in enumerate(columns) if column.get("include", True)]
    if not included:
        raise ApiError(400, "csv_columns", "请至少保留一个要导入的字段。")
    selected = [columns[index] for index in included]
    if any(not isinstance(column.get("type"), str) or column["type"] not in COLUMN_TYPES for column in selected):
        raise ApiError(400, "csv_columns", "字段类型无效，请重新确认识别结果。")
    names = header_names([column.get("name") for column in selected], limits)
    rows = table.get("rows")
    validate_rows(rows, len(columns), limits)
    rows = [[row[index] for index in included] for row in rows]
    columns = [{"name": name, "type": column["type"]} for name, column in zip(names, selected)]
    status_column = next((index for index, name in enumerate(names) if name.casefold() in STATUS_HEADERS), None)
    if status_column is None:
        status_column = next((
            index for index, values in enumerate(zip(*rows)) if infer_type(values) == "option"
        ), None)
    done = [
        status_column is not None and row[status_column].strip().casefold() in DONE_OPTIONS
        for row in rows
    ]
    seed = {"filename": filename, "columns": columns, "rows": rows, "done": done}
    sheet = table.get("sheet")
    if filename.casefold().endswith(".xlsx") and valid_label(sheet, 31):
        seed["sheet"] = sheet
    entries = [[TABLE_KEY, json.dumps(seed, ensure_ascii=False, separators=(",", ":"))]]
    try:
        pack_entries(entries, limit=TABLE_STORAGE_BYTES)
    except UnicodeError:
        raise ApiError(400, "csv_encoding", "确认结果含有无效字符，请重新选择 UTF-8 CSV。") from None
    except ApiError as error:
        if error.code != "tool_quota":
            raise
        raise ApiError(413, "csv_size", "确认后的表格超过工具存储空间，请减少行列后重试。") from None
    return SUGGESTION, render_tool(), entries


# Initial table template shipped before column-filter revisions (root.table.v1).
LEGACY_TOOL_DIGEST = "c22c4557b1a65d58e6332cc73cac304b925d797e96792c384158813d48f9009a"


def tool_configuration(html):
    """Recognize server-rendered table markup and recover only its display configuration."""
    if sha256(html.encode("utf-8")).hexdigest() == LEGACY_TOOL_DIGEST:
        return {"title": SUGGESTION, "filters": []}
    title = re.search(r"<title>([^<>]*)</title>", html)
    filters = re.search(r"const columnFilters = (\[[0-9, ]*\]);", html)
    if title and filters:
        configuration = {"title": unescape(title[1]), "filters": json.loads(filters[1])}
        if html in (render_tool(**configuration), render_tool(**configuration, styled=False)):
            return configuration
    raise ValueError("unrecognized table renderer")


def read_saved_table(entries):
    """Check the data contract shared by all recognized table renderers."""
    storage = json.loads(entries)
    if not isinstance(storage, list) or len(storage) != 1:
        raise ValueError("invalid saved table storage")
    key, value = storage[0]
    if key != TABLE_KEY:
        raise ValueError("invalid saved table key")
    table = json.loads(value)
    if not isinstance(table, dict) or not isinstance(table.get("filename"), str):
        raise ValueError("invalid saved table")
    columns, rows, done = table.get("columns"), table.get("rows"), table.get("done")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_TABLE_COLUMNS or any(
        not isinstance(column, dict) or not valid_label(column.get("name"), MAX_HEADER_LENGTH)
        or not isinstance(column.get("type"), str) or column["type"] not in COLUMN_TYPES
        for column in columns
    ):
        raise ValueError("invalid saved table columns")
    if not isinstance(rows, list) or any(
        not isinstance(row, list) or len(row) != len(columns) or not all(isinstance(cell, str) for cell in row)
        for row in rows
    ):
        raise ValueError("invalid saved table rows")
    if not isinstance(done, list) or len(done) != len(rows) or not all(type(status) is bool for status in done):
        raise ValueError("invalid saved table statuses")
    return table


def render_tool(title=SUGGESTION, filters=(), *, styled=True):
    """Render trusted markup. Records and field names stay in sandbox storage."""
    if not valid_label(title, MAX_HEADER_LENGTH):
        raise ValueError("invalid table title")
    if (not isinstance(filters, (tuple, list))
            or any(type(index) is not int or not 0 <= index < MAX_TABLE_COLUMNS for index in filters)
            or len(filters) != len(set(filters))):
        raise ValueError("invalid table filters")
    markup = (TOOL_HTML.replace("__TABLE_CELL_LIMIT__", str(MAX_CELL_LENGTH))
              .replace("__TABLE_COLUMN_FILTERS__", json.dumps(filters))
              .replace("__TABLE_TITLE__", escape(title)))
    if styled:
        return markup.replace("</style>", TABLE_VISUAL_STYLE + "</style>", 1)
    return markup


TABLE_VISUAL_STYLE = """
    body { padding: clamp(12px, 3vw, 28px); background: #f4f5f8; }
    h1 { font-size: clamp(22px, 4vw, 30px); line-height: 1.2; overflow-wrap: anywhere; }
    #table-source { color: #47546a; }
    #table-add-record, .table { background: #fff; border: 1px solid #d6dbe7; border-radius: 12px; }
    #table-add-record { padding: 12px; }
    #table-add-record summary { width: fit-content; font-weight: 650; color: #24499b; }
    input, select, button { max-width: 100%; padding: 6px 10px; border: 1px solid #a6afc0;
      border-radius: 6px; color: inherit; background: #fff; font: inherit; }
    #table-entry-form button { border-color: #24499b; background: #24499b; color: #fff; cursor: pointer; }
    :focus-visible { outline: 2px solid #24499b; outline-offset: 2px; }
"""


TOOL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TABLE_TITLE__</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; padding: 12px; color: #232638; background: #fafaff; font: 15px/1.5 system-ui, sans-serif; }
    h1 { margin: 0; font-size: 22px; }
    p { margin: 5px 0 12px; overflow-wrap: anywhere; }
    .controls { display: flex; flex-wrap: wrap; gap: 8px 16px; }
    label { display: grid; min-width: 0; gap: 4px; overflow-wrap: anywhere; }
    details { margin-block: 12px; }
    summary { cursor: pointer; }
    #table-entry-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 12em), 1fr));
      gap: 8px; margin-block: 8px; max-height: 16em; overflow: auto; }
    input, select { max-width: 100%; padding: 5px 8px; font: inherit; }
    .table { overflow: auto; }
    table { border-collapse: collapse; min-width: 100%; }
    th, td { padding: 7px 8px; border-bottom: 1px solid #dee1ed; text-align: left; vertical-align: top; }
    th { background: #f0f2fe; white-space: nowrap; }
    td { white-space: pre-wrap; overflow-wrap: anywhere; }
  </style>
</head>
<body>
  <main>
    <h1>__TABLE_TITLE__</h1>
    <p id="table-source"></p>
    <details id="table-add-record">
      <summary>新增记录</summary>
      <form id="table-entry-form">
        <div id="table-entry-fields"></div>
        <button type="submit">添加记录</button>
      </form>
    </details>
    <div class="controls">
      <label>筛选状态
        <select id="table-filter">
          <option value="all">全部</option>
          <option value="open">待完成</option>
          <option value="done">已完成</option>
        </select>
      </label>
      <label>查找记录 <input id="table-search" type="search"></label>
    </div>
    <div id="table-column-filters" class="controls"></div>
    <p id="table-count" role="status"></p>
    <p id="table-warning" role="alert" hidden></p>
    <div class="table" tabindex="0" role="region" aria-label="表格记录">
      <table id="table-records">
        <thead><tr id="table-columns"></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </main>
  <script>
    const storageKey = 'root.table.v1';
    const maxCellLength = __TABLE_CELL_LIMIT__;
    const columnFilters = __TABLE_COLUMN_FILTERS__;
    const filter = document.querySelector('#table-filter');
    const search = document.querySelector('#table-search');
    const rows = document.querySelector('#table-records tbody');
    const entryForm = document.querySelector('#table-entry-form');
    const entryFields = document.querySelector('#table-entry-fields');
    const filterFields = document.querySelector('#table-column-filters');
    const warning = document.querySelector('#table-warning');
    const report = text => {
      warning.textContent = text;
      warning.hidden = !text;
    };
    const cell = (tag, text) => {
      const node = document.createElement(tag);
      node.textContent = text;
      return node;
    };

    function showTable(saved) {
      let table = saved;
      const header = [...table.columns.map(column => cell('th', column.name)), cell('th', '跟进状态')];
      for (const node of header) node.scope = 'col';
      document.querySelector('#table-columns').replaceChildren(...header);
      document.querySelector('#table-source').textContent = '来源 · ' + table.filename;
      const inputs = table.columns.map(({ name }, index) => {
        const input = document.createElement('input');
        input.dataset.tableEntry = index;
        input.maxLength = 2 * maxCellLength;
        const label = cell('label', name);
        label.append(input);
        entryFields.append(label);
        return input;
      });
      const extraFilters = columnFilters.map(index => {
        const select = document.createElement('select');
        select.id = 'table-column-filter-' + index;
        select.dataset.tableColumnFilter = index;
        const label = cell('label', '按' + table.columns[index].name + '筛选');
        label.append(select);
        filterFields.append(label);
        select.addEventListener('change', render);
        return { index, select };
      });

      function updateFilterOptions() {
        for (const { index, select } of extraFilters) {
          const previous = select.value;
          const options = document.createDocumentFragment();
          options.append(new Option('全部', ''));
          for (const value of new Set(table.rows.map(row => row[index]))) {
            options.append(new Option(value || '（空值）', 'v' + value));
          }
          select.replaceChildren(options);
          select.value = previous;
        }
      }

      entryForm.addEventListener('submit', event => {
        event.preventDefault();
        const values = inputs.map(input => input.value);
        if (!values.some(value => value.trim())) {
          report('请至少填写一个字段。');
          inputs[0].focus();
          return;
        }
        const invalid = values.findIndex(value => [...value].length > maxCellLength || value.includes(String.fromCharCode(0)));
        if (invalid !== -1) {
          report('单元格不能超过 ' + maxCellLength + ' 个字符，也不能含空字符。');
          inputs[invalid].focus();
          return;
        }
        const next = { ...table, rows: [...table.rows, values], done: [...table.done, false] };
        try {
          localStorage.setItem(storageKey, JSON.stringify(next));
        } catch {
          report('记录暂时无法保存，输入已保留。');
          return;
        }
        table = next;
        entryForm.reset();
        report('');
        updateFilterOptions();
        render();
        inputs[0].focus();
      });

      function render() {
        rows.replaceChildren();
        const query = search.value.trim().toLocaleLowerCase();
        const state = filter.value;
        let visible = 0;
        table.rows.forEach((values, index) => {
          if (state !== 'all' && (state === 'done') !== table.done[index]) return;
          if (query && !values.some(value => value.toLocaleLowerCase().includes(query))) return;
          if (extraFilters.some(({ index, select }) => select.value && select.value !== 'v' + values[index])) return;
          visible++;
          const row = document.createElement('tr');
          for (const value of values) row.append(cell('td', value));
          const stateCell = document.createElement('td');
          const label = document.createElement('label');
          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox';
          checkbox.dataset.rowStatus = index;
          checkbox.checked = table.done[index];
          checkbox.setAttribute('aria-label', '第 ' + (index + 1) + ' 条记录已完成');
          const caption = cell('span', checkbox.checked ? '已完成' : '待完成');
          checkbox.addEventListener('change', () => {
            const done = table.done.slice();
            done[index] = checkbox.checked;
            const next = { ...table, done };
            try {
              localStorage.setItem(storageKey, JSON.stringify(next));
            } catch {
              checkbox.checked = table.done[index];
              report('状态暂时无法保存，请重试。');
              return;
            }
            table = next;
            report('');
            if (state !== 'all') {
              render();
              filter.focus();
            } else {
              caption.textContent = checkbox.checked ? '已完成' : '待完成';
            }
          });
          label.append(checkbox, caption);
          stateCell.append(label);
          row.append(stateCell);
          rows.append(row);
        });
        document.querySelector('#table-count').textContent = '显示 ' + visible + ' / ' + table.rows.length + ' 条记录';
      }
      filter.addEventListener('change', render);
      search.addEventListener('input', render);
      updateFilterOptions();
      render();
    }

    try {
      showTable(JSON.parse(localStorage.getItem(storageKey)));
    } catch {
      report('表格记录无法读取，请重新打开工具。');
      filter.disabled = search.disabled = true;
      document.querySelector('#table-add-record').hidden = true;
    }
  </script>
</body>
</html>"""
