// TEST-001 / TEST-002. Default uses a deterministic provider fixture. --live uses DeepSeek.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const live = process.argv.includes('--live');
const mainOnly = process.argv.includes('--main');
const artifactsArg = process.argv.find(arg => arg.startsWith('--artifacts='));
let artifacts = artifactsArg?.slice('--artifacts='.length);
const app = process.env.WORKBENCH_URL || 'http://127.0.0.1:8765';
const root = await mkdtemp(join(tmpdir(), 'root-workbench-'));
const profile = join(root, 'chrome');
const evidence = { mode: live ? 'deepseek' : 'fixture', tests: [], runs: [] };
const probeRequests = [];
const probeControls = ['/control', '/control-browser'];
let probe;
let probeUrl;
const runtimeErrors = [];
let stage = 'startup';
const children = [];
let socket;
let readPage;

function launch(command, args) {
  const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  children.push(child);
  child.stderr.on('data', data => process.stderr.write(data));
  return child;
}

async function until(check, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await delay(100);
  }
  throw new Error('等待浏览器状态超时');
}

const requirements = [
  '请做一个 h1 为「读书清单」的工具。首次列表为空。用 form 包含 id="book" 的书名输入和 type="submit" 的「添加书籍」按钮，使用可见的 ul#list 显示书名。提交后清空输入。请使用 localStorage 保存并恢复书单。',
  '请做一个 h1 为「搬家预算计算器」的工具，包含 id="truck" 的租车费数字输入和 id="boxes" 的纸箱费数字输入，点击 id="calculate" 的「计算总额」按钮时将两项相加，把纯数字结果显示到 #total。',
].map(requirement => live
  ? requirement + ' 采用最简界面，控制输出在 2500 tokens 以内，返回完整 HTML。'
  : requirement);

function postRequirement(base, requirement) {
  return fetch(base + '/api/generate', {
    method: 'POST',
    headers: { Origin: base, 'Content-Type': 'application/json' },
    body: JSON.stringify({ requirement }),
    signal: AbortSignal.timeout(5000),
  });
}

try {
  let base = app;
  if (!live) {
    probe = createServer((request, response) => {
      probeRequests.push(request.url);
      response.writeHead(200, { 'Content-Type': 'text/html', 'Access-Control-Allow-Origin': '*' });
      response.end('<!doctype html><title>Reachable HTTP probe</title><link rel="icon" href="data:,">');
    });
    probe.listen(0, '127.0.0.1');
    await once(probe, 'listening');
    probeUrl = 'http://127.0.0.1:' + probe.address().port;
    assert.equal((await fetch(probeUrl + '/control')).status, 200);
    const server = launch('python3', ['tests/fixtures.py', 'fixture-key', '0', join(root, 'runs'), probeUrl]);
    const [line] = await Promise.race([once(server.stdout, 'data'), once(server, 'exit').then(() => [''])]);
    base = String(line).match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
    assert.ok(base, '工作台应启动并提供本机地址');
  }
  const config = await (await fetch(base + '/api/config', { signal: AbortSignal.timeout(5000) })).json();
  evidence.build = config.build;
  assert.equal(config.configured, true, '需要配置本地工作台的 DeepSeek Key');
  assert.deepEqual(Object.keys(config).sort(), ['build', 'configured']);
  if (!live) {
    const invalidOrigin = await fetch(base + '/api/generate', {
      method: 'POST', headers: { Origin: 'null', 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirement: '来自隔离预览的请求' }),
    });
    assert.equal(invalidOrigin.status, 403);
    assert.equal((await invalidOrigin.json()).code, 'invalid_origin');
    const cases = [
      ['   ', 400, 'invalid_requirement', /请输入/],
      ['长'.repeat(2001), 400, 'invalid_requirement', /请输入/],
      ['回归：模型超时', 502, 'request_failed', /生成超时/],
      ['回归：模型限流', 502, 'http_error', /请求过多/],
      ['回归：余额不足', 502, 'http_error', /余额不足/],
    ];
    for (const [requirement, status, code, message] of cases) {
      const response = await postRequirement(base, requirement);
      assert.equal(response.status, status);
      const body = await response.json();
      assert.equal(body.code, code);
      assert.match(body.message, message);
    }
    evidence.tests.push({ id: 'request-guards', result: 'passed',
      cases: ['blank', 'over-length', 'opaque-origin', 'provider timeout', 'provider 429', 'provider 402'] });
  }
  if (live && !artifacts) {
    artifacts = await mkdtemp(join(resolve('runs'), 'workbench-live-'));
  }
  if (artifacts) await mkdir(artifacts, { recursive: true, mode: 0o700 });
  const chrome = launch(process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
    '--headless=new', '--no-first-run', '--disable-background-networking', '--disable-extensions',
    '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank',
  ]);
  const port = await until(async () => {
    assert.equal(chrome.exitCode, null);
    return (await readFile(join(profile, 'DevToolsActivePort'), 'utf8').catch(() => '')).split('\n')[0];
  });
  const tabs = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
  await once(socket, 'open');
  let sequence = 0;
  const pending = new Map();
  const iframeSessions = new Map();
  function cdp(method, params = {}, sessionId) {
    const id = ++sequence;
    return new Promise((resolveResult, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(method + ' timed out'));
      }, 10000);
      pending.set(id, reply => {
        clearTimeout(timer);
        if (reply.error) reject(new Error(reply.error.message));
        else resolveResult(reply.result);
      });
      socket.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }
  socket.addEventListener('message', event => {
    const reply = JSON.parse(event.data);
    if (reply.method === 'Target.attachedToTarget' && reply.params.targetInfo.type === 'iframe') {
      iframeSessions.set(reply.params.targetInfo.targetId, reply.params.sessionId);
    }
    if (reply.method === 'Target.detachedFromTarget') {
      iframeSessions.delete(reply.params.targetId);
    }
    if (reply.method === 'Runtime.exceptionThrown') runtimeErrors.push(reply.params.exceptionDetails.exception?.description);
    const callback = pending.get(reply.id);
    pending.delete(reply.id);
    callback?.(reply);
  });

  async function evaluate(expression, context) {
    const result = await cdp('Runtime.evaluate', {
      expression, contextId: context?.id, returnByValue: true,
    }, context?.session);
    if (result.exceptionDetails) throw new Error(result.result.description || result.exceptionDetails.text);
    return result.result.value;
  }

  readPage = () => evaluate(`({
    phase: document.querySelector('#phase')?.textContent,
    status: document.querySelector('#preview-status')?.textContent,
    error: document.querySelector('#error')?.textContent,
    tool: document.querySelector('#preview iframe[data-tool]')?.dataset.tool,
    disabled: document.querySelector('#generate')?.disabled,
  })`);

  async function activeTool(previousId = '') {
    const selector = '#preview iframe[data-tool]';
    return until(async () => {
      const id = await evaluate(`document.querySelector('${selector}')?.dataset.tool`);
      if (!id || id === previousId) {
        const error = await evaluate('document.querySelector("#error")?.textContent');
        if (error) throw new Error(error);
        return;
      }
      const { root } = await cdp('DOM.getDocument');
      const { nodeId } = await cdp('DOM.querySelector', { nodeId: root.nodeId, selector });
      if (!nodeId) return;
      const { node } = await cdp('DOM.describeNode', { nodeId });
      const session = iframeSessions.get(node.frameId);
      const { frameTree } = await cdp('Page.getFrameTree', {}, session);
      const frame = session ? frameTree.frame
        : frameTree.childFrames?.find(child => child.frame.id === node.frameId)?.frame;
      if (frame?.url !== 'about:srcdoc') return;
      const { executionContextId } = await cdp('Page.createIsolatedWorld', {
        frameId: frame.id, worldName: 'workbench-test',
      }, session);
      return { id, context: { id: executionContextId, session }, loader: frame.loaderId };
    }, live ? 135000 : 5000);
  }

  async function type(selector, value, context) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`, context);
    await cdp('Input.insertText', { text: value });
  }

  async function click(selector, context) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`, context);
  }

  async function screenshot(name) {
    if (!artifacts) return;
    const { data } = await cdp('Page.captureScreenshot', { format: 'png' });
    await writeFile(join(artifacts, name + '.png'), Buffer.from(data, 'base64'));
  }

  async function recordRun(tool) {
    const data = await readFile(join(live ? resolve('runs') : join(root, 'runs'), tool.id, 'generation.json'), 'utf8');
    const run = JSON.parse(data);
    assert.equal(run.http_status, 200);
    assert.equal(run.status, 'generated');
    assert.equal(run.returned_model, 'deepseek-flash');
    assert.ok(run.request_id);
    assert.equal(run.request_id.startsWith('fixture-'), !live);
    if (live) assert.ok(run.usage?.total_tokens > 0, '真实请求应提供用量证据');
    evidence.runs.push({ tool: tool.id, requestId: run.request_id, model: run.returned_model,
      elapsedSeconds: run.elapsed_seconds, usage: run.usage });
  }

  async function generate(requirement) {
    await evaluate('document.querySelector("#requirement").value = ""');
    await type('#requirement', requirement);
    await click('#generate');
  }

  await cdp('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
  await cdp('Page.enable');
  await cdp('Runtime.enable');
  evidence.browser = (await cdp('Browser.getVersion')).product;
  if (!live) {
    await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `
      if (window === top) {
        window.addEventListener('message', event => {
          if (event.origin === 'null' && event.data?.type === 'storage') {
            window.fixtureLastStorage = { source: event.source, token: event.data.token };
          }
          if (!window.holdFixturePreview || event.data?.type !== 'ready') return;
          event.stopImmediatePropagation();
          window.releaseFixturePreview = () => {
            window.holdFixturePreview = false;
            window.dispatchEvent(new MessageEvent('message', {
              data: event.data, origin: event.origin, source: event.source,
            }));
          };
        });
      }
    ` });
    await cdp('Page.navigate', { url: probeUrl + '/control-browser' });
    await until(() => evaluate('document.title === "Reachable HTTP probe"'));
    assert.deepEqual(probeRequests, probeControls);
  }
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
  await cdp('Page.navigate', { url: base });
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  if (!live) await screenshot('initial');
  assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
  await type('#requirement', '   ');
  assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
  await click('#example');
  assert.match(await evaluate('document.querySelector("#requirement").value'), /读书清单/);
  assert.equal(await evaluate('document.querySelector("#preview iframe") === null'), true);
  await evaluate('document.querySelector("#requirement").value = ""');
  await type('#requirement', requirements[0]);
  assert.equal(await evaluate('document.querySelector("#generate").disabled'), false,
    '输入新需求后应允许点击生成');

  stage = 'generate reading tool';
  await click('#generate');
  const first = await activeTool();
  await recordRun(first);
  assert.match(await evaluate('document.querySelector("h1").innerText', first.context), /读书清单/);
  const sample = '星际旅行手记';
  stage = 'add reading record';
  await type('#book', sample, first.context);
  await click('form button', first.context);
  await until(() => evaluate(`document.querySelector("#list").innerText.includes("${sample}")`, first.context));
  await until(() => evaluate(`localStorage.getItem("root:data:${first.id}")?.includes("${sample}")`));
  await screenshot('record-added');
  if (!live) {
    assert.equal(await evaluate('document.body.dataset.parentAccess', first.context), 'blocked');
    assert.equal(await evaluate('document.body.dataset.network', first.context), 'blocked');
    assert.deepEqual(probeRequests, probeControls);
  }
  stage = 'reload workbench';
  await cdp('Page.reload', { ignoreCache: true });
  await until(() => evaluate('performance.getEntriesByType("navigation")[0]?.type === "reload"'));
  let restored = await activeTool();
  assert.equal(restored.id, first.id);
  assert.notEqual(restored.loader, first.loader);
  assert.match(await evaluate('document.querySelector("#list").innerText', restored.context), /星际旅行手记/);
  evidence.tests.push({ id: 'TEST-002', result: 'passed', tool: first.id, record: sample,
    reloadedDocument: true, isolationFixtureChecked: !live });
  await screenshot('record-restored');
  async function assertOldTool() {
    assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").dataset.tool'), restored.id);
    assert.equal(await evaluate('localStorage.getItem("root:latest-tool")'), restored.id);
    assert.equal(await evaluate('document.querySelector("#preview-status").textContent'), '上次工具仍可用');
    assert.equal(await evaluate('document.querySelector("#requirement").value'), requirements[1]);
    assert.match(await evaluate('document.querySelector("#list").innerText', restored.context), /星际旅行手记/);
  }

  async function assertFailure(message) {
    const observed = await until(() => evaluate('document.querySelector("#error").textContent'));
    assert.match(observed, message);
    assert.equal(await evaluate('document.querySelector("#generate").textContent'), '重试');
    await assertOldTool();
    assert.equal(await evaluate('document.querySelectorAll("#preview iframe").length'), 1);
    assert.equal(await evaluate('Object.keys(localStorage).filter(key => key.startsWith("root:data:")).length'), 1);
  }

  async function addOldRecord(name) {
    await type('#book', name, restored.context);
    const button = await evaluate(`(() => {
      const rect = document.querySelector('form button').getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    })()`, restored.context);
    const frame = await evaluate(`(() => {
      const rect = document.querySelector('#preview iframe[data-tool]').getBoundingClientRect();
      return { x: rect.x, y: rect.y };
    })()`);
    const point = { x: frame.x + button.x, y: frame.y + button.y, button: 'left', clickCount: 1 };
    await cdp('Input.dispatchMouseEvent', { ...point, type: 'mousePressed', buttons: 1 });
    await cdp('Input.dispatchMouseEvent', { ...point, type: 'mouseReleased', buttons: 0 });
    assert.ok(await evaluate('document.querySelector("#list").innerText.includes(' + JSON.stringify(name) + ')', restored.context));
    await until(() => evaluate('localStorage.getItem(' + JSON.stringify('root:data:' + restored.id)
      + ')?.includes(' + JSON.stringify(name) + ')'));
  }

  if (!mainOnly) {
    stage = 'generate budget tool';
    await generate(requirements[1]);
    if (!live) {
      stage = 'model failure retains editable old tool';
      await assertFailure(/模型输出不完整/);
      await addOldRecord('失败后新增的书');
      await screenshot('failure-with-old-tool');

      stage = 'reload after failure before candidate switch';
      const previousLoader = restored.loader;
      await cdp('Page.reload', { ignoreCache: true });
      await until(() => evaluate('performance.getEntriesByType("navigation")[0]?.type === "reload"'));
      restored = await activeTool();
      assert.equal(restored.id, first.id);
      assert.notEqual(restored.loader, previousLoader);
      const afterFailure = await evaluate('document.querySelector("#list").innerText', restored.context);
      assert.match(afterFailure, /星际旅行手记/);
      assert.match(afterFailure, /失败后新增的书/);
      await screenshot('restored-after-failure');
      evidence.tests.push({ id: 'reload-after-failure', result: 'passed', tool: restored.id,
        reloadedDocument: true, records: ['星际旅行手记', '失败后新增的书'] });

      stage = 'candidate initialization failure';
      await generate(requirements[1]);
      await assertFailure(/预览初始化失败/);

      stage = 'candidate persistence failure';
      await evaluate(`(() => {
        const save = Storage.prototype.setItem;
        Storage.prototype.setItem = function (key, value) {
          if (key === 'root:latest-tool') throw new DOMException('fixture quota', 'QuotaExceededError');
          return save.call(this, key, value);
        };
        window.restoreFixtureStorage = () => { Storage.prototype.setItem = save; };
      })()`);
      await click('#generate');
      await assertFailure(/无法保存工具/);
      await evaluate('window.restoreFixtureStorage()');

      stage = 'retry keeps old tool usable in both phases';
      await evaluate('window.holdFixturePreview = true');
      await click('#generate');
      await until(() => evaluate('document.querySelector("#phase").textContent === "正在等待 DeepSeek 响应"'));
      assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
      await assertOldTool();
      stage = 'add during model wait';
      await until(() => readFile(join(root, 'model-started')).then(() => true, () => false));
      const duplicate = await postRequirement(base, requirements[1]);
      assert.equal(duplicate.status, 409);
      assert.equal((await duplicate.json()).code, 'busy');
      evidence.tests.push({ id: 'concurrent-generation', result: 'passed', secondRequestStatus: 409 });
      await addOldRecord('等待模型时新增的书');
      await writeFile(join(root, 'release-model'), '');
      stage = 'wait for candidate preview phase';
      await until(() => evaluate('document.querySelector("#phase").textContent === "正在准备预览"'));
      assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
      await assertOldTool();
      assert.equal(await evaluate('document.querySelectorAll("#preview iframe").length'), 2);
      stage = 'add during candidate preview';
      await addOldRecord('准备预览时新增的书');
      const forged = await evaluate(`(() => {
        const previous = window.fixtureLastStorage;
        const key = 'root:data:' + localStorage.getItem('root:latest-tool');
        const before = localStorage.getItem(key);
        const payload = { type: 'storage', token: previous.token, entries: [['books', 'forged']] };
        const attempts = [
          { source: previous.source, origin: location.origin, data: payload },
          { source: previous.source, origin: 'null', data: { ...payload, token: 'wrong-token' } },
          { source: window, origin: 'null', data: payload },
        ];
        window.fixtureDiscarded = { ...previous, key };
        return attempts.map(options => {
          window.dispatchEvent(new MessageEvent('message', options));
          return localStorage.getItem(key) === before;
        });
      })()`);
      assert.deepEqual(forged, [true, true, true]);
      await until(() => evaluate('typeof window.releaseFixturePreview === "function"'));
      await evaluate('window.releaseFixturePreview()');
      evidence.tests.push({ id: 'failure-and-retry', result: 'passed',
        preserved: ['requirement', 'active tool', 'existing and newly added records'],
        failures: ['incomplete output', 'preview initialization', 'browser persistence'],
        interaction: ['after failure', 'waiting for model', 'preparing candidate'] });
    }

    stage = 'switch to ready budget tool and calculate';
    const second = await activeTool(first.id);
    await recordRun(second);
    assert.match(await evaluate('document.querySelector("h1").innerText', second.context), /搬家预算计算器/);
    await evaluate(`
      document.querySelector('#truck').value = '';
      document.querySelector('#boxes').value = '';
    `, second.context);
    await type('#truck', '320', second.context);
    await type('#boxes', '85', second.context);
    assert.deepEqual(await evaluate(`
      ['#truck', '#boxes'].map(selector => document.querySelector(selector).value)
    `, second.context), ['320', '85']);
    await click('#calculate', second.context);
    assert.equal(await evaluate('document.querySelector("#total").innerText.trim()', second.context), '405');
    assert.equal(await evaluate('localStorage.getItem("root:latest-tool")'), second.id);
    if (!live) {
      assert.equal(await evaluate('document.body.dataset.readingData', second.context), 'absent');
      const oldData = await evaluate('localStorage.getItem(' + JSON.stringify('root:data:' + first.id) + ')');
      for (const record of ['失败后新增的书', '等待模型时新增的书', '准备预览时新增的书']) {
        assert.ok(oldData.includes(record));
      }
      assert.equal(await evaluate('localStorage.getItem(' + JSON.stringify('root:data:' + second.id) + ')'),
        '[["budget-version","1"]]');
      stage = 'replay storage message from discarded preview';
      const replay = await evaluate(`(() => {
        const old = window.fixtureDiscarded;
        const currentKey = 'root:data:' + localStorage.getItem('root:latest-tool');
        const oldBefore = localStorage.getItem(old.key);
        const currentBefore = localStorage.getItem(currentKey);
        window.dispatchEvent(new MessageEvent('message', {
          source: old.source,
          origin: 'null',
          data: { type: 'storage', token: old.token, entries: [['books', 'stale']] },
        }));
        return localStorage.getItem(old.key) === oldBefore && localStorage.getItem(currentKey) === currentBefore;
      })()`);
      assert.equal(replay, true);
      evidence.tests.push({ id: 'storage-bridge-isolation', result: 'passed',
        rejected: ['wrong origin', 'wrong instance token', 'wrong source', 'discarded preview replay'] });
    }
    assert.notEqual(evidence.runs[0].requestId, evidence.runs[1].requestId);
    evidence.tests.push({ id: 'TEST-001', result: live ? 'passed' : 'fixture passed', total: 405 });
    await screenshot('budget-result');

    if (!live) {
      stage = 'narrow viewport';
      await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      const layout = await evaluate(`({
        singleColumn: document.querySelector('.elys-preview').getBoundingClientRect().top >
          document.querySelector('.elys-composer').getBoundingClientRect().bottom,
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      })`);
      assert.deepEqual(layout, { singleColumn: true, overflow: false });
      await screenshot('narrow-budget');
      evidence.tests.push({ id: 'narrow-viewport', result: 'passed', width: 390 });

      stage = 'preview self-navigation isolation';
      await evaluate('location.href = ' + JSON.stringify(probeUrl + '/self-navigation'), second.context);
      await delay(600);
      assert.deepEqual(probeRequests, probeControls, '可达 HTTP 探针只能收到控制请求');
      assert.equal(await evaluate('location.origin'), base);
      evidence.tests.push({ id: 'preview-isolation', result: 'passed',
        attempted: ['parent DOM', 'fetch', 'external image', 'nested frame', 'self-navigation', 'opaque-origin POST'],
        receivedRequests: probeRequests });
    }
  }
  evidence.status = 'passed';
} catch (error) {
  evidence.status = 'failed';
  evidence.error = error.message;
  evidence.failedStage = stage;
  if (runtimeErrors.length) evidence.runtimeErrors = runtimeErrors;
  if (readPage) evidence.page = await readPage().catch(() => null);
  process.exitCode = 1;
} finally {
  if (artifacts) {
    evidence.artifacts = artifacts;
    await writeFile(join(artifacts, 'workbench-check.json'), JSON.stringify(evidence, null, 2) + '\n')
      .catch(error => { console.error(error.message); process.exitCode = 1; });
  }
  console.log(JSON.stringify(evidence, null, 2));
  socket?.close();
  probe?.close();
  probe?.closeAllConnections();
  await Promise.all(children.map(async child => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = once(child, 'exit');
    child.kill();
    await exited;
  }));
  await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
