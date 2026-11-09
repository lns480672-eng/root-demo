import assert from 'node:assert/strict';

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
  const previousNavigation = await evaluate('performance.timeOrigin');
  await cdp('Page.reload', { ignoreCache: true });
  await until(() => evaluate(`performance.timeOrigin !== ${previousNavigation} && document.documentElement.dataset.ready === 'true'`));
  const reloaded = await activeTool();
  assert.equal(reloaded.id, uploadedTool.id);
  assert.notEqual(reloaded.loader, uploadedTool.loader);
  assert.equal(await evaluate('document.querySelector(\'[data-row-status="0"]\').checked', reloaded.context), true);
  assert.deepEqual((await savedTable(reloaded.id)).rows, uploadedRows);
  await selectHistory(originalTool.id);
  const reopened = await selectHistory(uploadedTool.id);
  assert.equal(reopened.id, uploadedTool.id);
  assert.deepEqual((await previewRows(reopened.context)).map(row => row.slice(0, 4)), uploadedRows);
  assert.equal((await savedTable(reopened.id)).done[0], true);
  return {
    id: 'TEST-010', result: 'passed', exampleRows: exampleRows.length, uploadedRows: uploadedRows.length,
    explicitGenerations: 2, savedStatusRestored: true, previousToolPreservedOnMalformedCsv: true,
  };
}
