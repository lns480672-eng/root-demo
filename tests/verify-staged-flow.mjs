import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const requirement = '做一个中文求职投递看板，新增卡片填写公司名称、岗位名称、投递时间，可选填写链接和备注。' +
  '按以下阶段顺序分栏：待投递、已投递、简历筛选、面试中、已发offer、已结束。新增默认待投递。' +
  '卡片可点按钮切换阶段并移到对应栏，不需要拖拽。可以编辑和删除卡片。' +
  '浏览器保存数据，能导出全部投递记录为 JSON 备份文件，选之前下载的文件导入恢复。';
const taskPath = '/api/staged/';

export async function verifyStagedFlow(ctx) {
  const { cdp, evaluate, until, base, click, pointerClick, type, visible, appRequest, login, generate,
    activeTool, accountA, root, probeUrl, probeRequests, probeControls, library, stored, evidence, screenshot } = ctx;
  const stagedLive = process.env.ROOT_STAGED_LIVE === '1';
  await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  const downloadPath = join(root, 'downloads');
  await mkdir(downloadPath);
  await cdp('Browser.setDownloadBehavior', {behavior:'allow',downloadPath});
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await generate('做一份简单读书清单，新增书名并保存。');
  const old = await activeTool();
  const sandbox = () => evaluate('document.querySelector("#preview iframe[data-tool]").getAttribute("sandbox")');
  assert.equal(await sandbox(), 'allow-scripts allow-forms');
  await click('#new-tool');
  await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
  await generate(requirement);
  const task = async () => {
    const {body} = await appRequest('/api/staged');
    return body.tasks?.find(item => item.requirement === requirement);
  };
  const first = await until(task, 90000);
  const selected = '#staged-task-list button[data-task-id="' + first.id + '"]';
  if (!stagedLive) {
    await until(async () => (await task())?.state === 'failed');
    assert.deepEqual((await task()).completed, ['spec']);
    const second = await appRequest('/api/generate', {requirement:requirement + ' 另一个任务。', requestId:'2'.repeat(32)});
    assert.equal(second.status, 202);
    await until(async () => (await appRequest(taskPath + second.body.task.id)).body.task.state === 'failed');
    await cdp('Page.reload');
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    assert.equal((await activeTool()).id, old.id);
    for (const width of [1440, 390]) {
      await cdp('Emulation.setDeviceMetricsOverride', {width,height:844,deviceScaleFactor:1,mobile:width===390});
      assert.equal(await visible('#staged-panel'), false);
      await click('#new-tool');
      assert.equal(await visible(selected), true);
      assert.equal(await evaluate('document.querySelectorAll("#staged-task-list button[data-task-id]").length'), 2);
      if (width === 1440) await click('#return-to-library');
    }
    await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await click(selected);
    assert.equal(await evaluate('document.querySelector("#requirement").value'), requirement);
    assert.ok(await visible('#staged-issue'));
    await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
    await click('#staged-resume');
    evidence.tests.push('中途失败后整页刷新能找到同账号两项任务，选原任务续跑');
  }
  let realResume = null;
  await until(async () => {
    const current = await task();
    if (stagedLive && current?.state === 'failed' && await visible('#staged-resume')) {
      if (realResume && realResume !== current.issue) {
        throw new Error('真实任务续跑后仍失败，阶段 ' + current.stage + '，原因 ' + current.reason);
      }
      if (!realResume) {
        realResume = current.issue;
        await click('#staged-resume');
        evidence.tests.push('真模型阶段异常后从同一个任务继续');
      }
    }
    if (current?.state === 'unsupported') throw new Error('真实任务被误判不支持');
    return evaluate('typeof window.releaseFixturePreview === "function"');
  }, 120000);
  assert.equal((await library()).items.length, 1);
  const activeOld = await activeTool();
  await type('#book', '预览期间仍可保存', activeOld.context);
  await click('#add', activeOld.context);
  await until(async () => JSON.stringify((await stored(old.id)).entries).includes('预览期间仍可保存'));
  await evaluate('window.releaseFixturePreview()');
  const board = await until(async () => {
    const current = await activeTool().catch(() => null);
    return current && current.id !== old.id && current;
  }, 12000);
  assert.equal(await sandbox(), 'allow-scripts allow-forms allow-downloads');
  assert.equal((await library()).items.length, 2);
  const inBoard = expression => evaluate(expression, board.context);
  const count = () => inBoard('document.querySelectorAll("#record-board article").length');
  const columns = await inBoard('Array.from(document.querySelectorAll("[data-board-column]"), col => col.dataset.boardColumn)');
  assert.deepEqual(columns.map(value => value.toLowerCase().replace(/\s+/g,'')), ['待投递','已投递','简历筛选','面试中','已发offer','已结束']);
  assert.equal(await count(), 0);
  const ids = await inBoard('Array.from(document.querySelectorAll("#entry label"), label => [label.firstChild.textContent,label.lastElementChild.id])');
  const field = pattern => {
    const match = ids.find(([label]) => pattern.test(label));
    assert.ok(match, '缺少表单字段 ' + pattern);
    return match[1];
  };
  const company = field(/公司/), role = field(/岗位|职位/), date = field(/时间|日期/), link = field(/链接/), note = field(/备注/);
  const record = {[company]:'合成甲公司',[role]:'现场协调',[date]:'2026-11-09',[link]:'https://example.test/qa',[note]:'保留说明'};
  const fill = data => inBoard('(' + ((values) => {
    for (const [id,value] of Object.entries(values)) document.getElementById(id).value = value;
  }).toString() + ')(' + JSON.stringify(data) + ')');
  await fill(record);
  await click('#add-record', board.context);
  await fill({...record,[company]:'合成乙公司',[role]:'材料核对',[note]:''});
  await click('#add-record', board.context);
  assert.equal(await count(), 2);
  assert.equal(await inBoard('document.querySelectorAll("[data-board-column=待投递] article").length'), 2);
  await click('[data-record="1"] [data-move-stage="面试中"]', board.context);
  await click('[data-edit-record="1"]', board.context);
  assert.equal(await inBoard('document.getElementById(' + JSON.stringify(link) + ').value'), record[link]);
  await fill({[role]:'现场复核'});
  await click('#add-record', board.context);
  assert.match(await inBoard('document.querySelector("[data-board-column=面试中] article").textContent'), /现场复核/);

  await pointerClick('#export-backup', board.context);
  const backupFile = join(downloadPath, 'root-tool-backup.json');
  const backup = await until(async () => readFile(backupFile, 'utf8').catch(() => ''));
  const decoded = JSON.parse(backup);
  assert.equal(decoded.format, 'root-structured-backup');
  assert.equal(decoded.records.length, 2);
  assert.match(backup, /现场复核/);
  await click('[data-delete-record="2"]', board.context);
  assert.equal(await count(), 2);
  await click('[data-confirm-delete="2"]', board.context);
  assert.equal(await count(), 1);
  const snapshot = await until(async () => {
    const current = await stored(board.id);
    return current.entries.length && !JSON.stringify(current.entries).includes('合成乙公司') && current;
  });
  const selectFile = async path => {
    const {result} = await cdp('Runtime.evaluate', {
      expression:'document.querySelector("#import-backup")',contextId:board.context.id,
    }, board.context.session);
    await cdp('DOM.setFileInputFiles', {files:[path],objectId:result.objectId}, board.context.session);
  };
  const status = () => inBoard('document.querySelector("#backup-status").textContent');
  const corrupt = join(root, 'bad-backup.json');
  await writeFile(corrupt, '{invalid');
  await selectFile(corrupt);
  await until(async () => (await status()).includes('导入失败'));
  assert.deepEqual((await stored(board.id)).entries, snapshot.entries);

  await selectFile(backupFile);
  await until(() => inBoard('document.querySelector("#confirm-import").checkVisibility()'));
  await click('#cancel-import', board.context);
  assert.equal(await count(), 1);
  assert.deepEqual((await stored(board.id)).entries, snapshot.entries);

  await selectFile(backupFile);
  await until(() => inBoard('document.querySelector("#confirm-import").checkVisibility()'));
  await evaluate('(() => { const original = window.fetch; window.fetch = (url, options) => {' +
    'if (String(url).endsWith(' + JSON.stringify('/api/tools/' + board.id + '/data') + ')) {' +
    'window.fetch = original; return Promise.resolve(new Response(JSON.stringify({code:"forced",message:"替身保存失败"}),' +
    '{status:503,headers:{"Content-Type":"application/json"}})); } return original(url, options); }; })()');
  await click('#confirm-import', board.context);
  await until(async () => /失败|未确认/.test(await status()));
  assert.equal(await count(), 1);
  assert.deepEqual((await stored(board.id)).entries, snapshot.entries);

  await selectFile(backupFile);
  await until(() => inBoard('document.querySelector("#confirm-import").checkVisibility()'));
  await click('#confirm-import', board.context);
  await until(async () => (await count()) === 2);
  assert.match(await status(), /已恢复 2/);
  assert.match(JSON.stringify((await stored(board.id)).entries), /合成乙公司/);
  evidence.tests.push('同入口真实发布前旧工具能写入；分栏、移动、编辑、删除及实际下载和导入恢复通过；导入无效、取消及服务拒绝均保留数据');

  assert.equal(await inBoard('(() => { try { return Boolean(parent.document); } catch { return false; } })()'), false);
  assert.equal(await inBoard('fetch(' + JSON.stringify(probeUrl + '/board-preview') + ',{mode:"no-cors"}).then(() => true, () => false)'), false);
  assert.deepEqual(probeRequests, probeControls);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  const restored = await activeTool();
  assert.equal(restored.id, board.id);
  assert.equal(await sandbox(), 'allow-scripts allow-forms allow-downloads');
  assert.equal(await evaluate('document.querySelectorAll("#record-board article").length', restored.context), 2);
  assert.match(await evaluate('document.querySelector("[data-board-column=面试中] article").textContent', restored.context), /现场复核/);
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => item.task === first.id);
  assert.equal(evidence.runs.filter(item => item.stage === 'spec').length, 1);
  const expected = stagedLive ? ['spec','entry'] : ['spec','entry','filters','summary','export'];
  assert.deepEqual(evidence.runs.filter(item => item.status === 'ok').map(item => item.stage), expected);
  if (stagedLive) assert.ok(evidence.runs.every(item => item.request_id && item.usage?.total_tokens > 0));
  evidence.tests.push('刷新恢复同一看板和状态，父页与外部探针隔离');
  await screenshot('staged-board-restored');
}

export async function verifyStagedRegistration(ctx) {
  const { cdp, evaluate, until, base, click, visible, appRequest, login, generate,
    activeTool, accountA, root, stored, evidence, screenshot } = ctx;
  const input = '做一个公益活动雨具归还登记。每条填写物品编号（必填文本）、数量（必填非负整数）、归还日期（必填日期）。' +
    '只需新增、查看记录和刷新后保留，不需要筛选、统计或导出。';
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  const before = (await appRequest('/api/trial')).body;
  await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
  await generate(input);
  const task = await until(async () => {
    const {body} = await appRequest('/api/staged');
    const current = body.tasks?.find(item => item.requirement === input);
    if (['failed','unsupported','exhausted'].includes(current?.state)) {
      throw new Error('登记生成失败 ' + current.stage + ' / ' + current.reason);
    }
    return current?.state === 'complete' && await evaluate('typeof window.releaseFixturePreview === "function"') && current;
  }, 105000);
  assert.deepEqual(task.steps, ['spec','entry','assemble']);
  assert.deepEqual(task.completed, task.steps);
  if (process.env.ROOT_STAGED_LIVE !== '1') {
    await evaluate(`(() => {
      const original = window.fetch;
      window.fetch = (path, options) => {
        if (String(path).endsWith('/resolve-publish')) window.fixtureResolveCalls++;
        if (!String(path).endsWith('/publish')) return original(path, options);
        if (window.releaseLateStagedPublish) {
          return original(path, options).then(() => { throw new TypeError('fixture: saved response missing'); });
        }
        window.releaseLateStagedPublish = () => original(path, {...options, signal: undefined})
          .then(async response => ({status:response.status, code:(await response.json()).code}));
        return Promise.resolve(new Response('{"error":"unavailable"}',
          {status:502, headers:{'Content-Type':'application/json'}}));
      };
      window.fixtureResolveCalls = 0;
    })()`);
  }
  await evaluate('window.releaseFixturePreview()');
  if (process.env.ROOT_STAGED_LIVE !== '1') {
    await until(() => visible('#staged-sync'));
    assert.equal(await evaluate('window.fixtureResolveCalls'), 0, '自动核对只能读取保存结果');
    assert.equal(await evaluate('document.querySelector("#requirement").value'), input);
    await click('#staged-sync');
    await until(async () => (await evaluate('document.querySelector("#staged-reason").textContent'))
      .includes('未查到已保存的工具'));
    assert.deepEqual(await evaluate('window.releaseLateStagedPublish()'),
      {status:409, code:'staged_candidate_changed'});
    assert.equal((await appRequest('/api/tools')).body.items.length, 0);
    assert.equal(await evaluate('window.fixtureResolveCalls'), 1);
    await click('#staged-preview');
  }
  const tool = await activeTool();
  const frame = expression => evaluate(expression, tool.context);
  assert.deepEqual(await frame('["filters","summary","export"].map(id => document.getElementById(id).hidden)'),
    [true,true,true]);
  assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").getAttribute("sandbox")'),
    'allow-scripts allow-forms');
  const fields = await frame('Array.from(document.querySelectorAll("#entry input"), item => [item.type,item.required])');
  assert.deepEqual(fields.sort(), [['date',true],['number',true],['text',true]]);
  await frame(`(() => {
    document.querySelector('#entry input[type="text"]').value = 'QA-伞-001';
    document.querySelector('#entry input[type="number"]').value = '0';
    document.querySelector('#entry input[type="date"]').value = '2026-11-09';
  })()`);
  await click('#add-record', tool.context);
  const cells = await frame('Array.from(document.querySelectorAll("#records tbody td"), cell => cell.textContent)');
  assert.deepEqual(cells.sort(), ['0','2026-11-09','QA-伞-001']);
  await until(async () => JSON.stringify((await stored(tool.id)).entries).includes('QA-伞-001'));
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  const restored = await activeTool();
  assert.equal(restored.id, tool.id);
  assert.match(await evaluate('document.querySelector("#records tbody").textContent', restored.context), /QA-伞-001/);
  const published = (await appRequest(taskPath + task.id)).body.task;
  assert.equal(published.toolId, tool.id);
  const after = (await appRequest('/api/trial')).body;
  assert.deepEqual(after.analysis, before.analysis);
  const personal = after.generation.used - before.generation.used;
  const dispatched = after.generation.totalUsed - before.generation.totalUsed;
  assert.equal(personal, 1);
  assert.equal(dispatched, 2);
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => item.task === task.id);
  assert.deepEqual(evidence.runs.map(item => item.stage), ['spec','entry']);
  if (process.env.ROOT_STAGED_LIVE === '1') {
    assert.ok(evidence.runs.every(item => item.request_id && item.usage?.total_tokens > 0));
  }
  evidence.tests.push({ id:'ROOT-GEN-IA-04', steps:task.steps, toolId:tool.id, restored:true, personal, dispatched });
  await screenshot('staged-registration-restored');
}

export async function verifyStagedReconnect(ctx) {
  const { cdp, evaluate, until, base, click, visible, appRequest, login, generate,
    activeTool, accountA, evidence, screenshot } = ctx;
  const input = '做一份简单读书清单，新增书名并保存。';
  const nextInput = input + ' 保留我输入的书名。';
  await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await generate(input);
  const old = await activeTool();
  const before = (await appRequest('/api/trial')).body.generation.used;
  await click('#new-tool');
  await evaluate(`(() => {
    const original = window.fetch;
    window.fixtureSubmissions = [];
    window.fixtureReads = 0;
    window.fetch = async (path, options) => {
      if (path === '/api/staged' && window.fixtureSubmissions.length === 3 && !window.fixtureRestoreReads) {
        window.fixtureReads++;
        throw new TypeError('fixture: workbench unavailable');
      }
      if (path !== '/api/generate') return original(path, options);
      window.fixtureSubmissions.push(JSON.parse(options.body));
      if (window.fixtureSubmissions.length === 2) {
        return new Response('{"error":"unavailable"}', {status:503, headers:{'Content-Type':'application/json'}});
      }
      const response = await original(path, options);
      if (window.fixtureSubmissions.length !== 1) return response;
      window.fixtureAcceptedTask = (await response.json()).task.id;
      window.fixtureSubmissionBounded = !!options.signal;
      return new Promise((resolve, reject) => {
        if (options.signal?.aborted) reject(options.signal.reason);
        else options.signal?.addEventListener('abort', () => reject(options.signal.reason), {once:true});
      });
    };
  })()`);
  await generate(nextInput);
  const taskId = await until(() => evaluate('window.fixtureAcceptedTask'));
  assert.equal(await evaluate('window.fixtureSubmissionBounded'), true, '提交已受理但无响应时，页面必须结束等待');
  await until(() => visible('#error'));
  assert.match(await evaluate('document.querySelector("#error").textContent'), /连接.*保留.*重试/);
  assert.equal((await activeTool()).id, old.id);
  assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").inert'), false);
  assert.equal(await evaluate('document.querySelector("#requirement").value'), nextInput);
  await screenshot('submit-timeout-narrow');
  await click('#generate');
  await until(async () => await evaluate('window.fixtureSubmissions.length === 2') && await visible('#error'));
  await click('#generate');
  await until(() => visible('#staged-refresh'));
  await until(() => evaluate('window.fixtureReads >= 1'));
  await new Promise(resolve => setTimeout(resolve, 3200));
  assert.equal(await evaluate('window.fixtureReads'), 1, '连接失败后停止本轮自动读取');
  const requests = await evaluate('window.fixtureSubmissions');
  assert.equal(requests.length, 3);
  assert.match(requests[0].requestId, /^[a-f0-9]{32}$/);
  assert.deepEqual(requests[0], requests[1]);
  assert.deepEqual(requests[0], requests[2]);
  await screenshot('task-read-retry-narrow');
  await evaluate('window.fixtureRestoreReads = true');
  await click('#staged-refresh');
  const saved = await activeTool(old.id);
  const task = (await appRequest(taskPath + taskId)).body.task;
  assert.equal(task.state, 'published');
  assert.equal(task.toolId, saved.id);
  assert.equal((await appRequest('/api/tools')).body.items.length, 2);
  assert.equal((await appRequest('/api/trial')).body.generation.used - before, 1);
  evidence.tests.push({id:'TEST-020-01/03', taskId, toolId:saved.id, submissions:requests.length, personal:1});
}

export async function verifyLedgerTheme(ctx) {
  const { cdp, evaluate, until, base, click, visible, appRequest, login, generate,
    activeTool, accountA, root, evidence, screenshot, stored } = ctx;
  const input = '做一个中文求职投递看板。每条卡片填写公司名称、岗位名称、投递时间。' +
    '按待投递、已投递、简历筛选、面试中、已发offer、已结束分栏；默认待投递，能移动、编辑和删除。';
  const live = process.env.ROOT_STAGED_LIVE === '1';
  async function settle(text) {
    const task = await until(async () => (await appRequest('/api/staged')).body.tasks?.find(item => item.requirement === text), 12000);
    return until(async () => {
      const current = (await appRequest(taskPath + task.id)).body.task;
      return ['published','unsupported','failed'].includes(current.state) && current;
    }, 135000);
  }
  const balance = async () => (await appRequest('/api/trial')).body.generation;
  const board = ({ context }) => evaluate(`({
    background: getComputedStyle(document.body).backgroundColor,
    columns: [...document.querySelectorAll('[data-board-column]')].map(col => ({
      name: col.dataset.boardColumn, background: getComputedStyle(col).backgroundColor,
      cards: [...col.querySelectorAll('article')].map(card => card.textContent),
    })),
  })`, context);
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await generate(input);
  const initial = await settle(input);
  assert.equal(initial.state, 'published', initial.reason);
  const old = await activeTool();
  const fields = await evaluate('[...document.querySelectorAll("#entry label")].map(label => [label.firstChild.textContent, label.lastElementChild.id])', old.context);
  for (const company of ['合成甲公司', '合成乙公司']) {
    const inputs = [[/公司/, company], [/岗位|职位/, '验收'], [/时间|日期/, '2026-11-09']];
    for (const [pattern, value] of inputs) {
      const [, id] = fields.find(([label]) => pattern.test(label));
      await evaluate('document.getElementById(' + JSON.stringify(id) + ').value=' + JSON.stringify(value), old.context);
    }
    await click('#add-record', old.context);
    await until(async () => JSON.stringify((await stored(old.id)).entries).includes(company));
  }
  await click('[data-record="1"] [data-move-stage="面试中"]', old.context);
  const before = await until(async () => {
    const current = await stored(old.id);
    return JSON.stringify(current.entries).includes('面试中') && current;
  });
  const beforeBoard = await board(old);
  assert.equal(beforeBoard.background, 'rgb(244, 245, 248)');
  assert.match(beforeBoard.columns.find(col => col.name === '面试中').cards.join(), /合成甲公司/);
  const beforeBalance = await balance();
  const versions = async () => (await appRequest('/api/tools/' + old.id + '/versions')).body;
  await screenshot('before-theme');
  await click('#revise-tool');
  await generate('把颜色改成黄色');
  const revised = await settle('把颜色改成黄色');
  assert.equal(revised.state, 'published', revised.reason);
  assert.equal(revised.toolId, old.id);
  const history = await versions();
  assert.deepEqual(history.items.map(item => item.version), [2, 1]);
  assert.notEqual(history.items[0].commit, history.items[1].commit);
  assert.deepEqual((await stored(old.id)).entries, before.entries);
  assert.equal((await balance()).used, beforeBalance.used + 1);
  assert.equal((await balance()).totalUsed, beforeBalance.totalUsed + 1);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  const restored = await activeTool();
  assert.equal(restored.id, old.id);
  const after = await board(restored);
  assert.equal(after.background, 'rgb(255, 241, 184)');
  assert.deepEqual(after.columns, beforeBoard.columns.map(col => ({...col, background:after.background})));
  await screenshot('after-theme-reload');
  const negatives = live ? ['把颜色改成黑色'] : ['把颜色改成黑色', '把标题字号调整到 22px'];
  for (const change of negatives) {
    await click('#revise-tool');
    await generate(change);
    const rejected = await settle(change);
    assert.equal(rejected.state, 'unsupported');
    if (change.includes('22px')) assert.equal(rejected.reason, 'revision_no_change');
    await until(async () => /旧版仍可用.*未扣除/.test(await evaluate('document.querySelector("#staged-reason").textContent')));
    assert.deepEqual(await versions(), history);
    assert.deepEqual((await stored(old.id)).entries, before.entries);
    assert.equal((await balance()).used, beforeBalance.used + 1);
    assert.deepEqual(await board(await activeTool()), after);
    await screenshot(change.includes('22px') ? 'unchanged-theme' : 'unsupported-theme');
  }
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => [initial.id, revised.id].includes(item.task));
  if (live) assert.ok(evidence.runs.every(item => item.request_id && item.usage?.total_tokens > 0));
  evidence.tests.push({id:'ROOT-REV-VIS-01', before:beforeBoard, after, toolId:old.id, history, personal:1, negatives});
}

export async function verifyStagedCalculator(ctx) {
  const { cdp, evaluate, until, base, click, visible, appRequest, login, generate,
    activeTool, accountA, root, evidence, screenshot } = ctx;
  const input = '做一个摄氏和华氏温度双向换算工具，保留两位小数。使用数字输入 id=temperature，' +
    '单位下拉 id=unit，选项值 celsius（输入摄氏）、fahrenheit（输入华氏），' +
    '点击按钮 id=convert 进行换算，将可见结果写入 id=result。无效输入提示错误。无需联网。';
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
  await generate(input);
  let identifier;
  await until(async () => {
    const {body} = await appRequest('/api/staged');
    const task = body.tasks?.find(item => item.requirement === input);
    if (!task) return false;
    identifier = task.id;
    if (['failed','unsupported'].includes(task.state)) throw new Error('计算器生成失败 ' + task.stage + ' / ' + task.reason);
    return evaluate('typeof window.releaseFixturePreview === "function"');
  }, 90000);
  const {body:{task}} = await appRequest(taskPath + identifier);
  assert.deepEqual(task.steps, ['spec','implementation','assemble']);
  assert.deepEqual(task.completed, task.steps);
  await evaluate('window.releaseFixturePreview()');
  const tool = await activeTool();
  const run = expression => evaluate(expression, tool.context);
  async function check(value, unit, expected) {
    await run('document.getElementById("temperature").value=' + JSON.stringify(value) +
      ';document.getElementById("unit").value=' + JSON.stringify(unit) + ';');
    await click('#convert', tool.context);
    assert.match(await run('document.querySelector("#result").textContent'), expected);
  }
  await check('100','celsius',/212(?:\.00)?/);
  await check('32','fahrenheit',/(^|[^0-9])0(?:\.00)?/);
  assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").getAttribute("sandbox")'), 'allow-scripts allow-forms');
  const published = (await appRequest(taskPath + identifier)).body.task;
  assert.equal(published.toolId, tool.id);
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => item.task === identifier);
  assert.deepEqual(evidence.runs.map(item => item.stage), ['spec','implementation']);
  assert.ok(evidence.runs.every(item => item.request_id && item.usage?.total_tokens > 0));
  evidence.tests.push('统一生成任务走两次真实模型调用并发布，100℃→212℉ 和 32℉→0℃ 均正确');
  await screenshot('staged-calculator');
}

export async function verifyStagedRevision(ctx) {
  const { cdp, evaluate, until, base, click, type, visible, appRequest, login, generate,
    activeTool, accountA, root, evidence, screenshot, stored, anotherBrowser } = ctx;
  const change = '在现有读书清单增加书名关键词筛选。保留已有的添加书名和显示书单功能，'
    + '沿用 books 存储键及字符串数组格式。新增关键词输入框 id=search，输入时筛选现有书单。';
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await generate('做一份简单读书清单，新增书名并保存。');
  const old = await activeTool();
  const toolPath = '/api/tools/' + old.id;
  async function add(title, context = old.context) {
    await evaluate('document.querySelector("#book").value=""', context);
    await type('#book', title, context);
    await click('#add', context);
    await until(async () => JSON.stringify((await stored(old.id)).entries).includes(title));
  }
  await add('甲 旧版已有');
  await click('#revise-tool');
  await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
  await generate(change);
  const task = await until(async () => {
    const {body} = await appRequest('/api/staged');
    return body.tasks?.find(item => item.requirement === change);
  }, 90000);
  assert.equal(task.baseToolId, old.id);
  assert.equal(task.baseVersion, 1);
  await add('乙 生成期间写入');
  await until(async () => {
    const current = (await appRequest(taskPath + task.id)).body.task;
    if (['failed','unsupported'].includes(current.state)) {
      throw new Error('真模型修订失败 ' + current.stage + ' / ' + current.reason);
    }
    return evaluate('typeof window.releaseFixturePreview === "function"');
  }, 90000);
  await anotherBrowser('staged-revision-second', async () => {
    await cdp('Page.navigate', {url:base});
    await login(accountA);
    const other = await activeTool();
    assert.equal(other.id, old.id);
    await add('丙 候选之后写入', other.context);
  });
  assert.equal((await appRequest(toolPath + '/versions')).body.current, 1);
  await evaluate('window.releaseFixturePreview()');
  await until(() => evaluate('document.querySelector("#staged-preview")?.checkVisibility() && !document.querySelector("#staged-preview").disabled'));
  assert.equal((await activeTool()).loader, old.loader);
  assert.match(JSON.stringify((await stored(old.id)).entries), /丙 候选之后写入/);
  await click('#staged-preview');
  const revised = await until(async () => {
    const current = await activeTool().catch(() => null);
    return current && current.id === old.id && current.loader !== old.loader && current;
  }, 15000);
  assert.equal((await appRequest(toolPath + '/versions')).body.current, 2);
  const text = () => evaluate('document.querySelector("#list").textContent', revised.context);
  for (const name of ['甲 旧版已有','乙 生成期间写入','丙 候选之后写入']) assert.ok((await text()).includes(name));
  await type('#search', '乙', revised.context);
  assert.match(await text(), /乙 生成期间写入/);
  assert.doesNotMatch(await text(), /甲|丙/);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  assert.equal((await activeTool()).id, old.id);
  assert.equal((await appRequest(toolPath + '/versions')).body.current, 2);
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => item.task === task.id);
  assert.deepEqual(evidence.runs.map(item => item.stage), ['spec','implementation']);
  assert.ok(evidence.runs.every(item => item.request_id && item.usage?.total_tokens > 0));
  evidence.tests.push('同一工具走统一修改任务，生成及候选准备后另一会话仍实际写入，过时快照拒绝，复用同一候选重开后新版保留三笔记录');
  evidence.tests.push('新版关键词筛选实际生效，刷新恢复同工具及版本，重新预览没有重派发模型');
  await screenshot('staged-revision-restored');
}
