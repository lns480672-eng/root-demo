import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

export async function verifyCsv({ evaluate, click, until, cdp, activeTool, library, stored,
  selectHistory, screenshot, stage }) {
  const visible = selector => evaluate(`Boolean(document.querySelector(${JSON.stringify(selector)})?.checkVisibility())`);
  const previewRows = context => evaluate(`[...document.querySelectorAll('#table-records tbody tr')].map(row =>
    [...row.querySelectorAll('td')].map(cell => cell.textContent))`, context);
  const savedTable = async id => {
    const { entries } = await stored(id);
    const entry = entries.find(([key]) => key === 'root.table.v1');
    return entry && JSON.parse(entry[1]);
  };
  const chooseCsv = (filename, text) => evaluate(`(() => {
    const input = document.querySelector('#table-file');
    const transfer = new DataTransfer();
    transfer.items.add(new File([${JSON.stringify(text)}], ${JSON.stringify(filename)}, {type: 'text/csv'}));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  const chooseXlsx = (filename, base64) => evaluate(`(() => {
    const input = document.querySelector('#table-file');
    const transfer = new DataTransfer();
    const bytes = Uint8Array.from(atob(${JSON.stringify(base64)}), char => char.charCodeAt(0));
    transfer.items.add(new File([bytes], ${JSON.stringify(filename)}, {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  const selectFilter = (value, context) => evaluate(`(() => {
    const filter = document.querySelector('#table-filter');
    filter.value = ${JSON.stringify(value)};
    filter.dispatchEvent(new Event('change', {bubbles: true}));
  })()`, context);
  const originalTool = await activeTool();
  const originalLibrary = await library();

  await evaluate(`(() => {
    const fetchOriginal = window.fetch;
    window.testCsvGeneratePosts = [];
    window.fetch = (url, options) => {
      if (options?.method === 'POST' && String(url).endsWith('/api/table/generate')) {
        window.testCsvGeneratePosts.push(JSON.parse(options.body));
      }
      return fetchOriginal(url, options);
    };
  })()`);

  stage('AC-010 malformed CSV preserves the current preview and tool library');
  await click('#new-tool');
  await click('#table-entry');
  await chooseCsv('错误列数.csv', '事项,地点\n只有一列\n');
  await until(() => visible('#table-error'));
  assert.equal(await visible('#table-file'), true);
  assert.equal((await activeTool()).id, originalTool.id);
  assert.deepEqual((await library()).items, originalLibrary.items);
  assert.deepEqual(await evaluate('window.testCsvGeneratePosts'), []);

  stage('AC-010 example source is reviewed before creating an isolated tool');
  await click('#table-example');
  await until(() => visible('#table-review'));
  assert.match(await evaluate('document.querySelector("#table-source").textContent'), /交付跟进表\.csv$/);
  assert.match(await evaluate('document.querySelector("#table-source-kind").textContent'), /示例表格/);
  assert.match(await evaluate('document.querySelector("#table-count").textContent'), /3 条记录/);
  assert.equal(await evaluate('document.querySelector("#table-suggestion").textContent'), '交付跟进板');
  const exampleRows = await previewRows();
  assert.equal(exampleRows.length, 3);
  assert.deepEqual((await library()).items, originalLibrary.items);
  assert.deepEqual(await evaluate('window.testCsvGeneratePosts'), []);
  await screenshot('csv-example-review');
  await click('#table-confirm');
  const exampleTool = await activeTool(originalTool.id);
  await until(async () => (await library()).items.some(item => item.id === exampleTool.id));
  assert.deepEqual((await savedTable(exampleTool.id)).rows, exampleRows);
  assert.match(await evaluate('document.querySelector("#table-source").textContent', exampleTool.context), /交付跟进表\.csv$/);
  assert.equal(await evaluate('window.testCsvGeneratePosts.length'), 1);

  stage('AC-010 edited CSV metadata preserves quoted and zero-prefixed original values');
  await click('#new-tool');
  await click('#table-entry');
  const filename = '活动分工.csv';
  const uploadedRows = [
    ['清点工具', '0007', '工程,复核', '待完成'],
    ['寄送样本', '0003', '<img src=x onerror=alert(1)>', '已完成'],
  ];
  await chooseCsv(filename, '事项,编号,备注,状态\r\n清点工具,0007,"工程,复核",待完成\r\n寄送样本,0003,<img src=x onerror=alert(1)>,已完成\r\n');
  await until(() => visible('#table-review'));
  assert.equal(await evaluate('document.querySelector("#table-source").textContent'), filename);
  assert.match(await evaluate('document.querySelector("#table-source-kind").textContent'), /上传/);
  assert.deepEqual(await previewRows(), uploadedRows);
  assert.equal(await evaluate('document.querySelector("#table-records img") === null'), true);
  assert.equal((await library()).items.length, originalLibrary.items.length + 1);
  assert.equal(await evaluate('window.testCsvGeneratePosts.length'), 1);
  await evaluate(`(() => {
    const field = document.querySelector('#table-fields input');
    field.value = '交付事项';
    field.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('#table-fields select').value = 'text';
  })()`);
  assert.equal(await evaluate('document.querySelector("#table-records th").textContent'), '交付事项');
  await click('#table-confirm');
  const uploadedTool = await activeTool(exampleTool.id);
  await until(async () => (await library()).items.some(item => item.id === uploadedTool.id));
  assert.equal(await evaluate('window.testCsvGeneratePosts.length'), 2);
  assert.equal(await evaluate('document.querySelector("#table-source").textContent', uploadedTool.context), '来源 · ' + filename);
  assert.deepEqual((await previewRows(uploadedTool.context)).map(row => row.slice(0, 4)), uploadedRows);
  assert.equal(await evaluate('document.querySelector("#table-records img") === null', uploadedTool.context), true);
  assert.deepEqual((await savedTable(uploadedTool.id)).columns[0], { name: '交付事项', type: 'text' });

  stage('AC-010 filter and status update persist through refresh and history');
  await selectFilter('open', uploadedTool.context);
  assert.deepEqual((await previewRows(uploadedTool.context)).map(row => row[0]), ['清点工具']);
  const previousRevision = (await stored(uploadedTool.id)).revision;
  await click('[data-row-status="0"]', uploadedTool.context);
  await until(async () => (await savedTable(uploadedTool.id)).done[0] === true &&
    (await stored(uploadedTool.id)).revision > previousRevision);
  await selectFilter('done', uploadedTool.context);
  assert.deepEqual((await previewRows(uploadedTool.context)).map(row => row[0]), ['清点工具', '寄送样本']);
  await screenshot('csv-uploaded-tool');

  stage('CHG-029 adds a record in the imported tool without losing the original rows or status');
  const addedRow = ['新建跟进事项', '0005', '<img src=x onerror=alert(1)>', ''];
  assert.equal(await evaluate('Boolean(document.querySelector("#table-add-record > summary"))', uploadedTool.context),
    true, '导入工具需要提供新增记录入口');
  await click('#table-add-record > summary', uploadedTool.context);
  await click('#table-entry-form button[type="submit"]', uploadedTool.context);
  assert.match(await evaluate('document.querySelector("#table-warning").textContent', uploadedTool.context), /至少填写/);
  await evaluate(`(() => {
    const inputs = document.querySelectorAll('#table-entry-form input[data-table-entry]');
    ${JSON.stringify(addedRow)}.forEach((value, index) => { inputs[index].value = value; });
  })()`, uploadedTool.context);
  await click('#table-entry-form button[type="submit"]', uploadedTool.context);
  const rowsWithNewRecord = [...uploadedRows, addedRow];
  await until(async () => (await savedTable(uploadedTool.id)).rows.length === rowsWithNewRecord.length);
  assert.deepEqual((await savedTable(uploadedTool.id)).rows, rowsWithNewRecord);
  assert.deepEqual((await savedTable(uploadedTool.id)).done, [true, true, false]);
  await selectFilter('open', uploadedTool.context);
  assert.deepEqual((await previewRows(uploadedTool.context)).map(row => row.slice(0, 4)), [addedRow]);
  assert.equal(await evaluate('document.querySelector("#table-records img") === null', uploadedTool.context), true);
  await screenshot('csv-added-record');
  const previousNavigation = await evaluate('performance.timeOrigin');
  await cdp('Page.reload', { ignoreCache: true });
  await until(() => evaluate(`performance.timeOrigin !== ${previousNavigation} && document.documentElement.dataset.ready === 'true'`));
  const reloaded = await activeTool();
  assert.equal(reloaded.id, uploadedTool.id);
  assert.notEqual(reloaded.loader, uploadedTool.loader);
  assert.equal(await evaluate('document.querySelector(\'[data-row-status="0"]\').checked', reloaded.context), true);
  assert.deepEqual((await savedTable(reloaded.id)).rows, rowsWithNewRecord);
  await selectHistory(originalTool.id);
  const reopened = await selectHistory(uploadedTool.id);
  assert.equal(reopened.id, uploadedTool.id);
  assert.deepEqual((await previewRows(reopened.context)).map(row => row.slice(0, 4)), rowsWithNewRecord);
  assert.equal((await savedTable(reopened.id)).done[0], true);

  stage('AC-010 XLSX reads the first sheet and omits the excluded column from all saved rows');
  const xlsx = execFileSync('python3', ['-B', '-c',
    'import sys; sys.path.insert(0, "tests"); from test_table_import import sample_xlsx; print(sample_xlsx())'],
  {encoding: 'utf8'}).trim();
  await click('#new-tool');
  await click('#table-entry');
  await until(() => evaluate('document.querySelector("#table-file-help").textContent.includes("500 条记录")'));
  await chooseXlsx('第一工作表.xlsx', xlsx);
  await until(() => visible('#table-review'));
  assert.match(await evaluate('document.querySelector("#table-sheet").textContent'), /首个工作表.*输入/);
  assert.match(await evaluate('document.querySelector("#table-count").textContent'), /2 条记录/);
  assert.deepEqual(await previewRows(), [['0007', '2.50', '2026-11-09'], ['带空格 尾', '', '2026-11-09']]);
  await click('#table-fields .table-field:nth-of-type(2) input[type="checkbox"]');
  assert.deepEqual(await previewRows(), [['0007', '2026-11-09'], ['带空格 尾', '2026-11-09']]);
  const beforeXlsx = (await library()).items.length;
  await screenshot('xlsx-first-sheet-review');
  await click('#table-confirm');
  const xlsxTool = await activeTool(reopened.id);
  const xlsxSeed = await savedTable(xlsxTool.id);
  assert.equal((await library()).items.length, beforeXlsx + 1);
  assert.equal(xlsxSeed.sheet, '输入');
  assert.deepEqual(xlsxSeed.columns, [{name: '编号', type: 'text'}, {name: '截止日', type: 'date'}]);
  assert.deepEqual(xlsxSeed.rows, [['0007', '2026-11-09'], ['带空格 尾', '2026-11-09']]);
  await selectHistory(originalTool.id);
  const restoredXlsx = await selectHistory(xlsxTool.id);
  assert.deepEqual((await previewRows(restoredXlsx.context)).map(row => row.slice(0, 2)), xlsxSeed.rows);
  await screenshot('xlsx-excluded-column-tool');

  stage('AC-010 large CSV preview is capped but all 500 rows are saved and restored');
  await click('#new-tool');
  await click('#table-entry');
  const largeColumns = Array.from({length: 20}, (_, index) => '字段' + (index + 1));
  const longValue = 'x'.repeat(94);
  const largeRows = Array.from({length: 500}, (_, index) =>
    ['批量记录' + (index + 1), ...Array(19).fill(longValue)]);
  const largeCsv = [largeColumns, ...largeRows].map(row => row.join(',')).join('\n');
  const largeBytes = Buffer.byteLength(largeCsv);
  assert.ok(largeBytes > 256 * 1024 && largeBytes < 1024 * 1024);
  await chooseCsv('容量边界.csv', largeCsv);
  await until(() => visible('#table-review'));
  assert.match(await evaluate('document.querySelector("#table-count").textContent'), /20 个字段、500 条记录/);
  assert.equal((await previewRows()).length, 6);
  await click('#table-confirm');
  const largeTool = await activeTool(restoredXlsx.id);
  const largeSaved = await savedTable(largeTool.id);
  assert.equal(largeSaved.rows.length, 500);
  assert.deepEqual(largeSaved.rows.at(-1), largeRows.at(-1));
  await selectHistory(originalTool.id);
  const largeRestored = await selectHistory(largeTool.id);
  assert.match(await evaluate('document.querySelector("#table-count").textContent', largeRestored.context), /500 \/ 500 条记录/);
  return {
    id: 'TEST-010', result: 'passed', exampleRows: exampleRows.length, uploadedRows: uploadedRows.length,
    explicitGenerations: 4, savedStatusRestored: true, previousToolPreservedOnMalformedCsv: true,
    xlsxFirstSheet: xlsxSeed.sheet, xlsxRows: xlsxSeed.rows.length, xlsxExcludedColumn: true,
    largeBytes, largeRows: largeSaved.rows.length, addedRecordRestored: true,
  };
}
