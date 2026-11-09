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
    assert.equal(await evaluate('document.querySelectorAll("#staged-task-list button[data-task-id]").length'), 2);
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
  assert.deepEqual(evidence.runs.filter(item => item.status === 'ok').map(item => item.stage), ['spec','entry','filters','summary','export']);
  if (stagedLive) assert.ok(evidence.runs.every(item => item.request_id && item.usage?.total_tokens > 0));
  evidence.tests.push('刷新恢复同一看板和状态，父页与外部探针隔离');
  await screenshot('staged-board-restored');
}
