"""Inspect supported CSV input and build a deterministic, isolated table tool."""

import csv
import io
import json
import re
from datetime import date

from accounts import ApiError, pack_entries


MAX_CSV_BYTES = 96 * 1024
MAX_ROWS = 200
MAX_COLUMNS = 16
MAX_HEADER_LENGTH = 80
MAX_CELL_LENGTH = 4096
MAX_FILENAME_LENGTH = 120
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
NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_DAY = re.compile(r"([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日")
LABEL_CONTROLS = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
# csv.reader(strict=True) still accepts a bare quote inside an unquoted field.
CSV_FIELD = re.compile(r'(?:"(?:[^"]|"")*"|[^",\r\n]*)(?:,|\r\n|\r|\n|\Z)')
BOOLEAN_VALUES = frozenset({"true", "false", "yes", "no", "是", "否"})
DONE_OPTIONS = frozenset({"完成", "已完成", "已读", "已关闭", "done", "completed", "closed"})
STATUS_OPTIONS = DONE_OPTIONS | {"未完成", "待完成", "待办", "未开始", "进行中", "未读", "open", "pending", "todo"}
STATUS_HEADERS = frozenset({"状态", "完成状态", "status"})


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
        or not filename.casefold().endswith(".csv")
        or "/" in filename
        or "\\" in filename
    ):
        raise ApiError(400, "csv_format", "请选择文件名不超过 120 字符的 .csv 文件，或试用示例。")


def header_names(values):
    if not 1 <= len(values) <= MAX_COLUMNS:
        raise ApiError(400, "csv_columns", "请保留 1–16 列后重新选择表格。")
    names = [value.strip(" \u3000") if isinstance(value, str) else "" for value in values]
    if not all(valid_label(name, MAX_HEADER_LENGTH) for name in names):
        raise ApiError(400, "csv_headers", "请为每列填写不超过 80 字符的单行表头。")
    if len({name.casefold() for name in names}) != len(names):
        raise ApiError(400, "csv_headers", "表头不能重名，请修改重复的字段名。")
    return names


def validate_rows(rows, width):
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, "csv_no_rows", "CSV 表头下至少需要一条记录。")
    if len(rows) > MAX_ROWS:
        raise ApiError(413, "csv_rows", "CSV 最多支持 200 条记录，请减少记录后重试。")
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) != width:
            raise ApiError(400, "csv_width", f"第 {number} 条记录的列数与表头不一致，请核对分隔符。")
        if any(not isinstance(cell, str) or len(cell) > MAX_CELL_LENGTH or "\0" in cell for cell in row):
            raise ApiError(400, "csv_cell", f"第 {number} 条记录含有无效单元格，请检查内容或将单元格缩短到 4096 字符以内。")


def is_date(value):
    try:
        if ISO_DATE.fullmatch(value):
            date.fromisoformat(value)
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


def inspect_table(data):
    if not isinstance(data, dict):
        raise ApiError(400, "csv_format", "请选择 CSV 文件，或试用示例。")
    source = data.get("source")
    if source == "example":
        filename, raw = EXAMPLE_FILENAME, EXAMPLE_CSV
    else:
        filename, raw = data.get("filename"), data.get("csv")
    validate_filename(source, filename)
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "csv_empty", "CSV 为空，请选择有表头和记录的文件。")
    try:
        byte_count = len(raw.encode("utf-8"))
    except UnicodeError:
        raise ApiError(400, "csv_encoding", "请选择用 UTF-8 编码保存的 CSV 文件。") from None
    if byte_count > MAX_CSV_BYTES:
        raise ApiError(413, "csv_size", "CSV 文件最多 96 KiB，请减少行列后重试。")
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
        names = header_names(header)
        rows = []
        for row in reader:
            if row:
                if len(rows) == MAX_ROWS:
                    raise ApiError(413, "csv_rows", "CSV 最多支持 200 条记录，请减少记录后重试。")
                rows.append(row)
    except csv.Error:
        raise ApiError(400, "csv_syntax", "无法解析 CSV，请检查逗号分隔和双引号配对。") from None
    validate_rows(rows, len(names))
    columns = [{"name": name, "type": infer_type(values)} for name, values in zip(names, zip(*rows))]
    return {"source": source, "filename": filename, "columns": columns, "rows": rows, "suggestion": SUGGESTION}


def build_table_tool(table):
    if not isinstance(table, dict):
        raise ApiError(400, "invalid_table", "请先选择表格并确认识别结果。")
    source, filename = table.get("source"), table.get("filename")
    validate_filename(source, filename)
    columns = table.get("columns")
    if not isinstance(columns, list) or any(
        not isinstance(column, dict) or not isinstance(column.get("type"), str) or column["type"] not in COLUMN_TYPES
        for column in columns
    ):
        raise ApiError(400, "csv_columns", "字段类型无效，请重新确认识别结果。")
    names = header_names([column.get("name") for column in columns])
    rows = table.get("rows")
    validate_rows(rows, len(names))
    columns = [{"name": name, "type": column["type"]} for name, column in zip(names, columns)]
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
    entries = [[TABLE_KEY, json.dumps(seed, ensure_ascii=False, separators=(",", ":"))]]
    try:
        pack_entries(entries)
    except UnicodeError:
        raise ApiError(400, "csv_encoding", "确认结果含有无效字符，请重新选择 UTF-8 CSV。") from None
    except ApiError as error:
        if error.code != "tool_quota":
            raise
        raise ApiError(413, "csv_size", "确认后的表格超过工具存储空间，请减少行列后重试。") from None
    return SUGGESTION, TOOL_HTML, entries


TOOL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>交付跟进板</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; padding: 12px; color: #232638; background: #fafaff; font: 15px/1.5 system-ui, sans-serif; }
    h1 { margin: 0; font-size: 22px; }
    p { margin: 5px 0 12px; overflow-wrap: anywhere; }
    .controls { display: flex; flex-wrap: wrap; gap: 8px 16px; }
    label { display: grid; gap: 4px; }
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
    <h1>交付跟进板</h1>
    <p id="table-source"></p>
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
    const filter = document.querySelector('#table-filter');
    const search = document.querySelector('#table-search');
    const rows = document.querySelector('#table-records tbody');
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

      function render() {
        rows.replaceChildren();
        const query = search.value.trim().toLocaleLowerCase();
        const state = filter.value;
        let visible = 0;
        table.rows.forEach((values, index) => {
          if (state !== 'all' && (state === 'done') !== table.done[index]) return;
          if (query && !values.some(value => value.toLocaleLowerCase().includes(query))) return;
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
      render();
    }

    try {
      showTable(JSON.parse(localStorage.getItem(storageKey)));
    } catch {
      report('表格记录无法读取，请重新打开工具。');
      filter.disabled = search.disabled = true;
    }
  </script>
</body>
</html>"""
