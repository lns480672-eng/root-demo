// CHG-029. Import, save, revise and reopen one actual table tool.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

export async function verifyTableJourney({
  cdp, evaluate, until, base, click, visible, appRequest, login, generate,
  activeTool, accountA, root, evidence, screenshot, stored,
}) {
  const live = process.env.ROOT_STAGED_LIVE === '1';
  await cdp('Emulation.setDeviceMetricsOverride', {width:1360,height:900,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  await click('#welcome-direct');
  await click('#table-entry');
  await evaluate(`(() => {
    const input = document.querySelector('#table-file');
    const transfer = new DataTransfer();
    transfer.items.add(new File(['事项,负责人,进度\\n准备演示,林明,待完成\\n核对入口,陈雪,已完成\\n'],
      '合成交付跟进表.csv', {type:'text/csv'}));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles:true}));
  })()`);
  await until(() => visible('#table-review'));
  assert.match(await evaluate('document.querySelector("#table-source").textContent'), /合成交付跟进表/);
  assert.match(await evaluate('document.querySelector("#table-count").textContent'), /2 条记录/);
  await click('#table-confirm');
  let tool = await activeTool();
  const id = tool.id;
  const toolPath = '/api/tools/' + id;
  const readRows = () => evaluate(`[...document.querySelectorAll('#table-records tbody tr')].map(row =>
    [...row.cells].slice(0, 3).map(cell => cell.textContent))`, tool.context);
  const seed = [['准备演示','林明','待完成'], ['核对入口','陈雪','已完成']];
  assert.deepEqual(await readRows(), seed);

  async function addRow(values) {
    await evaluate(`document.querySelector('#table-add-record').open = true`, tool.context);
    await evaluate(`document.querySelectorAll('#table-entry-form input[data-table-entry]').forEach(
      (input, index) => { input.value = ${JSON.stringify(values)}[index]; })`, tool.context);
    await click('#table-entry-form button[type=submit]', tool.context);
    await until(async () => JSON.stringify((await stored(id)).entries).includes(values[0]));
  }
  const extra = ['补录证据','林明','待完成'];
  await addRow(extra);
  assert.deepEqual(await readRows(), [...seed, extra]);
  const initial = (await appRequest('/api/trial')).body.generation;
  const version = async () => (await appRequest(toolPath + '/versions')).body.current;
  const taskIds = [];

  async function revise(requirement, expectedVersion) {
    await click('#revise-tool');
    const loader = tool.loader;
    await generate(requirement);
    const task = await until(async () => {
      const error = await evaluate('document.querySelector("#error").textContent');
      if (error) throw new Error(error);
      const current = await evaluate('window.tableJourneyTask');
      return current?.requirement === requirement && current;
    });
    assert.equal(task?.baseToolId, id, '修改任务绑定导入的同一工具');
    taskIds.push(task.id);
    const taskPath = '/api/staged/' + task.id;
    await until(async () => {
      const current = (await appRequest(taskPath)).body.task;
      if (['failed','unsupported','exhausted'].includes(current.state)) {
        if (expectedVersion !== null) throw new Error('修改阶段 ' + current.stage + ' 失败 ' + current.reason);
        return true;
      }
      return expectedVersion !== null && (await version()) === expectedVersion;
    }, 60000);
    if (expectedVersion !== null) {
      tool = await until(async () => {
        const current = await activeTool().catch(() => null);
        return current?.id === id && current.loader !== loader && current;
      });
      assert.equal((await appRequest(toolPath + '/versions')).body.items[0].note, requirement);
    }
  }

  await evaluate(`(() => {
    const original = window.fetch;
    window.fetch = async (url, options) => {
      const response = await original(url, options);
      if (String(url).endsWith('/revise') && response.ok) {
        window.tableJourneyTask = (await response.clone().json()).task;
      }
      return response;
    };
  })()`);
  const filter = 'select[data-table-column-filter="1"]';
  assert.equal(await evaluate(`Boolean(document.querySelector('${filter}'))`, tool.context), false);
  await revise('增加一个按「负责人」精确筛选的下拉框，保留其他功能和数据。', 2);

  async function assertFilter() {
    const choose = value => evaluate(`(() => {
      const select = document.querySelector('${filter}');
      const option = [...select.options].find(item => item.textContent.trim() === ${JSON.stringify(value)});
      if (!option) throw new Error('缺少负责人选项');
      select.value = option.value;
      select.dispatchEvent(new Event('change'));
    })()`, tool.context);
    await choose('林明');
    assert.deepEqual(await readRows(), [seed[0], extra]);
    await choose('陈雪');
    assert.deepEqual(await readRows(), [seed[1]]);
    await choose('全部');
    assert.deepEqual(await readRows(), [...seed, extra]);
    assert.equal(await evaluate('Boolean(document.querySelector("#table-search") && document.querySelector("#table-filter"))',
      tool.context), true);
  }

  await assertFilter();
  const title = '交付行动看板';
  await revise('只把标题改成「' + title + '」，保留其他功能和数据。', 3);
  assert.equal(await evaluate('document.querySelector("h1").textContent', tool.context), title);
  await until(async () => await evaluate('document.querySelector("#preview-title").textContent') === title);
  await assertFilter();
  if (!live) {
    await revise('删除负责人筛选并清空所有原记录。', null);
    assert.equal(await version(), 3);
    await assertFilter();
    assert.equal((await appRequest('/api/trial')).body.generation.used, initial.used + 2);
  }

  const navigation = await evaluate('performance.timeOrigin');
  await cdp('Page.reload', {ignoreCache:true});
  await until(() => evaluate(`performance.timeOrigin !== ${navigation} && document.documentElement.dataset.ready === 'true'`));
  assert.equal(await evaluate('document.querySelector("#library-entry").getAttribute("aria-current")'), 'page');
  assert.match(await evaluate('document.querySelector("#tool-list button[aria-current=true]").dataset.version'), /^v3$/);
  tool = await activeTool();
  assert.equal(tool.id, id);
  assert.equal(await version(), 3);
  assert.equal(await evaluate('document.querySelector("h1").textContent', tool.context), title);
  await assertFilter();
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => taskIds.includes(item.task));
  if (live) {
    assert.ok(evidence.runs.length >= 2);
    assert.ok(evidence.runs.every(item => item.request_id && item.usage?.total_tokens > 0));
  }
  evidence.tests.push({id:'CHG-029', result:'passed', live, tool:id, versions:[1,2,3],
    sample:'合成交付跟进表.csv', importedRows:2, addedRows:1, exactFilter:true,
    titleOnlyRetainsFeatures:true, unsafeCandidateRejected:!live, sameToolAfterReload:true});
  await screenshot('table-journey-restored');
}
