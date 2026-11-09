// TEST-001 / TEST-002 / TEST-004 / TEST-005 / TEST-006. Default uses a deterministic provider fixture. --live uses DeepSeek.
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
const sockets = [];
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

  }
  if (live && !artifacts) {
    artifacts = await mkdtemp(join(resolve('runs'), 'workbench-live-'));
  }
  if (artifacts) await mkdir(artifacts, { recursive: true, mode: 0o700 });
  async function connectBrowser(browserProfile) {
    const chrome = launch(process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
      '--headless=new', '--no-first-run', '--disable-background-networking', '--disable-extensions',
      '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
      '--remote-debugging-port=0', '--user-data-dir=' + browserProfile, 'about:blank',
    ]);
    const port = await until(async () => {
      assert.equal(chrome.exitCode, null);
      return (await readFile(join(browserProfile, 'DevToolsActivePort'), 'utf8').catch(() => '')).split('\n')[0];
    });
    const tabs = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
    const connected = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
    await once(connected, 'open');
    sockets.push(connected);
    connected.addEventListener('message', onMessage);
    return connected;
  }
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
  function onMessage(event) {
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
  }
  socket = await connectBrowser(profile);

  async function evaluate(expression, context) {
    const result = await cdp('Runtime.evaluate', {
      expression, contextId: context?.id, returnByValue: true, awaitPromise: true,
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
    auth: document.querySelector('#auth-status')?.textContent,
    account: document.querySelector('#account-name')?.textContent,
    config: document.querySelector('#config')?.textContent,
    storage: document.querySelector('#storage-warning')?.textContent,
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
    const data = await readFile(join(live ? resolve(process.env.WORKBENCH_RUNS_DIR || 'runs') : join(root, 'runs'), tool.id, 'generation.json'), 'utf8');
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

  async function appRequest(path, data, marker) {
    return evaluate(`(async () => {
      const data = ${JSON.stringify(data)};
      const response = await fetch(${JSON.stringify(path)}, {
        method: data === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Root-Session': ${JSON.stringify(marker)} ?? session.marker },
        body: data === undefined ? undefined : JSON.stringify(data),
      });
      return { status: response.status, body: await response.json() };
    })()`);
  }

  async function stored(id) {
    const response = await appRequest('/api/tools/' + id);
    assert.equal(response.status, 200);
    return { entries: response.body.entries, revision: response.body.revision };
  }

  async function library() {
    const response = await appRequest('/api/tools');
    assert.equal(response.status, 200);
    return response.body;
  }

  const accountA = 'qa-a-' + Date.now().toString(36);
  const accountB = 'qa-b-' + Date.now().toString(36);
  const password = 'root-browser-test-only!';
  const invite = live ? process.env.ROOT_TEST_INVITE : 'fixture-invite';
  assert.ok(invite, '真实浏览器自测需要本机受控 ROOT_TEST_INVITE');

  async function login(username, register = false) {
    await until(() => evaluate('document.documentElement.dataset.ready === "true" && !document.querySelector("#auth").hidden'));
    await evaluate('document.querySelector("#username").value = ""; document.querySelector("#password").value = "";');
    await type('#username', username);
    if (register) {
      await click('#auth-mode');
      await type('#password', password);
      await type('#invite', invite);
      await click('#auth-submit');
      await until(() => evaluate('document.querySelector("#auth-status").textContent === "注册成功，请登录。"'));
    }
    await type('#password', password);
    await click('#auth-submit');
    await until(() => evaluate(`document.documentElement.dataset.ready === "true" &&
      !document.querySelector("#workspace").hidden && document.querySelector("#account-name").textContent === ${JSON.stringify(username)}`));
  }

  async function logout() {
    await click('#logout');
    await until(() => evaluate('document.documentElement.dataset.ready === "true" && !document.querySelector("#auth").hidden'));
    assert.equal(await evaluate('document.querySelectorAll("#preview iframe").length'), 0);
  }

  async function selectHistory(id) {
    await until(() => evaluate(`Boolean(document.querySelector('#tool-list button[data-id="${id}"]:enabled'))`));
    await click(`#tool-list button[data-id="${id}"]`);
    const selected = await until(async () => {
      const tool = await activeTool();
      return tool.id === id && tool;
    });
    await until(() => evaluate(`(() => {
      const item = document.querySelector('#tool-list button[data-id="${id}"]');
      return item.getAttribute('aria-current') === 'true' &&
        document.querySelector('#preview-title').textContent === item.textContent;
    })()`));
    return selected;
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
  if (!live) {
    stage = 'account visual layout and registration feedback';
    await screenshot('account-login');
    await click('#auth-mode');
    assert.deepEqual(await evaluate(
      'Array.from(document.querySelectorAll("#auth-form input"), input => input.id)'
    ), ['invite', 'username', 'password']);
    assert.equal(await evaluate(`['username', 'password'].every(id => {
      const field = document.getElementById(id);
      const hint = document.getElementById(id + '-hint');
      return hint?.checkVisibility() && field.getAttribute('aria-describedby').includes(hint.id);
    })`), true, '提交前可见且关联到各字段的注册格式提示');
    await screenshot('account-register');
    await type('#invite', 'invalid-fixture-invite');
    await type('#username', 'bad name');
    await type('#password', 'short');
    await click('#auth-submit');
    await until(() => evaluate('document.querySelector("#username").getAttribute("aria-invalid") === "true"'));
    await evaluate('document.querySelector("#username").value = ""');
    await type('#username', 'visual-probe');
    await click('#auth-submit');
    await until(() => evaluate('document.querySelector("#password").getAttribute("aria-invalid") === "true"'));
    await evaluate('document.querySelector("#password").value = ""');
    await type('#password', password);
    await click('#auth-submit');
    await until(() => evaluate('document.querySelector("#invite").getAttribute("aria-invalid") === "true"'));
    assert.equal(await evaluate('document.querySelector("#invite").value'), 'invalid-fixture-invite');
    assert.equal(await evaluate('document.querySelector("#username").value'), 'visual-probe');
    assert.equal(await evaluate('document.querySelector("#password").value'), password);
    await screenshot('account-invalid-invite');
    await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await cdp('Emulation.setTouchEmulationEnabled', { enabled: true });
    assert.deepEqual(await evaluate(`({
      illustration: document.querySelector('.auth-illustration').checkVisibility(),
      belowTitle: document.querySelector('#auth-form').getBoundingClientRect().top >
        document.querySelector('#auth-intro').getBoundingClientRect().bottom,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    })`), { illustration: false, belowTitle: true, overflow: false });
    assert.equal(await evaluate(`matchMedia('(pointer: coarse)').matches &&
      [...document.querySelectorAll('#auth-form input, #auth-submit')]
        .every(element => element.getBoundingClientRect().height >= 44)`), true);
    await screenshot('account-narrow');
    await cdp('Emulation.setTouchEmulationEnabled', { enabled: false });
    await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
    await evaluate('document.querySelector("#invite").value = ""');
    await click('#auth-mode');
    evidence.tests.push({ id: 'account-visual-feedback', result: 'passed',
      fieldErrors: ['username', 'password', 'invite'], narrowWidth: 390 });
  }
  stage = 'register and log in via workbench';
  await login(accountA, true);
  evidence.tests.push({ id: 'TEST-004', result: 'passed', flow: ['invite registration', 'login'] });
  if (!live) {
    const cases = [
      ['   ', 400, 'invalid_requirement', /请输入/],
      ['长'.repeat(2001), 400, 'invalid_requirement', /请输入/],
      ['回归：模型超时', 502, 'request_failed', /生成超时/],
      ['回归：模型限流', 502, 'http_error', /请求过多/],
      ['回归：余额不足', 502, 'http_error', /余额不足/],
    ];
    for (const [requirement, status, code, message] of cases) {
      const response = await appRequest('/api/generate', { requirement });
      assert.equal(response.status, status);
      const body = response.body;
      assert.equal(body.code, code);
      assert.match(body.message, message);
    }
    evidence.tests.push({ id: 'request-guards', result: 'passed',
      cases: ['blank', 'over-length', 'opaque-origin', 'provider timeout', 'provider 429', 'provider 402'] });
  }
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
  await until(async () => JSON.stringify((await stored(first.id)).entries).includes(sample));
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
    assert.equal((await library()).latest, restored.id);
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
    assert.equal((await library()).items.length, 1);
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
    await until(async () => JSON.stringify((await stored(restored.id)).entries).includes(name));
  }

  if (!mainOnly) {
    stage = 'generate budget tool';
    await click('#new-tool');
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
        const realFetch = window.fetch;
        window.fetch = (url, options) => String(url).endsWith('/commit')
          ? Promise.resolve(new Response(JSON.stringify({ message: '无法保存工具' }), { status: 503 }))
          : realFetch(url, options);
        window.restoreFixtureStorage = () => { window.fetch = realFetch; };
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
      const duplicate = await appRequest('/api/generate', { requirement: requirements[1] });
      assert.equal(duplicate.status, 409);
      assert.equal(duplicate.body.code, 'busy');
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
      const beforeForgery = await stored(restored.id);
      await evaluate(`(() => {
        const previous = window.fixtureLastStorage;
        const payload = { type: 'storage', token: previous.token, entries: [['books', 'forged']] };
        const attempts = [
          { source: previous.source, origin: location.origin, data: payload },
          { source: previous.source, origin: 'null', data: { ...payload, token: 'wrong-token' } },
          { source: window, origin: 'null', data: payload },
        ];
        window.fixtureDiscarded = previous;
        for (const options of attempts) window.dispatchEvent(new MessageEvent('message', options));
      })()`);
      await delay(150);
      assert.deepEqual(await stored(restored.id), beforeForgery);
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
    assert.equal((await library()).latest, second.id);
    if (!live) {
      assert.equal(await evaluate('document.body.dataset.readingData', second.context), 'absent');
      const oldData = JSON.stringify((await stored(first.id)).entries);
      for (const record of ['失败后新增的书', '等待模型时新增的书', '准备预览时新增的书']) {
        assert.ok(oldData.includes(record));
      }
      await until(async () => (await stored(second.id)).entries.some(([key]) => key === 'budget-values'));
      stage = 'replay storage message from discarded preview';
      const beforeReplay = await Promise.all([stored(first.id), stored(second.id)]);
      await evaluate(`(() => {
        const old = window.fixtureDiscarded;
        window.dispatchEvent(new MessageEvent('message', {
          source: old.source,
          origin: 'null',
          data: { type: 'storage', token: old.token, entries: [['books', 'stale']] },
        }));
      })()`);
      await delay(150);
      assert.deepEqual(await Promise.all([stored(first.id), stored(second.id)]), beforeReplay);
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
          document.querySelector('.tool-library').getBoundingClientRect().bottom,
        composerBelow: document.querySelector('.elys-composer').getBoundingClientRect().top >
          document.querySelector('.elys-preview').getBoundingClientRect().bottom,
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      })`);
      assert.deepEqual(layout, { singleColumn: true, composerBelow: true, overflow: false });
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
      stage = 'cross-browser account restoration';
      const primary = socket;
      socket = await connectBrowser(join(root, 'chrome-independent'));
      await cdp('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
      await cdp('Page.enable');
      await cdp('Page.navigate', { url: base });
      await login(accountA);
      let tool = await activeTool();
      assert.equal(tool.id, second.id);
      assert.equal(await evaluate('document.querySelector("#total").innerText.trim()', tool.context), '405');
      tool = await selectHistory(first.id);
      assert.match(await evaluate('document.querySelector("#list").innerText', tool.context), /星际旅行手记/);
      await type('#book', '独立浏览器保存的书', tool.context);
      await click('form button', tool.context);
      await until(async () => JSON.stringify((await stored(first.id)).entries).includes('独立浏览器保存的书'));
      await screenshot('second-browser');
      await logout();

      socket = primary;
      await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
      await logout();
      await login(accountA);
      tool = await activeTool();
      assert.equal(tool.id, first.id);
      assert.match(await evaluate('document.querySelector("#list").innerText', tool.context), /独立浏览器保存的书/);
      assert.deepEqual((await library()).items.map(item => item.id).sort(), [first.id, second.id].sort());
      assert.deepEqual((await library()).items.map(item => item.title).sort(),
        ['读书清单', '搬家预算计算器'].sort());
      tool = await selectHistory(second.id);
      assert.equal(await evaluate('document.querySelector("#total").innerText.trim()', tool.context), '405');

      stage = 'failure preserves two tools and editable preview';
      await click('#new-tool');
      await generate('回归：强制新建失败');
      await until(() => evaluate('document.querySelector("#error").textContent'));
      assert.equal((await library()).items.length, 2);
      assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").dataset.tool'), second.id);
      await evaluate('document.querySelector("#boxes").value = ""', tool.context);
      await type('#boxes', '100', tool.context);
      await click('#calculate', tool.context);
      assert.equal(await evaluate('document.querySelector("#total").innerText.trim()', tool.context), '420');
      await until(async () => JSON.stringify((await stored(second.id)).entries).includes('100'));
      await screenshot('history-after-failure');
      evidence.tests.push({ id: 'TEST-006', result: 'passed', restoredTools: 2, retainedTotalAfterFailure: 420 });

      stage = 'same-browser account switch and direct tool authorization';
      const oldMarker = await evaluate('session.marker');
      await logout();
      await login(accountB, true);
      assert.deepEqual((await library()).items, []);
      assert.equal(await evaluate('document.querySelectorAll("#preview iframe").length'), 0);
      assert.equal((await appRequest('/api/tools/' + first.id)).status, 404);
      assert.equal((await appRequest('/api/tools/' + first.id + '/data', { revision: 1, entries: [] })).status, 404);
      const stale = await appRequest('/api/tools', undefined, oldMarker);
      assert.equal(stale.status, 409);
      assert.equal(stale.body.code, 'session_changed');
      evidence.tests.push({ id: 'TEST-005', result: 'passed', browserProfiles: 2,
        crossBrowserRecord: '独立浏览器保存的书', sameBrowserAccountSwitch: true,
        directReadStatus: 404, directWriteStatus: 404, staleSessionStatus: 409 });

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
  for (const connection of sockets) connection.close();
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
