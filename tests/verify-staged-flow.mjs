import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const requirement = '做一个中文求职投递看板，新增卡片填写公司名称、岗位名称、投递时间，可选填写链接和备注。' +
  '按以下阶段顺序分栏：待投递、已投递、简历筛选、面试中、已发offer、已结束。新增默认待投递。' +
  '卡片可点按钮切换阶段并移到对应栏，不需要拖拽。可以编辑和删除卡片。' +
  '浏览器保存数据，能导出全部投递记录为 JSON 备份文件，选之前下载的文件导入恢复。';
const taskPath = '/api/staged/';

async function findStagedTask(appRequest, requirement, exclude) {
  for (const path of ['/api/staged', '/api/staged/history']) {
    const {body} = await appRequest(path);
    const task = body.tasks?.find(item => item.requirement === requirement && item.id !== exclude);
    if (task) return task;
  }
}

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
  await type('#book', '从我的工具找回原记录', old.context);
  await click('#add', old.context);
  await until(async () => JSON.stringify((await stored(old.id)).entries).includes('从我的工具找回原记录'));
  const used = async () => (await appRequest('/api/trial')).body.generation.used;
  const before = await used();
  const sandbox = () => evaluate('document.querySelector("#preview iframe[data-tool]").getAttribute("sandbox")');
  assert.equal(await sandbox(), 'allow-scripts allow-forms');
  await click('#new-tool');
  await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
  await generate(requirement);
  const first = await until(() => findStagedTask(appRequest, requirement), 90000);
  let publishingTask = first.id;
  const task = async id => (await appRequest(taskPath + id)).body.task;
  const selected = '#staged-history-list button[data-history-task-id="' + first.id + '"]';
  if (!stagedLive) {
    const failed = await until(async () => {
      const current = await task(first.id);
      return current.state === 'failed' && current;
    });
    assert.deepEqual(failed.completed, ['spec']);
    assert.equal(failed.stage, 'entry');
    assert.equal(failed.reason, 'invalid_staged_result');
    assert.equal(await used(), before);
    const second = await appRequest('/api/generate', {requirement:requirement + ' 另一个任务。', requestId:'2'.repeat(32)});
    assert.equal(second.status, 202);
    await until(async () => (await appRequest(taskPath + second.body.task.id)).body.task.state === 'failed');
    assert.equal(await used(), before);
    await cdp('Page.reload');
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    await click('#library-entry');
    assert.equal((await activeTool()).id, old.id);
    for (const width of [1440, 390]) {
      await cdp('Emulation.setDeviceMetricsOverride', {width,height:844,deviceScaleFactor:1,mobile:width===390});
      assert.equal(await visible('#staged-panel'), false);
      await click('#new-tool');
      if (!await evaluate('document.querySelector("#staged-history").open')) await click('#staged-history summary');
      await until(() => visible(selected));
      assert.equal(await evaluate('document.querySelectorAll("#staged-history-list button[data-history-task-id]").length'), 2);
      assert.equal(await evaluate('document.querySelectorAll("#staged-task-list button[data-task-id]").length'), 0);
      if (width === 1440) await click('#library-entry');
    }
    await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await click(selected);
    assert.equal(await evaluate('document.querySelector("#requirement").value'), requirement);
    assert.ok(await visible('#staged-issue'));
    assert.equal(await visible('#staged-resume'), false);
    assert.equal(await used(), before);
    await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
    await click('#generate');
    publishingTask = (await until(() => findStagedTask(appRequest, requirement, first.id), 90000)).id;
    evidence.tests.push('中途失败后整页刷新能从历史找回需求，明确重新提交才创建新任务');
  }
  let retried = false;
  await until(async () => {
    const current = await task(publishingTask);
    if (stagedLive && current?.state === 'failed' && await visible('#staged-reason')) {
      if (retried) throw new Error('真实任务重新提交后仍失败，阶段 ' + current.stage + '，原因 ' + current.reason);
      retried = true;
      await click('#generate');
      publishingTask = (await until(() => findStagedTask(appRequest, requirement, first.id), 90000)).id;
      evidence.tests.push('真模型阶段异常后明确提交新任务');
    }
    if (current?.state === 'unsupported') throw new Error('真实任务被误判不支持');
    return evaluate('typeof window.releaseFixturePreview === "function"');
  }, 120000);
  assert.equal((await library()).items.length, 1);
  assert.equal(await visible('.elys-preview'), false, '新工具在保存前不显示预览');
  assert.match(JSON.stringify((await stored(old.id)).entries), /从我的工具找回原记录/);
  await evaluate('window.releaseFixturePreview()');
  const board = await activeTool(old.id).catch(async error => {
    const current = await task(publishingTask);
    const message = await evaluate('document.querySelector("#staged-reason").textContent');
    throw new Error(`${error.message}，任务 ${current.state} / ${current.reason || '-'}，${message}`);
  });
  assert.equal(await visible('#generation-saved'), true);
  assert.equal(await visible('#open-generated-tool'), true);
  assert.equal(await visible('#requirement'), false);
  assert.equal(await evaluate('document.querySelector("#generation-original p").textContent'), requirement);
  assert.equal(await sandbox(), 'allow-scripts allow-forms allow-downloads');
  assert.equal((await library()).items.length, 2);
  assert.equal(await used(), before + 1);
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
  await click('#open-generated-tool');
  assert.equal(await visible('#requirement'), false);
  assert.equal((await activeTool()).id, board.id);
  assert.equal(await inBoard('document.querySelectorAll("[data-board-column=待投递] article").length'), 2);
  await click('[data-record="1"] [data-move-stage="面试中"]', board.context);
  await click('[data-edit-record="1"]', board.context);
  assert.equal(await inBoard('document.getElementById(' + JSON.stringify(link) + ').value'), record[link]);
  await fill({[role]:'现场复核'});
  await click('#add-record', board.context);
  assert.match(await inBoard('document.querySelector("[data-board-column=面试中] article").textContent'), /现场复核/);

  await inBoard('document.querySelector("#export-backup").scrollIntoView({block:"center"})');
  await pointerClick('#export-backup', board.context);
  evidence.tests.push('保存后留在生成页，预览可录入；点打开工具进入同一项');
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
  evidence.tests.push('新建前写入的旧工具记录在切换后保留；分栏、移动、编辑、删除及实际下载和导入恢复通过；导入无效、取消及服务拒绝均保留数据');

  assert.equal(await inBoard('(() => { try { return Boolean(parent.document); } catch { return false; } })()'), false);
  assert.equal(await inBoard('fetch(' + JSON.stringify(probeUrl + '/board-preview') + ',{mode:"no-cors"}).then(() => true, () => false)'), false);
  assert.deepEqual(probeRequests, probeControls);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  await click('#library-entry');
  const restored = await activeTool();
  assert.equal(restored.id, board.id);
  assert.equal(await sandbox(), 'allow-scripts allow-forms allow-downloads');
  assert.equal(await evaluate('document.querySelectorAll("#record-board article").length', restored.context), 2);
  assert.match(await evaluate('document.querySelector("[data-board-column=面试中] article").textContent', restored.context), /现场复核/);
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => item.task === publishingTask);
  assert.equal(evidence.runs.filter(item => item.stage === 'spec').length, 1);
  const expected = stagedLive ? ['spec','entry'] : ['spec','entry','filters','summary','export'];
  assert.deepEqual(evidence.runs.filter(item => item.status === 'ok').map(item => item.stage), expected);
  if (!stagedLive) {
    const rejection = receipts.find(item => item.task === first.id && item.validation === 'bad_field_reference');
    assert.equal(rejection?.reference_slot, 'board');
    assert.equal(rejection.reference_problem, 'unknown_id');
  }
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
  await click('#library-entry');
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

export async function verifyRunningLayout(ctx) {
  const { cdp, evaluate, until, base, click, visible, appRequest, login, generate, accountA, evidence, screenshot } = ctx;
  const input = '做一份简单读书清单，新增书名并保存。用于核对生成中的任务进度。';
  await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  await generate(input);
  const task = await until(() => findStagedTask(appRequest, input));
  const taskButton = '#staged-task-list button[data-task-id="' + task.id + '"]';
  const assertRunningLayout = async path => {
    for (const width of [1440, 390]) {
      await cdp('Emulation.setDeviceMetricsOverride', {width,height:844,deviceScaleFactor:1,mobile:width===390});
      const layout = await evaluate(`(() => {
        const composer = document.querySelector('.elys-composer');
        const preview = document.querySelector('.elys-preview');
        const progress = document.querySelector('#staged-steps li[aria-current=step]');
        return {
          mode: document.querySelector('#workspace').dataset.mode,
          sidebarFits: ${width} === 390 || (composer.clientWidth >= 260 && composer.clientWidth <= 360),
          previewVisible: preview.checkVisibility(),
          original: document.querySelector('#staged-original p').textContent,
          task: document.querySelector('#staged-task-list [aria-current=true]')?.dataset.taskId,
          step: progress?.checkVisibility(),
          fullForm: document.querySelector('#composer').checkVisibility(),
        };
      })()`);
      assert.deepEqual(layout, {mode:'compose',sidebarFits:true,previewVisible:true,original:input,
        task:task.id,step:true,fullForm:false}, path + ' ' + width + 'px');
      await screenshot('running-' + path + '-' + width);
    }
  };
  await until(() => visible('#staged-steps li[aria-current=step]'));
  await assertRunningLayout('submit');
  await click('#new-tool');
  assert.equal(await visible('#requirement'), true);
  await click(taskButton);
  await assertRunningLayout('reopen');
  const navigation = await evaluate('performance.timeOrigin');
  await cdp('Page.reload');
  await until(() => evaluate(`performance.timeOrigin !== ${navigation} && document.documentElement.dataset.ready === 'true'`));
  await assertRunningLayout('reload');
  assert.equal((await appRequest('/api/staged')).body.tasks.filter(item => item.requirement === input).length, 1);
  evidence.tests.push({id:'BLUE-02',task:task.id,paths:['submit','reopen','reload'],viewports:[1440,390]});
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
  assert.equal(await visible('#preview iframe[data-tool]'), true, '连接失败时保留可操作的上次工具');
  assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").inert'), false);
  assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").dataset.tool'), old.id);
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
    const task = await until(() => findStagedTask(appRequest, text), 12000);
    return until(async () => {
      const current = (await appRequest(taskPath + task.id)).body.task;
      return ['published','unsupported','failed','clarifying'].includes(current.state) && current;
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
  await generate('验收改为白色');
  const ambiguous = await settle('验收改为白色');
  assert.equal(ambiguous.reason, 'revision_clarification_required');
  await until(() => visible('#revision-clarification'));
  assert.match(await evaluate('document.querySelector("#revision-question").textContent'), /分栏的颜色.*字段显示名/);
  assert.equal(await visible('#revision-confirm'), false);
  assert.equal((await versions()).current, 1);
  assert.deepEqual((await stored(old.id)).entries, before.entries);
  assert.equal((await balance()).used, beforeBalance.used);
  assert.equal((await balance()).totalUsed, beforeBalance.totalUsed + 1);
  await screenshot('ambiguous-color-asks-target');
  await evaluate('document.querySelector("#revision-answer").value = "把颜色改成黄色"');
  await click('#revision-answer-submit');
  await until(() => visible('#revision-confirm'));
  assert.equal((await versions()).current, 1);
  assert.equal((await balance()).used, beforeBalance.used);
  await screenshot('theme-plan-before-confirm');
  await click('#revision-confirm');
  const revised = await settle('验收改为白色');
  assert.equal(revised.id, ambiguous.id);
  assert.equal(revised.state, 'published', revised.reason);
  assert.equal(revised.toolId, old.id);
  const history = await versions();
  assert.deepEqual(history.items.map(item => item.version), [2, 1]);
  assert.notEqual(history.items[0].commit, history.items[1].commit);
  assert.deepEqual((await stored(old.id)).entries, before.entries);
  assert.equal((await balance()).used, beforeBalance.used + 1);
  assert.equal((await balance()).totalUsed, beforeBalance.totalUsed + 2);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  await click('#library-entry');
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
    assert.equal(rejected.reason, 'staged_unsupported');
    await until(async () => /旧版仍可用.*未扣除/.test(await evaluate('document.querySelector("#staged-reason").textContent')));
    assert.deepEqual(await versions(), history);
    assert.deepEqual((await stored(old.id)).entries, before.entries);
    assert.equal((await balance()).used, beforeBalance.used + 1);
    assert.deepEqual(await board(await activeTool()), after);
    await screenshot(change.includes('22px') ? 'unchanged-theme' : 'unsupported-theme');
  }
  if (!live) {
    const beforeLocal = await balance();
    const localChange = '公司名称改为雇主';
    await click('#revise-tool');
    await generate(localChange);
    await until(() => visible('#revision-confirm'));
    assert.match(await evaluate('document.querySelector("#revision-plan-changes").textContent'), /公司名称.*雇主/);
    await screenshot('field-rename-cancel-plan');
    await click('#revision-cancel');
    await until(async () => !await visible('#revision-confirm') && await visible('#requirement'));
    await click('#return-to-library');
    assert.equal((await versions()).current, 2);
    assert.deepEqual((await stored(old.id)).entries, before.entries);
    assert.equal((await balance()).used, beforeLocal.used);
    assert.equal((await balance()).totalUsed, beforeLocal.totalUsed + 1);
    for (const [request, version] of [[localChange, 3], ['新增一个可空文本字段「项目」', 4],
      ['把「面试中」分栏背景改为浅蓝', 5]]) {
      await click('#revise-tool');
      await generate(request);
      await until(() => visible('#revision-confirm'));
      await screenshot('local-plan-v' + version);
      await click('#revision-confirm');
      await until(async () => (await versions()).current === version);
      await until(() => evaluate('document.querySelector("#revise-tool")?.checkVisibility()'
        + ' && !document.querySelector("#revise-tool").disabled'));
      await screenshot('local-applied-v' + version);
    }
    assert.deepEqual((await stored(old.id)).entries, before.entries);
    assert.equal((await balance()).used, beforeLocal.used + 3);
    assert.equal((await balance()).totalUsed, beforeLocal.totalUsed + 4);
    await cdp('Page.reload');
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    await click('#library-entry');
    const reopened = await activeTool();
    assert.equal(reopened.id, old.id);
    const finalBoard = await board(reopened);
    const column = finalBoard.columns.find(item => item.name === '面试中');
    assert.equal(column.background, 'rgb(219, 234, 254)');
    assert.match(column.cards.join(), /雇主.*合成甲公司.*项目/s);
    await screenshot('local-restored');
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
    '点击按钮 id=convert 进行换算，将可见结果写入 id=result。无效输入提示错误。无需联网，不保存任何数据。';
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
  if (process.env.ROOT_STAGED_SCRIPT_REVISION === '1') {
    await evaluate(`window.addEventListener('message', event => {
      if (event.data?.type === 'stateless-test-received') window.statelessTestReceived = true;
    })`);
    await evaluate(`(() => {
      const token = document.scripts[0].textContent.match(/[0-9a-f-]{36}/)?.[0];
      if (!token) throw new Error('missing bootstrap token');
      parent.postMessage({ type:'storage', token, entries:[['forged','record']] }, '*');
      parent.postMessage({ type:'stateless-test-received' }, '*');
    })()`, tool.context);
    await until(() => evaluate('window.statelessTestReceived === true'));
    assert.deepEqual((await appRequest('/api/tools/' + tool.id)).body.entries, []);
    const requirement = '改实际换算逻辑，摄氏和华氏的结果都只保留一位小数。保留所有输入与操作。';
    await click('#revise-tool');
    await generate(requirement);
    const task = await until(() => findStagedTask(appRequest, requirement));
    await until(async () => {
      const current = (await appRequest(taskPath + task.id)).body.task;
      if (['failed', 'unsupported'].includes(current.state)) {
        throw new Error('脚本修改未生成候选 ' + current.reason + ' / ' + (current.guidance || ''));
      }
      return visible('#revision-confirm');
    });
    assert.match(await evaluate('document.querySelector("#revision-plan-changes").textContent'), /交互脚本/);
    assert.equal((await appRequest('/api/tools/' + tool.id + '/versions')).body.current, 1);
    await click('#revision-confirm');
    const revised = await until(async () => {
      const current = await activeTool().catch(() => null);
      return current?.id === tool.id && current.loader !== tool.loader && current;
    });
    for (const [value, unit, expected] of [['100','celsius',/212\.0(?!\d)/], ['32','fahrenheit',/(^|\D)0\.0(?!\d)/]]) {
      await evaluate('document.getElementById("temperature").value=' + JSON.stringify(value) +
        ';document.getElementById("unit").value=' + JSON.stringify(unit) + ';', revised.context);
      await click('#convert', revised.context);
      assert.match(await evaluate('document.querySelector("#result").textContent', revised.context), expected);
    }
    assert.equal((await appRequest('/api/tools/' + tool.id + '/versions')).body.current, 2);
    evidence.tests.push({id:'TEST-007-script', sameTool:true, precision:1, originalConversionsPreserved:true});
    await screenshot('staged-calculator-revised');
  }
}

export async function verifyStagedRevision(ctx) {
  const { cdp, evaluate, until, base, click, type, visible, appRequest, login, generate,
    activeTool, accountA, evidence, screenshot, stored, anotherBrowser } = ctx;
  const change = '把页面文案「添加」改为「加入书单」';
  await cdp('Emulation.setDeviceMetricsOverride', {width:1360,height:980,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  if (process.env.ROOT_STAGED_LIVE === '1') {
    await evaluate(`document.querySelector('#generation-category').value = 'program'`);
  }
  await generate('做一份简单读书清单，新增书名并保存。只生成一个简单的清单程序，使用 localStorage 的 books 保存书名。请务必使用 id=add 且初始文案是添加的按钮，id=book 的书名输入，id=list 的书单。');
  let old = await activeTool();
  const toolPath = '/api/tools/' + old.id;
  const currentVersion = async () => (await appRequest(toolPath + '/versions')).body.current;
  const trial = async () => (await appRequest('/api/trial')).body.generation;
  async function add(title, context = old.context) {
    await evaluate('document.querySelector("#book").value=""', context);
    await evaluate('document.querySelector("#book").value=' + JSON.stringify(title), context);
    await click('#add', context);
    await until(async () => JSON.stringify((await stored(old.id)).entries).includes(title))
      .catch(error => { throw new Error('旧工具添加后未保存：' + title, {cause:error}); });
  }
  await add('甲 旧版已有');
  assert.equal((await appRequest('/api/profile', {nickname:'验收昵称'})).status, 200);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  await click('#library-entry');
  await until(() => visible('#revise-tool'));
  old = await activeTool();
  const before = await trial();
  await click('#revise-tool');
  assert.equal(await visible('#preview-view svg'), true, '修改页默认显示带眼睛图标的预览入口');
  for (const width of [1440, 390]) {
    await cdp('Emulation.setDeviceMetricsOverride', {width,height:844,deviceScaleFactor:1,mobile:width===390});
    assert.deepEqual(await evaluate(`(() => {
      const dialog = document.querySelector('.elys-composer');
      const preview = document.querySelector('.elys-preview');
      const left = dialog.getBoundingClientRect();
      const right = preview.getBoundingClientRect();
      return {
        theme: getComputedStyle(dialog).backgroundColor,
        viewsFit: document.querySelector('#revision-views').scrollWidth ===
          document.querySelector('#revision-views').clientWidth,
        layout: ${width} === 390 ? right.top >= left.bottom && left.left === right.left : right.left >= left.right,
        horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
      };
    })()`), {theme:'rgb(237, 241, 248)',viewsFit:true,layout:true,horizontalOverflow:false});
    await screenshot('revision-layout-' + width);
  }
  await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:980,deviceScaleFactor:1,mobile:false});
  await click('#source-view');
  await until(() => visible('#source-files'));
  assert.match(await evaluate('document.querySelector("#source-code").textContent'), /添加/);
  assert.equal(await evaluate('document.querySelector("#source-code").children.length'), 0);
  await click('#preview-view');
  await generate(change);
  await until(() => visible('#revision-confirm'));
  assert.equal(await visible('.elys-preview'), true);
  assert.equal(await currentVersion(), 1);
  assert.equal(await evaluate('document.querySelector("#tool-revision-log [data-role=user] .revision-author").textContent'), '验收昵称');
  assert.match(await evaluate('document.querySelector("#revision-plan-tool .revision-author").textContent'), /小 R/);
  assert.equal(await visible('#candidate-badge'), true);
  const inspected = await activeTool('', '#preview iframe[data-revision-inspection]');
  assert.equal(await evaluate('document.querySelector("#add").textContent', inspected.context), '加入书单');
  assert.equal(await evaluate('document.querySelector("[data-revision-inspection]").inert'), true);
  await click('#source-view');
  await until(() => evaluate('document.querySelector("#source-code").textContent.includes("加入书单")'));
  assert.match(await evaluate('document.querySelector("#source-status").textContent'), /尚未应用/);
  assert.deepEqual(await evaluate('Array.from(document.querySelector("#source-files").options, o => o.textContent)'),
    ['app.html', 'implementation.json', 'spec.json']);
  await evaluate('document.querySelector("#source-files").value="1"; document.querySelector("#source-files").dispatchEvent(new Event("change"));');
  assert.match(await evaluate('document.querySelector("#source-code").textContent'), /加入书单/);
  await screenshot('revision-source-pending');
  await click('#tool-history');
  await until(() => evaluate('document.querySelectorAll("#tool-version-list li").length === 1'));
  assert.equal(await visible('#requirement'), true);
  await click('#preview-view');
  await screenshot('program-plan-before-concurrent-write');
  await anotherBrowser('staged-revision-second', async () => {
    await cdp('Emulation.setDeviceMetricsOverride', {width:1360,height:980,deviceScaleFactor:1,mobile:false});
    await cdp('Page.navigate', {url:base});
    await login(accountA, false, '验收昵称');
    if (await visible('.elys-composer')) await click('#library-entry');
    const other = await activeTool();
    assert.equal(other.id, old.id);
    await add('乙 方案展示后写入', other.context);
  });
  await click('#revision-confirm');
  await until(async () => /重新查看并确认/.test(await evaluate('document.querySelector("#staged-reason").textContent')));
  assert.equal(await currentVersion(), 1);
  assert.equal((await activeTool()).loader, old.loader);
  assert.equal((await trial()).used, before.used);
  assert.equal((await trial()).totalUsed, before.totalUsed + 1);
  await screenshot('program-concurrent-confirmation-rejected');
  await click('#staged-preview');
  await until(() => visible('#revision-confirm'));
  await screenshot('program-plan-reconfirmed');
  await click('#revision-confirm');
  const revised = await until(async () => {
    const current = await activeTool().catch(() => null);
    return current && current.id === old.id && current.loader !== old.loader && current;
  }, 15000);
  assert.equal(await currentVersion(), 2);
  assert.equal(await evaluate('document.querySelector("#add").textContent', revised.context), '加入书单');
  const text = await evaluate('document.querySelector("#list").textContent', revised.context);
  assert.match(text, /甲 旧版已有.*乙 方案展示后写入/s);
  await click('#source-view');
  await until(() => evaluate('document.querySelector("#source-status").textContent === "当前 v2"'));
  assert.match(await evaluate('document.querySelector("#source-code").textContent'), /加入书单/);
  await click('#preview-view');
  await add('丙 修改后继续新增', (await activeTool()).context);
  assert.equal((await trial()).used, before.used + 1);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  if (await visible('.elys-composer')) await click('#library-entry');
  assert.equal((await activeTool()).id, old.id);
  assert.equal(await currentVersion(), 2);
  await click('#revise-tool');
  await until(() => evaluate('document.querySelector("#tool-revision-log").textContent.includes("加入书单")'));
  assert.match(await evaluate('document.querySelector("#tool-revision-log").textContent'), /已应用/);
  await click('#tool-history');
  await until(() => evaluate('document.querySelectorAll("#tool-version-list li").length === 2'));
  assert.deepEqual(await evaluate('Array.from(document.querySelectorAll("#tool-version-list strong"), item => item.textContent)'), ['v2 · 当前', 'v1']);
  await click('#preview-view');
  assert.equal(await visible('#staged-history'), false);
  if (process.env.ROOT_STAGED_LIVE !== '1') {
    await generate('把按钮文案从加入书单改为待取消');
    await until(() => visible('#revision-confirm'));
    await click('#source-view');
    await until(() => evaluate('document.querySelector("#source-code").textContent.includes("待取消")'));
    await click('#revision-cancel');
    await until(() => evaluate('document.querySelector("#source-status").textContent === "当前 v2"'));
    assert.match(await evaluate('document.querySelector("#source-code").textContent'), /加入书单/);
    assert.equal(await evaluate('document.querySelector("#candidate-badge").dataset.candidate'), 'false');
    assert.match(await evaluate('document.querySelector("#tool-revision-log [data-role=system]").textContent'), /系统提示/);
    assert.equal(await currentVersion(), 2);
    await screenshot('revision-source-cancelled');
    await click('#preview-view');
    const failedChange = '把按钮文案改为收藏，模拟补丁失配';
    await generate(failedChange);
    const failed = await until(async () => {
      const {body} = await appRequest(toolPath + '/revision-tasks');
      return body.tasks.find(task => task.requirement === failedChange && task.state === 'failed');
    });
    await cdp('Page.reload');
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    if (await visible('.elys-composer')) await click('#library-entry');
    assert.equal((await activeTool()).id, old.id);
    await until(() => evaluate('!document.querySelector("#revise-tool").disabled'));
    await click('#revise-tool');
    await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
    const action = '#tool-revision-log button[data-revision-task="' + failed.id + '"]';
    await until(() => visible(action)).catch(error => {
      throw new Error('重开后未显示失败消息操作：' + error.message);
    });
    await click(action);
    await until(() => visible('#revision-abandon'));
    await click('#revision-repair');
    await until(() => visible('#revision-confirm'));
    await click('#revision-confirm');
    await until(() => visible('#revision-apply-repair'), 30000);
    assert.equal(await currentVersion(), 2);
    assert.equal((await trial()).used, before.used + 1);
    const trialFrame = await activeTool('', '#preview iframe[data-repair-trial]');
    const beforeTrial = (await stored(old.id)).entries;
    await type('#book', '只在候选里试填', trialFrame.context);
    await click('#add', trialFrame.context);
    await until(() => evaluate('document.querySelector("#list").textContent.includes("只在候选里试填")', trialFrame.context));
    assert.deepEqual((await stored(old.id)).entries, beforeTrial);
    await screenshot('staged-repair-trial');
    await click('#revision-apply-repair');
    await until(async () => await currentVersion() === 3, 30000);
    const repaired = await until(async () => {
      const current = await activeTool();
      return await evaluate('document.querySelector("#add").textContent', current.context) === '收藏' && current;
    });
    assert.match(await evaluate('document.querySelector("#list").textContent', repaired.context), /丙 修改后继续新增/);
    assert.doesNotMatch(await evaluate('document.querySelector("#list").textContent', repaired.context), /只在候选里试填/);
    await until(async () => (await trial()).used === before.used + 2);
    evidence.tests.push({id:'TEST-007-REPAIR-03', failedConversationRestored:true, trialBeforeApply:true, applied:true});
  }
  evidence.tests.push({id:'TEST-007-program-copy', sameTool:true, staleConfirmationRejected:true,
    olderAndConcurrentRecordsPreserved:true, originalActionVerified:true, chargedOnce:true, conversationRestored:true});
  await screenshot('staged-revision-restored');
}

export async function verifyStagedQuotaFeedback(ctx) {
  const { cdp, evaluate, until, base, click, visible, appRequest, login, generate,
    activeTool, accountA, evidence, screenshot } = ctx;
  const original = '做一份简单读书清单，新增书名并保存。';
  const next = '做一个 AA 分账计算器，根据总金额和人数计算人均费用。';
  const text = id => evaluate('document.getElementById(' + JSON.stringify(id) + ').textContent');
  await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await generate(original);
  const old = await activeTool();
  const before = (await appRequest('/api/trial')).body.generation;
  assert.equal(before.remaining, 1);
  assert.equal(before.totalRemaining, 0);
  await click('#new-tool');
  await generate(next);
  const task = await until(async () => {
    const { body } = await appRequest('/api/staged/history');
    return body.tasks.find(item => item.requirement === next && item.state === 'failed');
  });
  assert.equal(task.reason, 'generation_total_limit');
  assert.match(task.issue, /^[a-f0-9]{12}$/);
  await until(() => visible('#staged-reason'));
  assert.equal(await text('generation-balance'), '个人生成额度剩余 1 / 2 次');
  assert.ok(await visible('#generation-block'));
  assert.match(await text('generation-block'), /全站共享的生成额度已用完/);
  assert.equal(await text('staged-status'), '全站共享的生成额度已用完');
  assert.match(await text('staged-reason'), /所有账号共用.*联系管理员/);
  assert.equal(await visible('#staged-issue'), false);
  assert.equal(await visible('#staged-resume'), false);
  assert.equal(await evaluate('document.querySelector("#requirement").value'), next);
  assert.equal(await visible('.elys-preview'), false);
  assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").dataset.tool'), old.id);
  assert.deepEqual((await appRequest('/api/trial')).body.generation, before);
  await screenshot('shared-generation-quota-narrow');
  evidence.tests.push({id:'TEST-017-06', personalRemaining:before.remaining, siteRemaining:before.totalRemaining});
}

export async function verifyStagedHistory(ctx) {
  const { cdp, evaluate, until, base, click, type, visible, appRequest, login, generate,
    activeTool, accountA, root, stored, evidence, screenshot } = ctx;
  const initial = '做一份简单读书清单，新增书名并保存。';
  const failedRequirement = '做一个小工具，历史验证首次失败';
  const revisedRequirement = '把页面文案「不存在的提示」改为「加入书单」';
  const balance = async () => (await appRequest('/api/trial')).body.generation;
  const waitTask = (id, state) => until(async () => {
    const {body:{task}} = await appRequest(taskPath + id);
    return task.state === state && task;
  });
  const historyButton = id => '#staged-history-list button[data-history-task-id="' + id + '"]';
  const bringBack = async id => {
    await click('#staged-history summary');
    await until(() => visible(historyButton(id)));
    await click(historyButton(id));
  };
  const fixtureSql = script => execFileSync('python3', ['-B', '-c', script,
    join(root, 'runs', 'workspace.sqlite3'), accountA], {encoding:'utf8'}).trim();
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await generate(initial);
  const old = await activeTool();
  await type('#book', '原工具的数据', old.context);
  await click('#add', old.context);
  await until(async () => JSON.stringify((await stored(old.id)).entries).includes('原工具的数据'));
  const repo = () => fixtureSql(`
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    print(db.execute("SELECT repo_id FROM tool_git_repositories WHERE tool_id=?", (${JSON.stringify(old.id)},)).fetchone()[0])
`);
  const originalRepo = repo();
  const oldBalance = await balance();
  const ready = (await appRequest('/api/generate', {
    requirement:'另一个等待预览的任务', requestId:'4'.repeat(32),
  })).body.task;
  await waitTask(ready.id, 'complete');

  await click('#new-tool');
  await generate(failedRequirement);
  const failed = await until(() => findStagedTask(appRequest, failedRequirement));
  await waitTask(failed.id, 'failed');
  await until(() => visible('#staged-reason'));
  assert.equal(await evaluate('document.querySelector("#requirement").value'), failedRequirement);
  assert.equal(await visible('#staged-resume'), false);
  assert.equal((await balance()).used, oldBalance.used);
  assert.equal(await visible('.elys-preview'), false);
  assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").dataset.tool'), old.id);
  assert.equal((await appRequest('/api/tools')).body.items.length, 1);
  fixtureSql(`
import sqlite3, sys, time, uuid
with sqlite3.connect(sys.argv[1]) as db:
    owner = db.execute("SELECT id FROM accounts WHERE username=?", (sys.argv[2],)).fetchone()[0]
    now = time.time()
    for index in range(36):
        expiry = now - 20 if index % 2 else now + 1000
        db.execute("INSERT INTO staged_jobs(id,owner,request_id,requirement,head,expires,state,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex, owner, uuid.uuid4().hex, f'旧失败 {index}', '0'*40, expiry, 'failed', now-30-index))
`);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  await click('#new-tool');
  const listed = (await appRequest('/api/staged')).body.tasks;
  assert.deepEqual(listed.map(task => [task.id, task.state]), [[ready.id, 'complete']]);
  assert.equal(await visible('#staged-task-list button[data-task-id="' + ready.id + '"]'), true);
  const beforeBringBack = await balance();
  await bringBack(failed.id);
  assert.equal(await evaluate('document.querySelector("#requirement").value'), failedRequirement);
  assert.equal(await visible('#staged-resume'), false);
  assert.deepEqual(await balance(), beforeBringBack);
  await screenshot('failure-history-brought-back');
  const edited = '做一个小工具，历史验证重新提交';
  await generate(edited);
  const tool = await activeTool(old.id);
  const published = (await appRequest(taskPath + failed.id)).body.task;
  assert.equal(published.state, 'failed');
  assert.equal((await appRequest('/api/tools')).body.items.length, 2);
  assert.equal((await balance()).used, oldBalance.used + 1);

  const previous = (await appRequest('/api/tools/' + old.id + '/revise', {
    requirement:revisedRequirement, version:1, requestId:'5'.repeat(32),
  })).body.task;
  await waitTask(previous.id, 'unsupported');
  await click('#new-tool');
  const beforeRevise = await balance();
  await bringBack(previous.id);
  await until(async () => (await activeTool()).id === old.id);
  assert.equal(await evaluate('document.querySelector("#requirement").value'), revisedRequirement);
  assert.equal(await evaluate('document.querySelector("#generate").textContent'), '查看修改方案');
  assert.deepEqual(await balance(), beforeRevise);
  await evaluate(`(() => {
    const original = window.fetch;
    window.fetch = async (path, options) => {
      if (options?.method === 'POST' && String(path).endsWith('/publish')) {
        const response = await original(path, options);
        window.historyPublishedPath = String(path).replace(/\\/publish$/, '');
        if (response.ok) throw new TypeError('替身模拟保存响应丢失');
        return response;
      }
      if (path === window.historyPublishedPath && !options?.method && !window.historyAllowRead) {
        throw new TypeError('替身模拟暂时无法读取保存结果');
      }
      return original(path, options);
    };
  })()`);
  const revised = '把页面文案「添加」改为「加入书单」';
  await generate(revised);
  await until(() => visible('#revision-confirm'));
  await click('#revision-confirm');
  const pendingTaskPath = await until(async () => {
    if (!await visible('#staged-sync')) return;
    return evaluate('!document.querySelector("#staged-sync").disabled && window.historyPublishedPath');
  });
  await evaluate('window.historyAllowRead = true');
  await click('#staged-sync');
  await until(async () => (await appRequest(pendingTaskPath)).body.task.state === 'published' && !await visible('#staged-sync'));
  const {body:{task:current}} = await appRequest(pendingTaskPath);
  assert.equal(current.toolId, old.id);
  assert.notEqual(current.id, previous.id);
  assert.equal((await appRequest('/api/tools/' + old.id + '/versions')).body.current, 2);
  assert.equal(repo(), originalRepo);
  assert.equal((await appRequest('/api/tools')).body.items.length, 2);
  assert.match(JSON.stringify((await stored(old.id)).entries), /原工具的数据/);
  assert.equal((await balance()).used, beforeRevise.used + 1);
  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  assert.deepEqual(receipts.filter(item => item.task === failed.id).map(item => item.stage), ['spec','implementation']);
  assert.deepEqual(receipts.filter(item => item.task === current.id).map(item => item.stage), ['revision-plan']);
  assert.notEqual(tool.id, old.id);
  evidence.tests.push({id:'TEST-018-09', historyLimitSeparate:true, failedPersonalCharge:0,
    newTool:tool.id, revisedTool:old.id, originalRepository:originalRepo, savedOperation:current.id});
}
