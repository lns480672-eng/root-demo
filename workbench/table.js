'use strict';

function setupTableImport({ request, generate, onExit }) {
  const panel = document.getElementById('table-panel');
  const maxFileBytes = 96 * 1024;
  const previewLimit = 6;
  const fieldTypes = new Map([
    ['text', '文本'], ['number', '数字'], ['date', '日期'], ['boolean', '布尔'], ['option', '选项'],
  ]);
  panel.setAttribute('aria-labelledby', 'table-heading');
  panel.innerHTML = `
    <p class="auth-eyebrow">从表格生成</p>
    <div id="table-select">
      <h3 id="table-heading" tabindex="-1">选择带表头的 CSV</h3>
      <div id="table-drop" class="table-drop">
        <label for="table-file">选择 CSV 文件，或拖到这里</label>
        <input id="table-file" type="file" accept=".csv,text/csv" aria-describedby="table-file-help">
        <p id="table-file-help" class="auth-muted">UTF-8 编码，最大 96 KiB。第一行为字段名。</p>
      </div>
      <div class="entry-actions">
        <button id="table-exit" type="button">返回直接开发</button>
        <button id="table-example" type="button">试用示例表格</button>
      </div>
    </div>
    <div id="table-review" hidden>
      <h3 id="table-review-title" tabindex="-1">确认识别结果</h3>
      <div class="table-summary">
        <p id="table-source-kind" class="auth-eyebrow"></p>
        <strong id="table-source"></strong>
        <p id="table-count" class="auth-muted"></p>
      </div>
      <fieldset id="table-fields">
        <legend>识别字段</legend>
      </fieldset>
      <div class="table-records-scroll" tabindex="0" role="region" aria-label="识别出的记录">
        <table id="table-records">
          <caption id="table-preview-note"></caption>
          <thead><tr></tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <div class="table-summary">
        <p class="auth-eyebrow">输出 · 建议生成</p>
        <strong id="table-suggestion"></strong>
        <p class="auth-muted">生成后保留确认的记录，可筛选并更新完成状态。</p>
      </div>
      <div class="entry-actions">
        <button id="table-reselect" type="button">返回选择表格</button>
        <button id="table-confirm" type="button" class="elys-primary">确认并生成</button>
      </div>
    </div>
    <p id="table-status" class="auth-muted" role="status" hidden></p>
    <p id="table-error" class="elys-error" role="alert" hidden></p>
  `;

  const find = selector => panel.querySelector(selector);
  const selection = find('#table-select');
  const review = find('#table-review');
  const fileInput = find('#table-file');
  const fields = find('#table-fields');
  const records = find('#table-records');
  const confirm = find('#table-confirm');
  const reselect = find('#table-reselect');
  const status = find('#table-status');
  const alert = find('#table-error');
  let inspection = 0;
  let table = null;
  let generating = false;

  function say(element, message = '') {
    element.textContent = message;
    element.hidden = !message;
  }

  function showSelection() {
    table = null;
    selection.hidden = false;
    review.hidden = true;
    panel.setAttribute('aria-labelledby', 'table-heading');
    fileInput.value = '';
  }

  function setBusy(busy) {
    generating = busy;
    fields.disabled = confirm.disabled = reselect.disabled = busy;
    confirm.textContent = busy ? '正在生成…' : '确认并生成';
    review.setAttribute('aria-busy', String(busy));
  }

  function textCell(tag, value) {
    const cell = document.createElement(tag);
    cell.textContent = value;
    return cell;
  }

  function renderReview() {
    find('#table-source-kind').textContent = table.source === 'example' ? '输入 · 示例表格' : '输入 · 上传的表格';
    find('#table-source').textContent = table.filename;
    find('#table-count').textContent = `识别到 ${table.columns.length} 个字段、${table.rows.length} 条记录`;
    find('#table-suggestion').textContent = table.suggestion;
    find('#table-preview-note').textContent = table.rows.length > previewLimit
      ? `预览前 ${previewLimit} 条记录，生成时保留全部记录。`
      : '识别出的记录';
    const header = records.querySelector('thead tr');
    header.replaceChildren(...table.columns.map(column => textCell('th', column.name)));
    fields.replaceChildren(fields.querySelector('legend'));
    table.columns.forEach((column, index) => {
      const row = document.createElement('div');
      row.className = 'table-field';
      const name = document.createElement('input');
      name.value = column.name;
      name.maxLength = 160;
      name.setAttribute('aria-label', `字段 ${index + 1} 名称`);
      name.addEventListener('input', () => { header.children[index].textContent = name.value.trim(); });
      const type = document.createElement('select');
      type.setAttribute('aria-label', `字段 ${index + 1} 类型`);
      for (const [value, label] of fieldTypes) type.add(new Option(label, value));
      type.value = column.type;
      row.append(name, type);
      fields.append(row);
    });
    records.querySelector('tbody').replaceChildren(...table.rows.slice(0, previewLimit).map(values => {
      const row = document.createElement('tr');
      row.append(...values.map(value => textCell('td', value)));
      return row;
    }));
    selection.hidden = true;
    review.hidden = false;
    panel.setAttribute('aria-labelledby', 'table-review-title');
    find('#table-review-title').focus();
  }

  async function inspect(file) {
    if (generating) return;
    const current = ++inspection;
    say(alert);
    say(status, '正在识别表格…');
    try {
      let payload = { source: 'example' };
      if (file) {
        if (!/\.csv$/i.test(file.name)) throw new Error('请选择带表头的 CSV 文件。');
        if (file.size > maxFileBytes) throw new Error('CSV 文件超过 96 KiB，请缩小后重新选择。');
        let csv;
        try {
          csv = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
        } catch {
          throw new Error('无法读取文件，请保存为 UTF-8 编码的 CSV 后重新选择。');
        }
        payload = { source: 'upload', filename: file.name, csv };
      }
      if (current !== inspection) return;
      const result = await request('/api/table/inspect', payload);
      if (current !== inspection) return;
      table = result;
      renderReview();
    } catch (error) {
      if (current === inspection) say(alert, error.message || '识别失败，请重新选择表格。');
    } finally {
      if (current === inspection) say(status);
    }
  }

  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    fileInput.value = '';
    if (file) inspect(file);
  });
  const drop = find('#table-drop');
  drop.addEventListener('dragover', event => { event.preventDefault(); });
  drop.addEventListener('drop', event => {
    event.preventDefault();
    const files = event.dataTransfer.files;
    if (files.length > 1) {
      inspection++;
      say(status);
      say(alert, '请一次选择一个 CSV 文件。');
    } else if (files.length === 1) {
      inspect(files[0]);
    }
  });
  find('#table-example').addEventListener('click', () => inspect());
  find('#table-exit').addEventListener('click', onExit);
  reselect.addEventListener('click', () => {
    inspection++;
    showSelection();
    say(status);
    say(alert);
    fileInput.focus();
  });
  confirm.addEventListener('click', async () => {
    if (generating || !table) return;
    const columns = [...fields.querySelectorAll('.table-field')].map(row => ({
      name: row.querySelector('input').value.trim(),
      type: row.querySelector('select').value,
    }));
    if (columns.some(column => !column.name || [...column.name].length > 80)) {
      say(alert, '字段名须为 1 至 80 个字符，请修改后再生成。');
      return;
    }
    if (new Set(columns.map(column => column.name)).size !== columns.length) {
      say(alert, '字段名不能重复，请修改后再生成。');
      return;
    }
    say(alert);
    setBusy(true);
    try {
      await generate({ ...table, columns });
      showSelection();
    } catch (error) {
      say(alert, error.message || '生成失败，已保留表格，可重试。');
    } finally {
      setBusy(false);
    }
  });

  return {
    open() {
      panel.hidden = false;
      find(table ? '#table-review-title' : '#table-heading').focus();
    },
    close() {
      inspection++;
      say(status);
      panel.hidden = true;
    },
  };
}
