'use strict';

const {spec, entry, filters, summary, export: exportConfig} = config;
const fields = new Map(spec.fields.map(field => [field.id, field]));
const storageKey = 'root-structured-records-v1';
let records = JSON.parse(localStorage.getItem(storageKey) || '[]');
let lastRecordId = records.reduce((maximum, row) => Math.max(maximum, Number(row.id) || 0), 0);
const el = (tag, text = '') => {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
};
const section = id => document.getElementById(id);
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
    input.max = '1000000000';
    input.step = '1';
  }
  if (field.type === 'text') input.maxLength = 300;
  return input;
}

function withLabel(text, input) {
  const label = el('label', text);
  label.append(input);
  return label;
}

function fieldValue(field, input) {
  const value = input.value.trim();
  if (!value && !field.required && !input.validity.badInput) return '';
  const message = '请检查' + field.label;
  if (!value || !input.validity.valid) throw new Error(message);
  if (field.type === 'integer') {
    if (!/^\d+$/.test(value) || Number(value) > 1_000_000_000) throw new Error(message);
    return Number(value);
  }
  if (field.type === 'date') {
    const date = new Date(value + 'T00:00:00Z');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(date.valueOf()) ||
        date.toISOString().slice(0, 10) !== value) throw new Error(message);
  }
  if (field.type === 'choice' && !field.options.includes(value)) throw new Error(message);
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
const error = el('p');
error.id = 'entry-error';
error.setAttribute('role', 'alert');
form.append(add);
section('entry').append(form, error);
form.addEventListener('submit', event => {
  event.preventDefault();
  error.textContent = '';
  try {
    const row = {id: String(lastRecordId + 1)};
    for (const [id, input] of inputs) row[id] = fieldValue(fields.get(id), input);
    if (entry.edit_choice) row[entry.edit_choice.field] = entry.edit_choice.initial;
    save([...records, row]);
    lastRecordId += 1;
    form.reset();
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
filterBar.addEventListener('input', render);
filterBar.addEventListener('change', render);
reset.addEventListener('click', () => {
  search.value = from.value = to.value = '';
  for (const select of exactControls.values()) select.value = '';
  render();
});

const table = el('table');
const head = table.createTHead().insertRow();
for (const id of entry.columns) head.append(el('th', fields.get(id).label));
const tbody = table.createTBody();
const empty = el('p');
empty.id = 'records-message';
section('records').append(empty, table);

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
section('export').append(exportButton, copy, csvOutput, copyResult);

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
            save(records.map(item => item.id === record.id ? {...item, [id]:select.value} : item));
            error.textContent = '';
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
  }
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
