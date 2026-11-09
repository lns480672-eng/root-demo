'use strict';

const {spec, entry, filters, summary, export: exportConfig} = config;
const fields = new Map(spec.fields.map(field => [field.id, field]));
const storageKey = 'root-structured-records-v1';
const maxNumber = 1_000_000_000;
const maxText = 300;
let records = JSON.parse(localStorage.getItem(storageKey) || '[]');
let lastRecordId = records.reduce((maximum, row) => Math.max(maximum, Number(row.id) || 0), 0);
let editingId = null;
let deletingId = null;
const el = (tag, text = '') => {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
};
const section = id => document.getElementById(id);
const button = (text, action) => {
  const node = el('button', text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
};
section('heading').textContent = spec.title;
document.title = spec.title;

function options(select, values, placeholder) {
  select.replaceChildren();
  if (placeholder !== undefined) select.add(new Option(placeholder, ''));
  for (const value of values) select.add(new Option(value, value));
}

function control(field) {
  if (field.type === 'choice') {
    const select = el('select');
    options(select, field.options, '请选择');
    return select;
  }
  const input = el('input');
  input.type = {text: 'text', integer: 'number', date: 'date'}[field.type];
  if (field.type === 'integer') {
    input.min = '0';
    input.max = String(maxNumber);
    input.step = '1';
  }
  if (field.type === 'text') input.maxLength = maxText;
  return input;
}

function withLabel(text, input) {
  const label = el('label', text);
  label.append(input);
  return label;
}

function validValue(field, value) {
  if (value === '') return !field.required;
  switch (field.type) {
    case 'integer':
      return Number.isInteger(value) && 0 <= value && value <= maxNumber;
    case 'text':
      return typeof value === 'string' && value.length <= maxText && Boolean(value.trim());
    case 'date': {
      if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
      const date = new Date(value + 'T00:00:00Z');
      return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === value;
    }
    case 'choice':
      return field.options.includes(value);
    default:
      return false;
  }
}

function fieldValue(field, input) {
  const raw = input.value.trim();
  const value = field.type === 'integer' && /^\d+$/.test(raw) ? Number(raw) : raw;
  if (!input.validity.valid || !validValue(field, value)) throw new Error('请检查' + field.label);
  return value;
}

function save(next) {
  localStorage.setItem(storageKey, JSON.stringify(next));
  records = next;
  render();
}

const form = el('form');
form.noValidate = true;
const inputs = new Map();
for (const id of entry.form) {
  const field = fields.get(id);
  const input = control(field);
  input.id = 'field-' + id;
  input.dataset.field = id;
  input.required = field.required;
  form.append(withLabel(field.label, input));
  inputs.set(id, input);
}
const add = el('button', '新增记录');
add.id = 'add-record';
add.type = 'submit';
const cancelEdit = button('取消编辑', () => {
  resetEditor();
  error.textContent = '';
});
cancelEdit.id = 'cancel-edit';
cancelEdit.hidden = true;
const error = el('p');
error.id = 'entry-error';
error.setAttribute('role', 'alert');
form.append(add, cancelEdit);
section('entry').append(form, error);

function resetEditor() {
  form.reset();
  editingId = null;
  add.textContent = '新增记录';
  cancelEdit.hidden = true;
}

function startEditing(record) {
  editingId = record.id;
  for (const [id, input] of inputs) input.value = record[id];
  add.textContent = '保存修改';
  cancelEdit.hidden = false;
  error.textContent = '';
  inputs.values().next().value.focus();
}

form.addEventListener('submit', event => {
  event.preventDefault();
  error.textContent = '';
  try {
    const current = records.find(row => row.id === editingId);
    if (!current && lastRecordId >= Number.MAX_SAFE_INTEGER) throw new Error('记录编号已达上限。');
    const row = current ? {...current} : {id: String(lastRecordId + 1)};
    for (const [id, input] of inputs) row[id] = fieldValue(fields.get(id), input);
    if (!current && entry.edit_choice) row[entry.edit_choice.field] = entry.edit_choice.initial;
    save(current ? records.map(item => item.id === current.id ? row : item) : [...records, row]);
    if (!current) lastRecordId += 1;
    resetEditor();
  } catch (err) {
    error.textContent = err.message;
  }
});

const filterBar = el('div');
filterBar.className = 'filter-bar';
const search = el('input');
search.id = 'search';
if (filters.search.length) filterBar.append(withLabel('关键词', search));
const exactControls = new Map();
for (const id of filters.exact) {
  const select = el('select');
  select.id = 'filter-' + id;
  select.dataset.filter = id;
  filterBar.append(withLabel(fields.get(id).label, select));
  exactControls.set(id, select);
}
const from = el('input');
const to = el('input');
from.type = to.type = 'date';
from.id = 'filter-from';
to.id = 'filter-to';
if (filters.date) filterBar.append(withLabel('起始日期', from), withLabel('结束日期', to));
const reset = el('button', '清空筛选');
reset.id = 'filter-reset';
reset.type = 'button';
filterBar.append(reset);
section('filters').append(filterBar);
section('filters').hidden = !filters.search.length && !filters.exact.length && !filters.date;
filterBar.addEventListener('input', render);
filterBar.addEventListener('change', render);
reset.addEventListener('click', () => {
  search.value = from.value = to.value = '';
  for (const select of exactControls.values()) select.value = '';
  render();
});

const board = entry.board ? el('div') : null;
const boardColumns = new Map();
const table = board ? null : el('table');
let tbody;
if (board) {
  board.id = 'record-board';
  for (const stage of fields.get(entry.board).options) {
    const column = el('section');
    column.dataset.boardColumn = stage;
    const cards = el('div');
    column.append(el('h3', stage), cards);
    board.append(column);
    boardColumns.set(stage, cards);
  }
} else {
  const head = table.createTHead().insertRow();
  for (const id of entry.columns) head.append(el('th', fields.get(id).label));
  if (entry.allow_edit || entry.allow_delete) head.append(el('th', '操作'));
  tbody = table.createTBody();
}
const empty = el('p');
empty.id = 'records-message';
section('records').append(empty, board || table);

const count = el('p');
count.id = 'report-count';
const sum = el('p');
sum.id = 'report-sum';
const groupTitle = el('h3', summary.group?.label);
const groups = el('ul');
groups.id = 'report-groups';
if (summary.count) section('summary').append(count);
if (summary.sum) section('summary').append(sum);
if (summary.group) section('summary').append(groupTitle, groups);
section('summary').hidden = !summary.count && !summary.sum && !summary.group;

const exportButton = el('button', exportConfig.label);
exportButton.id = 'export-csv';
exportButton.type = 'button';
const copy = el('button', '复制 CSV');
copy.type = 'button';
const csvOutput = el('textarea');
csvOutput.id = 'csv-output';
csvOutput.readOnly = true;
csvOutput.rows = 4;
const copyResult = el('p');
copyResult.setAttribute('role', 'status');
if (exportConfig.columns.length) section('export').append(exportButton, copy, csvOutput, copyResult);
section('export').hidden = !exportConfig.columns.length && !entry.backup;
if (entry.backup) installBackup();

function installBackup() {
  const format = 'root-structured-backup';
  const version = 1;
  const schema = spec.fields.map(({id, type, required, options: values}) => [id, type, required, values]);
  const schemaKey = JSON.stringify(schema);
  const keys = ['id', ...fields.keys()];
  const invalid = '备份文件无效或不适用于当前工具。';
  let selection = 0;
  let pending = null;

  const status = el('p');
  status.id = 'backup-status';
  status.setAttribute('role', 'status');
  const download = button('导出为文件', event => {
    if (!event.isTrusted) return;
    const content = JSON.stringify({format, version, schema, records});
    const url = URL.createObjectURL(new Blob([content], {type:'application/json'}));
    const link = el('a');
    link.href = url;
    link.download = 'root-tool-backup.json';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
    status.textContent = '已请求下载全部 ' + records.length + ' 条记录的备份。';
  });
  download.id = 'export-backup';
  const fileInput = el('input');
  fileInput.id = 'import-backup';
  fileInput.type = 'file';
  fileInput.accept = '.json,application/json';
  const confirm = button('确认导入并替换', () => {
    if (!pending) return;
    try {
      save(pending);
      lastRecordId = records.reduce((maximum, row) => Math.max(maximum, Number(row.id)), 0);
      deletingId = null;
      resetEditor();
      render();
      clearPending('已恢复 ' + records.length + ' 条记录。');
    } catch (err) {
      status.textContent = '导入失败，已有记录未改变。' + err.message;
    }
  });
  confirm.id = 'confirm-import';
  const cancel = button('取消导入', () => {
    selection += 1;
    clearPending('已取消导入，已有记录未改变。');
  });
  cancel.id = 'cancel-import';

  function clearPending(message) {
    pending = null;
    confirm.hidden = cancel.hidden = true;
    status.textContent = message;
  }

  function parse(content) {
    let value;
    try {
      value = JSON.parse(content);
    } catch {
      throw new Error(invalid);
    }
    if (!value || typeof value !== 'object' || Object.keys(value).length !== 4 ||
        value.format !== format || value.version !== version || JSON.stringify(value.schema) !== schemaKey ||
        !Array.isArray(value.records)) throw new Error(invalid);
    const ids = new Set();
    for (const record of value.records) {
      if (!record || typeof record !== 'object' || Array.isArray(record) ||
          Object.keys(record).length !== keys.length || !keys.every(key => Object.hasOwn(record, key)) ||
          typeof record.id !== 'string' || !/^[1-9]\d{0,15}$/.test(record.id) ||
          !Number.isSafeInteger(Number(record.id)) || ids.has(record.id)) throw new Error(invalid);
      for (const [id, field] of fields) {
        if (!validValue(field, record[id])) throw new Error(invalid);
      }
      if (board && !boardColumns.has(record[entry.board])) throw new Error(invalid);
      ids.add(record.id);
    }
    return value.records;
  }

  fileInput.addEventListener('change', async () => {
    const sequence = ++selection;
    const file = fileInput.files[0];
    fileInput.value = '';
    clearPending('');
    if (!file) return;
    try {
      if (!file.size || file.size > 512 * 1024) throw new Error('备份文件大小超出限制。');
      const content = await file.text();
      if (sequence !== selection) return;
      pending = parse(content);
      status.textContent = '备份含 ' + pending.length + ' 条记录。确认后会替换当前工具的全部记录。';
      confirm.hidden = cancel.hidden = false;
    } catch (err) {
      if (sequence === selection) clearPending('导入失败，已有记录未改变。' + err.message);
    }
  });
  clearPending('');
  section('export').append(download, withLabel('导入文件', fileInput), status, confirm, cancel);
}

function selectedRecords() {
  const query = search.value.trim().toLowerCase();
  const visible = records.filter(row =>
    (!query || filters.search.some(id => String(row[id] ?? '').toLowerCase().includes(query))) &&
    [...exactControls].every(([id, select]) => !select.value || String(row[id] ?? '') === select.value) &&
    (!filters.date || ((!from.value || row[filters.date] >= from.value) &&
      (!to.value || row[filters.date] <= to.value))));
  if (entry.sort) {
    const {field, direction} = entry.sort;
    const sign = direction === 'desc' ? -1 : 1;
    visible.sort((a, b) => sign * ((a[field] > b[field]) - (a[field] < b[field])));
  }
  return visible;
}

function writeCsv() {
  const escape = value => {
    const text = String(value ?? '');
    return /[",\r\n]/.test(text) ? '"' + text.replaceAll('"', '""') + '"' : text;
  };
  const columns = exportConfig.columns;
  const rows = [columns.map(id => fields.get(id).label),
    ...selectedRecords().map(row => columns.map(id => row[id]))];
  csvOutput.value = rows.map(row => row.map(escape).join(',')).join('\r\n');
}
exportButton.addEventListener('click', writeCsv);
copy.addEventListener('click', async () => {
  writeCsv();
  csvOutput.select();
  try {
    await navigator.clipboard.writeText(csvOutput.value);
    copyResult.textContent = '已复制';
  } catch {
    copyResult.textContent = 'CSV 已选中，可以手动复制';
  }
});

function actions(record) {
  const container = el('div');
  container.className = 'record-actions';
  if (entry.allow_edit) {
    const edit = button('编辑', () => startEditing(record));
    edit.dataset.editRecord = record.id;
    container.append(edit);
  }
  if (!entry.allow_delete) return container;
  if (deletingId === record.id) {
    const confirm = button('确认删除', () => {
      try {
        deletingId = null;
        save(records.filter(row => row.id !== record.id));
        if (editingId === record.id) resetEditor();
        error.textContent = '';
      } catch (err) {
        deletingId = record.id;
        error.textContent = err.message;
      }
    });
    confirm.dataset.confirmDelete = record.id;
    const cancel = button('取消删除', () => {
      deletingId = null;
      render();
    });
    container.append(confirm, cancel);
  } else {
    const remove = button('删除', () => {
      deletingId = record.id;
      render();
    });
    remove.dataset.deleteRecord = record.id;
    container.append(remove);
  }
  return container;
}

function updateStage(record, value) {
  save(records.map(item => item.id === record.id ? {...item, [entry.edit_choice.field]:value} : item));
  error.textContent = '';
}

function renderBoard(visible) {
  for (const column of boardColumns.values()) column.replaceChildren();
  for (const record of visible) {
    const current = record[entry.board];
    const card = el('article');
    card.dataset.record = record.id;
    const details = el('dl');
    for (const id of entry.columns) {
      if (id !== entry.board) details.append(el('dt', fields.get(id).label), el('dd', record[id] ?? ''));
    }
    const choices = el('div');
    choices.className = 'record-actions';
    for (const value of boardColumns.keys()) {
      const move = button(value, () => {
        try {
          updateStage(record, value);
        } catch (err) {
          error.textContent = err.message;
        }
      });
      move.dataset.moveStage = value;
      move.setAttribute('aria-pressed', String(value === current));
      move.disabled = value === current;
      choices.append(move);
    }
    card.append(details, choices);
    if (entry.allow_edit || entry.allow_delete) card.append(actions(record));
    boardColumns.get(current).append(card);
  }
}

function renderTable(visible) {
  tbody.replaceChildren();
  for (const record of visible) {
    const row = tbody.insertRow();
    row.dataset.record = record.id;
    for (const id of entry.columns) {
      const cell = row.insertCell();
      if (id === entry.edit_choice?.field) {
        const select = control(fields.get(id));
        select.remove(0);
        select.value = record[id];
        select.dataset.recordStatus = record.id;
        select.addEventListener('change', () => {
          try {
            updateStage(record, select.value);
          } catch (err) {
            select.value = record[id];
            error.textContent = err.message;
          }
        });
        cell.append(select);
      } else {
        cell.textContent = record[id] ?? '';
      }
    }
    if (entry.allow_edit || entry.allow_delete) row.insertCell().append(actions(record));
  }
}

function render() {
  for (const [id, select] of exactControls) {
    const current = select.value;
    const field = fields.get(id);
    const values = field.type === 'choice' ? field.options :
      [...new Set(records.map(row => String(row[id] ?? '')).filter(Boolean))].sort();
    options(select, values, '全部');
    select.value = values.includes(current) ? current : '';
  }
  const visible = selectedRecords();
  empty.textContent = records.length ? (visible.length ? '' : '没有匹配记录') : '暂无记录';
  if (board) renderBoard(visible);
  else renderTable(visible);
  if (summary.count) count.textContent = summary.count + '：' + visible.length;
  if (summary.sum) {
    const value = row => row[summary.sum.field] || 0;
    sum.textContent = summary.sum.label + '：' + visible.reduce((total, row) => total + value(row), 0);
    if (summary.group) {
      const totals = new Map();
      for (const row of visible) {
        const group = row[summary.group.field] ?? '';
        totals.set(group, (totals.get(group) || 0) + value(row));
      }
      groups.replaceChildren(...[...totals].map(([group, total]) => el('li', group + '：' + total)));
    }
  }
  if (csvOutput.value) writeCsv();
}
render();
