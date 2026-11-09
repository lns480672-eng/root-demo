// TEST-001 / TEST-002 / TEST-004 / TEST-005 / TEST-006. Default uses a deterministic provider fixture. --live uses DeepSeek.
import assert from 'node:assert/strict';
import { verifyStagedFlow, verifyStagedCalculator, verifyStagedRegistration, verifyStagedRevision } from './verify-staged-flow.mjs';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const live = process.argv.includes('--live');
const stagedFlow = process.argv.includes('--staged-flow');
if (live && stagedFlow) throw new Error('--staged-flow uses ROOT_STAGED_LIVE=1 for real staged generation');
const liveRevision = live && process.argv.includes('--revision');
const collectCoverage = process.argv.includes('--coverage');
const mainOnly = process.argv.includes('--main');
const entryFlow = process.argv.includes('--entry-flow');
const adminFlow = process.argv.includes('--admin-flow');
if ((entryFlow || adminFlow) && live) throw new Error('选定的流程使用模型替身，请移除 --live');
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
let writeCoverage;

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
  liveRevision
    ? '做一个最小读书清单，h1 为「读书清单」。一个 form，文本框 id="book"，提交按钮文字「添加书籍」，提交清空输入。书名用文本渲染到 ul#list。用 localStorage 保存和恢复书名数组。无需 CSS、删除、示例数据或额外控件。首字符是 <!doctype html> 的尖括号，末尾是 </html>，无 Markdown 和前后文字。'
    : '请做一个 h1 为「读书清单」的工具。首次列表为空。用 form 包含 id="book" 的书名输入和 type="submit" 的「添加书籍」按钮，使用可见的 ul#list 显示书名。提交后清空输入。请使用 localStorage 保存并恢复书单。',
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
    const fixture = stagedFlow ? 'tests/staged_fixtures.py' : 'tests/fixtures.py';
    const server = launch('python3', [fixture, 'fixture-key', '0', join(root, 'runs'), probeUrl,
      ...(adminFlow ? ['--admin-flow'] : [])]);
    const [line] = await Promise.race([once(server.stdout, 'data'), once(server, 'exit').then(() => [''])]);
    base = String(line).match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
    assert.ok(base, '工作台应启动并提供本机地址');
  }
  const config = await (await fetch(base + '/api/config', { signal: AbortSignal.timeout(5000) })).json();
  evidence.build = config.build;
  assert.equal(config.configured, true, '需要配置本地工作台的 DeepSeek Key');
  assert.deepEqual(Object.keys(config).sort(), stagedFlow ? ['build', 'configured', 'staged'] : ['build', 'configured']);
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
  if (collectCoverage && !artifacts) throw new Error('--coverage requires --artifacts');
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
  const coverageSources = ['/workbench.js', '/entry.js', '/assistant.js', '/table.js'];
  const coverage = [];
  async function startCoverage() {
    if (!collectCoverage) return;
    await cdp('Profiler.enable');
    await cdp('Profiler.startPreciseCoverage', { callCount: true, detailed: true });
  }
  async function sampleCoverage() {
    if (!collectCoverage) return;
    const { result } = await cdp('Profiler.takePreciseCoverage');
    coverage.push(...result.filter(script => coverageSources.some(source => script.url === base + source)));
  }
  if (collectCoverage) writeCoverage = async () => {
    await sampleCoverage();
    await writeFile(join(artifacts, 'host-js-coverage.json'), JSON.stringify({
      build: evidence.build, sources: coverageSources, format: 'CDP detailed V8 ranges', samples: coverage,
    }, null, 2) + '\n');
  };
  await startCoverage();

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
      try {
        const { frameTree } = await cdp('Page.getFrameTree', {}, session);
        const frame = session ? frameTree.frame
          : frameTree.childFrames?.find(child => child.frame.id === node.frameId)?.frame;
        if (frame?.url !== 'about:srcdoc') return;
        const { executionContextId } = await cdp('Page.createIsolatedWorld', {
          frameId: frame.id, worldName: 'workbench-test',
        }, session);
        return { id, context: { id: executionContextId, session }, loader: frame.loaderId };
      } catch (error) {
        // A replacement iframe can detach between DOM.describeNode and the target request.
        if (!/Session with given id not found|No frame for given id/.test(error.message)) throw error;
      }
    }, live ? 135000 : 5000);
  }

  async function type(selector, value, context) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`, context);
    await cdp('Input.insertText', { text: value });
  }

  async function click(selector, context) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`, context);
  }

  async function pointerClick(selector, context) {
    const button = await evaluate(`(() => {
      const target = document.querySelector(${JSON.stringify(selector)});
      target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      const rect = target.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    })()`, context);
    const frame = context ? await evaluate(`(() => {
      const rect = document.querySelector('#preview iframe[data-tool]').getBoundingClientRect();
      return { x: rect.x, y: rect.y };
    })()`) : { x: 0, y: 0 };
    const point = { x: frame.x + button.x, y: frame.y + button.y, button: 'left', clickCount: 1 };
    await cdp('Input.dispatchMouseEvent', { ...point, type: 'mousePressed', buttons: 1 });
    await cdp('Input.dispatchMouseEvent', { ...point, type: 'mouseReleased', buttons: 0 });
  }

  async function screenshot(name) {
    if (!artifacts) return;
    const { data } = await cdp('Page.captureScreenshot', { format: 'png' });
    await writeFile(join(artifacts, name + '.png'), Buffer.from(data, 'base64'));
  }

  async function recordRun(tool) {
    const data = await readFile(join(live ? resolve(process.env.WORKBENCH_RUNS_DIR || 'runs') : join(root, 'runs'),
      tool.runId || tool.id, 'generation.json'), 'utf8');
    const run = JSON.parse(data);
    assert.equal(run.http_status, 200);
    assert.equal(run.status, 'generated');
    assert.equal(run.returned_model, 'deepseek-flash');
    assert.ok(run.request_id);
    assert.equal(run.request_id.startsWith('fixture-'), !live);
    if (live) assert.ok(run.usage?.total_tokens > 0, '真实请求应提供用量证据');
    evidence.runs.push({ tool: tool.id, requestId: run.request_id, model: run.returned_model,
      elapsedSeconds: run.elapsed_seconds, usage: run.usage });
    if (tool.runId) evidence.runs.at(-1).runId = tool.runId;
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
      await type('#invite', invite);
      await evaluate('window.fixtureRegistrationDocument = true;');
    }
    await type('#password', password);
    await sampleCoverage();
    await click('#auth-submit');
    await until(async () => {
      if (register) {
        assert.notEqual(await evaluate('document.querySelector("#auth-status").textContent'), '注册成功，请登录。',
          '提交注册后应直接进入工作台，无需另行登录');
      }
      return evaluate(`document.documentElement.dataset.ready === "true" &&
        !document.querySelector("#workspace").hidden && document.querySelector("#account-name").textContent === ${JSON.stringify(username)}`);
    });
    if (register) assert.equal(await evaluate('window.fixtureRegistrationDocument'), true, '首次引导应从同一注册页面进入');
  }

  async function logout() {
    await sampleCoverage();
    await click('#logout');
    await until(() => evaluate('document.documentElement.dataset.ready === "true" && !document.querySelector("#auth").hidden'));
    assert.equal(await evaluate('document.querySelectorAll("#preview iframe").length'), 0);
  }

  async function selectHistory(id) {
    await until(() => evaluate(`Boolean(document.querySelector('#tool-list button[data-id="${id}"]:enabled'))`));
    await click(`#tool-list button[data-id="${id}"]`);
    await until(() => evaluate('phase === "idle"'));
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

  const visible = selector => evaluate(`Boolean(document.querySelector(${JSON.stringify(selector)})?.checkVisibility())`);
  const guideCopy = () => evaluate('document.querySelector("#guide-copy")?.textContent.trim() || ""');
  const waitGuide = message => until(async () => (await visible('#workspace-guide')) && message.test(await guideCopy()));

  async function monitorGeneration() {
    await evaluate(`(() => {
      const fetchFromWorkbench = window.fetch;
      window.testGenerationPosts = [];
      window.testPublishPosts = [];
      window.fetch = (url, options) => {
        const path = String(url);
        if (options?.method === 'POST') {
          if (path.endsWith('/api/generate')) window.testGenerationPosts.push(JSON.parse(options.body).requirement);
          if (path.includes('publish')) window.testPublishPosts.push(path);
        }
        return fetchFromWorkbench(url, options);
      };
    })()`);
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
  if (stagedFlow) {
    evidence.mode = process.env.ROOT_STAGED_LIVE === '1' ? 'deepseek-staged' : 'fixture-staged';
    const calculator = process.env.ROOT_STAGED_CALCULATOR === '1';
    const registration = process.env.ROOT_STAGED_REGISTRATION === '1';
    const revision = process.env.ROOT_STAGED_REVISION === '1';
    stage = registration ? 'staged workbench registration' : revision ? 'staged workbench revision' :
      calculator ? 'staged workbench calculator' : 'staged workbench board flow';
    const verify = registration ? verifyStagedRegistration : revision ? verifyStagedRevision :
      calculator ? verifyStagedCalculator : verifyStagedFlow;
    const anotherBrowser = async (name, action) => {
      const original = socket;
      socket = await connectBrowser(join(root, name));
      try {
        await cdp('Target.setAutoAttach', {autoAttach:true, waitForDebuggerOnStart:false, flatten:true});
        await cdp('Page.enable');
        await cdp('Runtime.enable');
        return await action();
      } finally {
        socket = original;
      }
    };
    await verify({ cdp, evaluate, until, base, click, pointerClick, type, visible, appRequest, login,
      generate, activeTool, accountA, root, probeUrl, probeRequests, probeControls, library, stored,
      evidence, screenshot, anotherBrowser });
  } else {
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
  await cdp('Page.navigate', { url: base });
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  if (adminFlow) {
    const { verifyAdmin } = await import('./slice-admin.mjs');
    evidence.tests.push(...await verifyAdmin({
      evaluate, click, pointerClick, type, until, cdp, login, logout, visible, appRequest, screenshot,
      stage: name => { stage = name; },
    }));
  }
  if (!live) {
    stage = 'account visual layout and registration feedback';
    await screenshot('account-login');
    await click('#auth-mode');
    assert.deepEqual(await evaluate(
      'Array.from(document.querySelectorAll("#auth-form input"), input => input.id)'
    ), ['invite', 'username', 'password']);
    assert.equal(await evaluate(`['invite', 'username', 'password'].every(id => {
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
    assert.match(await evaluate('document.querySelector("#invite-error").textContent'), /邀请码无效/);
    assert.equal(await evaluate('document.activeElement.id'), 'invite');
    await screenshot('account-invalid-invite');
    for (const [code, expected] of [
      ['', '请输入邀请码'], ['   ', '请输入邀请码'],
      ['fixture-expired-invite', '邀请码已过期'], ['fixture-used-invite', '邀请码已使用'],
    ]) {
      await evaluate(`(() => {
        const input = document.querySelector('#invite');
        input.value = ${JSON.stringify(code)};
        input.dispatchEvent(new Event('input', {bubbles: true}));
      })()`);
      await click('#auth-submit');
      await until(() => evaluate('!document.querySelector("#invite-error").hidden'));
      assert.match(await evaluate('document.querySelector("#invite-error").textContent'), new RegExp(expected));
      assert.equal(await evaluate('document.querySelector("#invite").value'), code);
      assert.equal(await evaluate('document.querySelector("#username").value'), 'visual-probe');
      assert.equal(await evaluate('document.querySelector("#password").value'), password);
      assert.equal(await evaluate('document.activeElement.id'), 'invite');
    }
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
      fieldErrors: ['username', 'password', 'invite'],
      inviteReasons: ['missing', 'invalid', 'expired', 'used'], narrowWidth: 390 });
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
  if (entryFlow) {
    stage = 'entry choices and first-use guide';
    for (const selector of ['#quick-entry', '#direct-entry', '#idea-entry']) {
      assert.equal(await visible(selector), true, `首页入口应可见 ${selector}`);
    }
    const welcome = '欢迎使用 ROOT。先选一个模板，试着生成你的第一个工具。';
    await waitGuide(/快速体验|欢迎使用 ROOT/);
    const firstWelcome = await guideCopy();

    stage = 'guide auto-opens only after registration and stays closed on later login';
    await sampleCoverage();
    await cdp('Page.reload', { ignoreCache: true });
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    assert.equal(await visible('#workspace-guide'), false, '首次未完成时重新进入也不应自动打开');
    const primary = socket;
    socket = await connectBrowser(join(root, 'chrome-guide-independent'));
    await startCoverage();
    await cdp('Page.enable');
    await cdp('Page.navigate', { url: base });
    await login(accountA);
    assert.equal(await visible('#workspace-guide'), false, '同账号换浏览器登录时，未完成引导也不应自动打开');
    await click('#guide-start');
    await waitGuide(/^欢迎使用 ROOT/);
    assert.equal(await guideCopy(), welcome);
    await logout();
    await login(accountB, true);
    await waitGuide(/^欢迎使用 ROOT/);
    assert.equal(await guideCopy(), welcome, '另一个新账号应在注册成功后收到同样的首次引导');
    await click('#guide-skip');
    assert.equal(await visible('#workspace-guide'), false);
    await sampleCoverage();
    socket = primary;
    assert.equal(firstWelcome, welcome);
    await click('#guide-start');
    await waitGuide(/^欢迎使用 ROOT/);

    stage = 'three local samples are usable without upstream calls or saved tools';
    const generatedDirectories = async () => (await readdir(join(root, 'runs'))).filter(name => name.startsWith('run-')).sort();
    const before = await generatedDirectories();
    assert.deepEqual((await library()).items, []);
    await monitorGeneration();
    await click('#quick-entry');
    assert.equal(await visible('#quick-panel'), true);
    await waitGuide(/用此模板生成/);
    for (const [key, title] of [['board', '交付跟进板'], ['list', '读书清单'], ['budget', 'AA 分账']]) {
      await click(`[data-entry-template="${key}"]`);
      await click('#template-sample');
      assert.equal(await visible('#sample-preview'), true);
      assert.match(await evaluate('document.querySelector("#sample-title").textContent'), new RegExp(title));
      const previous = await evaluate('document.querySelector("#sample-result").textContent');
      if (key === 'budget') {
        await evaluate('document.querySelector("#sample-amount").value = ""; document.querySelector("#sample-people").value = "";');
        await type('#sample-amount', '96');
        await type('#sample-people', '4');
        await click('#sample-calculate');
        assert.match(await evaluate('document.querySelector("#sample-result").textContent'), /24(?:\.00)?/);
      } else {
        const checkbox = '#sample-preview [data-sample-toggle]';
        const checked = await evaluate(`document.querySelector(${JSON.stringify(checkbox)}).checked`);
        await click(checkbox);
        assert.equal(await evaluate(`document.querySelector(${JSON.stringify(checkbox)}).checked`), !checked);
        assert.notEqual(await evaluate('document.querySelector("#sample-result").textContent'), previous);
      }
      await waitGuide(/用此模板生成/);
      await click('#sample-close');
      assert.equal(await visible('#sample-preview'), false);
    }
    assert.deepEqual(await generatedDirectories(), before);
    assert.deepEqual((await library()).items, []);
    assert.deepEqual(await evaluate('window.testGenerationPosts'), []);

    stage = 'customize template prefills an editable direct requirement without generating';
    await click('[data-entry-template="list"]');
    await click('#template-customize');
    assert.equal(await visible('#requirement'), true);
    assert.match(await evaluate('document.querySelector("#requirement").value'), /读书清单/);
    assert.equal(await evaluate('document.querySelector("#requirement").readOnly'), false);
    assert.deepEqual(await evaluate('window.testGenerationPosts'), []);
    await click('#quick-entry');
    await click('[data-entry-template="list"]');
    await waitGuide(/用此模板生成/);
    stage = 'explicit template generation creates one real fixture tool';
    await click('#template-generate');
  } else {
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
  }
  const first = await activeTool();
  await recordRun(first);
  assert.match(await evaluate('document.querySelector("h1").innerText', first.context), /读书清单/);
  if (entryFlow) {
    assert.match(await evaluate('window.testGenerationPosts.join(" ")'), /读书清单/);
    assert.equal(await evaluate('window.testGenerationPosts.length'), 1);
    assert.deepEqual((await library()).items.map(item => item.id), [first.id]);
    stage = 'guide waits for a real preview operation and publish remains locked';
    await waitGuide(/预览/);
    assert.equal(await visible('#publish-tool'), true);
    assert.equal(await evaluate('document.querySelector("#publish-tool").disabled'), true);
    assert.match(await evaluate('document.querySelector("#publish-explanation").textContent'), /应用发布暂未开放/);
    await pointerClick('#publish-tool');
    assert.equal(await evaluate(`(() => {
      const target = document.querySelector('#publish-tool');
      target.focus();
      return document.activeElement === target;
    })()`), false);
    assert.deepEqual(await evaluate('window.testPublishPosts'), []);
    evidence.tests.push({ id: 'TEST-015', result: 'passed', disabled: true, skippedByKeyboardFocus: true, publishRequests: 0 });
  }
  const sample = '星际旅行手记';
  stage = 'add reading record';
  await type('#book', sample, first.context);
  if (entryFlow) await pointerClick('form button', first.context);
  else await click('form button', first.context);
  await until(() => evaluate(`document.querySelector("#list").innerText.includes("${sample}")`, first.context));
  await until(async () => JSON.stringify((await stored(first.id)).entries).includes(sample));
  let operatedTool = first;
  if (entryFlow) {
    stage = 'guide advances after actual preview operation and finding the generated tool';
    await waitGuide(/我的工具/);
    operatedTool = await selectHistory(first.id);
    assert.match(await evaluate('document.querySelector("#list").innerText', operatedTool.context), /星际旅行手记/);
    await until(async () => !(await visible('#workspace-guide')) || /完成/.test(await guideCopy()));
    await screenshot('entry-guide-completed');
  }
  await screenshot('record-added');
  if (!live) {
    assert.equal(await evaluate('document.body.dataset.parentAccess', operatedTool.context), 'blocked');
    assert.equal(await evaluate('document.body.dataset.network', operatedTool.context), 'blocked');
    assert.deepEqual(probeRequests, probeControls);
  }
  stage = 'reload workbench';
  await sampleCoverage();
  await cdp('Page.reload', { ignoreCache: true });
  await until(() => evaluate('performance.getEntriesByType("navigation")[0]?.type === "reload"'));
  let restored = await activeTool();
  assert.equal(restored.id, first.id);
  assert.notEqual(restored.loader, operatedTool.loader);
  assert.equal(await visible('.elys-composer'), false, '刷新恢复已有工具后隐藏新建区');
  assert.match(await evaluate('document.querySelector("#list").innerText', restored.context), /星际旅行手记/);
  if (entryFlow) {
    assert.equal(await visible('#workspace-guide'), false, '完成引导后重新进入不应自动打开');
    evidence.tests.push({ id: 'TEST-016', result: 'passed', autoShownOncePerAccountAcrossBrowsers: true,
      secondFreshAccountShown: true, skippedAndReopened: true, sampleDidNotAdvance: true,
      trustedPreviewOperation: true, foundSameTool: true, dismissedAfterReload: true });
  }
  evidence.tests.push({ id: 'TEST-002', result: 'passed', tool: first.id, record: sample,
    reloadedDocument: true, isolationFixtureChecked: !live });
  await screenshot('record-restored');
  if (liveRevision) {
    stage = 'real DeepSeek revision preserves data and adds an operable filter';
    await evaluate(`(() => {
      const fetchFromWorkbench = window.fetch;
      window.fetch = async (url, options) => {
        const response = await fetchFromWorkbench(url, options);
        if (String(url).endsWith('/revise') && response.ok) {
          window.liveRevisionRun = (await response.clone().json()).runId;
        }
        return response;
      };
    })()`);
    await type('#book', '已读：设计中的设计', restored.context);
    await click('form button', restored.context);
    await until(async () => JSON.stringify((await stored(first.id)).entries).includes('已读：设计中的设计'));
    await click('#revise-tool');
    await generate('加一个 id="read-filter" 的下拉菜单，选项 all 表示全部，read 表示仅显示书名以「已读：」开头的书。保留 #book、原表单和 #list，按原样保存书名和原有数据。');
    await until(async () => {
      const state = await readPage();
      if (state.error) throw new Error(state.error);
      return evaluate('document.querySelector("#tool-history").textContent.includes("v2")');
    }, 135000);
    let updated = await activeTool();
    assert.equal(updated.id, first.id);
    assert.notEqual(updated.loader, restored.loader);
    await recordRun({ ...updated, runId: await evaluate('window.liveRevisionRun') });
    const books = 'document.querySelector("#list").innerText';
    assert.match(await evaluate(books, updated.context), /星际旅行手记/);
    assert.match(await evaluate(books, updated.context), /已读：设计中的设计/);
    await evaluate(`const filter = document.querySelector('#read-filter');
      filter.value = 'read'; filter.dispatchEvent(new Event('change'));`, updated.context);
    const filtered = await evaluate(books, updated.context);
    assert.match(filtered, /已读：设计中的设计/);
    assert.doesNotMatch(filtered, /星际旅行手记/);
    await evaluate(`(() => {
      const filter = document.querySelector('#read-filter');
      filter.value = 'all'; filter.dispatchEvent(new Event('change'));
    })()`, updated.context);
    assert.match(await evaluate(books, updated.context), /星际旅行手记/);
    await click('#tool-history');
    await until(() => evaluate('document.querySelectorAll("#tool-version-list li").length === 2'));
    await screenshot('live-revision');
    await cdp('Page.reload', { ignoreCache: true });
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    updated = await activeTool();
    assert.equal(updated.id, first.id);
    assert.match(await evaluate(books, updated.context), /星际旅行手记/);
    assert.match(await evaluate(books, updated.context), /已读：设计中的设计/);
    assert.equal(await evaluate('Boolean(document.querySelector("#read-filter"))', updated.context), true);
    evidence.tests.push({ id: 'TEST-007', result: 'passed', sameTool: true, filterVerified: true,
      reloaded: true, persistedRecords: ['星际旅行手记', '已读：设计中的设计'] });
    evidence.tests.push({ id: 'TEST-008', result: 'passed', versions: [2, 1] });
  }
  if (!live) {
    stage = 'release history navigation retains the current preview';
    const beforeUpdates = restored.loader;
    await evaluate('document.querySelector("#requirement").value = "返回后保留的草稿"');
    await click('#updates-link');
    await until(() => evaluate('location.pathname === "/updates" && !document.querySelector("#updates").hidden'));
    await until(() => evaluate('document.querySelectorAll("#release-list li").length >= 2'));
    const config = await appRequest('/api/config');
    assert.equal(await evaluate('document.querySelector("#release-list li[aria-current=true] code").textContent'),
      config.body.build);
    assert.equal(await evaluate('document.querySelector("#workspace").hidden'), true);
    await screenshot('release-history');
    await click('#updates-back');
    await until(() => evaluate('location.pathname === "/" && !document.querySelector("#workspace").hidden'));
    assert.equal((await activeTool()).loader, beforeUpdates);
    assert.equal(await evaluate('document.querySelector("#requirement").value'), '返回后保留的草稿');
    evidence.tests.push({ id: 'TEST-009', result: 'passed', currentBuild: config.body.build, retainedPreview: true });
  }
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
    await pointerClick('form button', restored.context);
    assert.ok(await evaluate('document.querySelector("#list").innerText.includes(' + JSON.stringify(name) + ')', restored.context));
    await until(async () => JSON.stringify((await stored(restored.id)).entries).includes(name));
  }

  if (!live && process.argv.includes('--publish-recovery')) {
    const toolPath = '/api/tools/' + first.id;
    const historyVersion = () => evaluate('document.querySelector("#tool-history").textContent');
    const waitForVersion = version => until(async () => {
      const state = await readPage();
      if (state.error) throw new Error(state.error);
      return (await historyVersion()).includes('v' + version + ' ·');
    });
    async function revise() {
      await click('#revise-tool');
      await generate('增加已读筛选');
    }
    async function injectPublishFault(drops, holdReadback = false, deliverPublish = true) {
      await evaluate(`(() => {
        const realFetch = window.fetch;
        window.fixturePublishResponses = [];
        window.fixtureBlockReadback = ${holdReadback};
        window.fetch = async (url, options) => {
          const path = String(url);
          if (path.endsWith('/publish') && ${drops} === 0) {
            return new Response(JSON.stringify({ code: 'storage_unavailable', message: '本机数据暂时无法保存' }), { status: 503 });
          }
          if (path.endsWith('/publish') && !${deliverPublish}) {
            throw new TypeError('fixture: publish did not reach service');
          }
          if (path.endsWith('/resolve-publish') && window.fixtureBlockReadback) {
            throw new TypeError('fixture: reconciliation unavailable');
          }
          const response = await realFetch(url, options);
          if (path.endsWith('/publish') && response.ok) {
            window.fixturePublishResponses.push(await response.clone().json());
            if (window.fixturePublishResponses.length <= ${drops}) throw new TypeError('fixture: publish acknowledgement lost');
          }
          return response;
        };
        window.fixtureRestoreFetch = () => { window.fetch = realFetch; };
      })()`);
    }
    stage = 'publish refused before commit, old tool stays editable';
    await injectPublishFault(0);
    await revise();
    assert.match(await until(() => evaluate('document.querySelector("#error").textContent')), /暂时无法保存/);
    assert.match(await historyVersion(), /v1 ·/);
    await addOldRecord('明确拒绝后仍可保存');
    await evaluate('window.fixtureRestoreFetch()');

    stage = 'unacknowledged requests that never reached the server restore the old preview';
    await injectPublishFault(2, false, false);
    await revise();
    assert.match(await until(() => evaluate('document.querySelector("#error").textContent')), /本次修改未发布/);
    assert.equal((await appRequest(toolPath)).body.version, 1);
    assert.equal((await activeTool()).loader, restored.loader);
    await addOldRecord('核对未发布后仍可保存');
    await evaluate('window.fixtureRestoreFetch()');

    stage = 'one lost publish response is retried once';
    await injectPublishFault(1);
    await revise();
    await waitForVersion(2);
    assert.deepEqual(await evaluate('window.fixturePublishResponses.map(item => item.version)'), [2, 2]);
    await evaluate('window.fixtureRestoreFetch()');

    stage = 'two lost publish responses recover the authoritative preview';
    await injectPublishFault(2);
    await revise();
    await waitForVersion(3);
    assert.deepEqual(await evaluate('window.fixturePublishResponses.map(item => item.version)'), [3, 3]);
    assert.deepEqual((await appRequest(toolPath + '/versions')).body.items.map(item => item.version), [3, 2, 1]);
    await click('#tool-history');
    assert.match(await until(() => evaluate('document.querySelector("#tool-version-list li[aria-current=true] strong")?.textContent')), /v3 · 当前/);
    restored = await activeTool();
    await addOldRecord('双确认丢失后仍可保存');
    await evaluate('window.fixtureRestoreFetch()');
    await screenshot('recovered-after-two-lost-confirmations');

    stage = 'unknown result disables writes until explicit synchronization';
    await injectPublishFault(2, true);
    await revise();
    await until(() => evaluate('document.querySelector("#sync-tool")?.checkVisibility()'));
    assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").inert'), true);
    assert.equal(await evaluate('document.querySelector("#tool-history").hidden'), true);
    assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
    assert.equal(await evaluate('document.querySelector("#preview-status").hasAttribute("data-usable")'), false);
    const before = await stored(first.id);
    assert.equal((await appRequest(toolPath)).body.version, 4);
    assert.equal(await evaluate(`(() => {
      const script = document.createElement('script');
      script.textContent = "try { localStorage.setItem('books', '[]'); } catch (e) { document.body.dataset.pauseError = e.name; }";
      document.body.append(script);
      return document.body.dataset.pauseError;
    })()`, restored.context), 'InvalidStateError');
    assert.deepEqual(await stored(first.id), before);
    await screenshot('publication-awaiting-sync');
    await click('#sync-tool');
    await until(() => evaluate('!document.querySelector("#sync-tool").disabled'));
    assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").inert'), true);
    const { identifier } = await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `
      if (window === top) {
        const realFetch = window.fetch;
        window.fetch = (url, options) => String(url).endsWith('/resolve-publish')
          ? Promise.reject(new TypeError('fixture: reconciliation unavailable after reload'))
          : realFetch(url, options);
        window.fixtureRestoreFetch = () => { window.fetch = realFetch; };
      }
    ` });
    await cdp('Page.reload', { ignoreCache: true });
    await until(() => evaluate('document.documentElement.dataset.ready === "true" && document.querySelector("#sync-tool").checkVisibility()'));
    assert.equal(await evaluate('document.querySelector("#preview iframe") === null'), true);
    await cdp('Page.removeScriptToEvaluateOnNewDocument', { identifier });
    await evaluate('window.fixtureRestoreFetch()');
    await click('#sync-tool');
    await waitForVersion(4);
    restored = await activeTool();
    await addOldRecord('手动同步后保存');
    await cdp('Page.reload', { ignoreCache: true });
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    const refreshed = await activeTool();
    assert.match(await historyVersion(), /v4 ·/);
    assert.match(await evaluate('document.querySelector("#list").innerText', refreshed.context), /手动同步后保存/);
    evidence.tests.push({ id: 'ROOT-S3-IA-02', result: 'passed', rejectedBeforeCommit: true,
      droppedAcknowledgements: [1, 2], confirmedVersions: [1, 2, 3, 4], blockedWhileUnknown: true,
      abandonedDraftResolvedToOldVersion: true, recoveredAfterReloadDuringOutage: true, manualSyncAndReload: true });
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
      await sampleCoverage();
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
      await click('#new-tool');
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
      stage = 'TEST-006-02 separates library, creation and revision';
      for (const width of [1360, 390]) {
        await cdp('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: width === 390 });
        assert.equal(await visible('.elys-composer'), false, '浏览已保存工具时隐藏整个新建区');
        assert.equal(await visible('#new-tool'), true);
        assert.equal(await visible('#tool-history'), true);
        await evaluate('document.querySelector("#new-tool").focus(); document.querySelector("#requirement").focus()');
        assert.equal(await evaluate('document.activeElement.id'), 'new-tool', '隐藏表单不能获取焦点');
        await cdp('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
        await cdp('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
        assert.equal(await evaluate('document.activeElement.closest("#tool-list")?.id'), 'tool-list');
        assert.equal(await evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'), false);
        if (width === 390) {
          assert.equal(await evaluate(`document.querySelector('.elys-preview').getBoundingClientRect().top >
            document.querySelector('.tool-library').getBoundingClientRect().bottom`), true);
        }
        await screenshot('library-' + width);

        await click('#new-tool');
        assert.equal(await visible('#table-entry'), true);
        assert.equal(await visible('#idea-entry'), true);
        assert.equal(await evaluate('document.activeElement.id'), 'requirement');
        assert.equal((await activeTool()).loader, second.loader);
        await click('#return-to-library');
        assert.equal(await visible('.elys-composer'), false);
        assert.equal(await evaluate('document.activeElement.id'), 'new-tool');

        await click('#revise-tool');
        assert.equal(await visible('#requirement'), true);
        assert.equal(await evaluate('document.activeElement.id'), 'requirement');
        assert.match(await evaluate('document.querySelector("#composer-title").textContent'), /修改/);
        for (const id of ['quick-entry', 'direct-entry', 'idea-entry', 'table-entry', 'guide-start', 'staged-task-list']) {
          assert.equal(await visible('#' + id), false, '修改时隐藏 ' + id);
        }
        await screenshot('revision-' + width);
        await click('#return-to-library');
        assert.equal(await visible('.elys-composer'), false);
        assert.equal(await evaluate('document.activeElement.id'), 'revise-tool');
        await click('#new-tool');
        await evaluate(`document.querySelector('#tool-list button[data-id="${first.id}"]').focus()`);
        await selectHistory(first.id);
        assert.equal(await visible('.elys-composer'), false);
        assert.equal(await evaluate('document.activeElement.dataset.id'), first.id);
        await selectHistory(second.id);
        const previousNavigation = await evaluate('performance.timeOrigin');
        await cdp('Page.reload', { ignoreCache: true });
        await until(() => evaluate(`performance.timeOrigin !== ${previousNavigation} &&
          document.documentElement.dataset.ready === "true"`));
        const reopened = await activeTool();
        assert.equal(reopened.id, second.id);
        second.loader = reopened.loader;
        second.context = reopened.context;
        assert.equal(await visible('.elys-composer'), false);
      }
      evidence.tests.push({ id: 'TEST-006-02', result: 'passed', widths: [1360, 390],
        libraryFocus: true, scopedRevisionForm: true, returnedAndSwitched: true });

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
      await startCoverage();
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
      const secondary = socket;
      await sampleCoverage();
      const secondaryReading = tool;

      socket = primary;
      await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
      stage = 'tool revision incompatibility preserves version and allows writing';
      restored = await selectHistory(first.id);
      const versionPath = '/api/tools/' + first.id + '/versions';
      await click('#tool-history');
      await until(() => evaluate('document.querySelectorAll("#tool-version-list li").length === 1'));
      const priorHistory = (await appRequest(versionPath)).body;
      assert.equal(priorHistory.current, 1);
      await click('#revise-tool');
      await generate('回归：不兼容数据');
      assert.match(await until(() => evaluate('document.querySelector("#error").textContent')), /无法.*已有数据/);
      await addOldRecord('修改失败后保存的书');
      assert.deepEqual((await appRequest(versionPath)).body, priorHistory);
      assert.equal((await activeTool()).loader, restored.loader);

      stage = 'revision keeps writes made from a second browser during preview';
      await evaluate('window.holdFixturePreview = true; window.releaseFixturePreview = undefined');
      await generate('增加已读筛选');
      await until(() => evaluate('typeof window.releaseFixturePreview === "function"'));
      assert.equal((await activeTool()).loader, restored.loader);
      socket = secondary;
      tool = await selectHistory(first.id);
      assert.notEqual(tool.loader, secondaryReading.loader);
      await type('#book', '已读：并发新增记录', tool.context);
      await click('form button', tool.context);
      await until(async () => JSON.stringify((await stored(first.id)).entries).includes('已读：并发新增记录'));
      await sampleCoverage();
      socket = primary;
      await evaluate('window.releaseFixturePreview()');
      tool = await until(async () => {
        const current = await activeTool();
        return current.loader !== restored.loader && current;
      });
      assert.equal(tool.id, first.id);
      assert.equal((await library()).items.length, 2);
      const books = 'document.querySelector("#list").innerText';
      assert.match(await evaluate(books, tool.context), /星际旅行手记/);
      assert.match(await evaluate(books, tool.context), /修改失败后保存的书/);
      assert.match(await evaluate(books, tool.context), /已读：并发新增记录/);
      await evaluate(`const filter = document.querySelector('#read-filter');
        filter.value = 'read'; filter.dispatchEvent(new Event('change'));`, tool.context);
      assert.equal((await evaluate(books, tool.context)).trim(), '已读：并发新增记录');
      const history = (await appRequest(versionPath)).body;
      assert.deepEqual(history.items.map(item => item.version), [2, 1]);
      assert.equal(history.current, 2);
      assert.equal(history.items[0].note, '增加已读筛选');
      if (await evaluate('document.querySelector("#tool-version-panel").hidden')) await click('#tool-history');
      await until(() => evaluate('document.querySelectorAll("#tool-version-list li").length === 2'));
      assert.equal(await evaluate(`document.querySelector('#tool-version-panel').getBoundingClientRect().left >
        document.querySelector('.elys-preview').getBoundingClientRect().right`), true);
      await screenshot('tool-version-history');
      await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      assert.equal(await evaluate(`document.querySelector('#tool-version-panel').getBoundingClientRect().top >
        document.querySelector('.elys-preview').getBoundingClientRect().bottom &&
        document.documentElement.scrollWidth === document.documentElement.clientWidth`), true);
      await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
      await click('#revise-tool');
      await generate('回归：修改输出失败');
      assert.match(await until(() => evaluate('document.querySelector("#error").textContent')), /模型输出不完整/);
      assert.deepEqual((await appRequest(versionPath)).body, history);
      await logout();
      await login(accountA);
      tool = await activeTool();
      assert.equal(tool.id, first.id);
      assert.match(await evaluate('document.querySelector("#list").innerText', tool.context), /独立浏览器保存的书/);
      assert.match(await evaluate(books, tool.context), /已读：并发新增记录/);
      assert.equal(await evaluate('Boolean(document.querySelector("#read-filter"))', tool.context), true);
      assert.deepEqual((await appRequest(versionPath)).body, history);
      evidence.tests.push({ id: 'TEST-007', result: 'passed', sameTool: true, crossBrowserWriteDuringPreview: true,
        incompatibleDraftRetainedOldVersion: true, filterVerified: true });
      evidence.tests.push({ id: 'TEST-008', result: 'passed', versions: [2, 1], restoredAfterLogin: true,
        failedRevisionDidNotPublish: true });
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
      await login(accountB, !entryFlow);
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
  if (entryFlow) {
    stage = 'explicit direct development submits one edited requirement';
    const before = (await library()).items;
    const previous = await evaluate('document.querySelector("#preview iframe[data-tool]")?.dataset.tool || ""');
    await monitorGeneration();
    await click('#new-tool');
    await click('#direct-entry');
    assert.equal(await visible('#requirement'), true);
    await evaluate('document.querySelector("#requirement").value = ""');
    const custom = '做一个夜读笔记的读书清单，支持新增书名并保存。';
    await type('#requirement', custom);
    assert.equal(await evaluate('document.querySelector("#generate").textContent'), '直接生成');
    assert.deepEqual(await evaluate('window.testGenerationPosts'), []);
    await click('#generate');
    const directTool = await activeTool(previous);
    await recordRun(directTool);
    assert.deepEqual(await evaluate('window.testGenerationPosts'), [custom]);
    assert.equal((await library()).items.length, before.length + 1);
    evidence.tests.push({ id: 'TEST-011', result: 'passed', sampleTemplates: ['board', 'list', 'budget'],
      zeroSampleGenerations: true, noSavedSampleTools: true, customizeWithoutRequest: true,
      templateGenerationRequests: 1, directGenerationRequests: 1 });
  }
  if (live && process.argv.includes('--analysis-live')) {
    const initialBalance = (await appRequest('/api/trial')).body;
    assert.equal(initialBalance.analysisMode, 'deepseek');
    await click('#new-tool');
    await click('#idea-entry');
    const idea = '#assistant-idea-text';
    async function analyzeIdea(value, result) {
      await evaluate(`document.querySelector(${JSON.stringify(idea)}).value = ""`);
      await type(idea, value);
      await click('#assistant-analyze button');
      await until(async () => {
        const error = await evaluate('document.querySelector("#assistant-status.elys-error")?.textContent');
        if (error) throw new Error(error);
        return visible(result);
      }, 35000);
    }
    stage = 'DeepSeek analysis of a clear idea reaches an editable draft';
    await analyzeIdea('做一个社区图书借阅清单。记录书名和借阅人，能新增借阅并标记已归还，刷新后保留记录。',
      '#assistant-draft-editor');
    assert.match(await evaluate('document.querySelector("#assistant-draft-editor").value'), /借阅|归还/);
    await screenshot('live-analysis-draft');
    await click('#assistant-edit-idea');
    stage = 'DeepSeek analysis of a vague idea presents directions';
    await analyzeIdea('每周都在群里追问项目进度，经常不知道哪里卡住了。', '#assistant-directions');
    const count = await evaluate('document.querySelectorAll("#assistant-directions [data-direction]").length');
    assert.ok(count >= 2 && count <= 3);
    await screenshot('live-analysis-directions');
    const finalBalance = (await appRequest('/api/trial')).body;
    assert.equal(finalBalance.analysis.used, initialBalance.analysis.used + 2);
    assert.deepEqual(finalBalance.generation, initialBalance.generation);
    evidence.tests.push({id: 'TEST-012-live', result: 'passed', clearDraft: true, directions: count,
      analysisRequests: 2, generationBalanceUnchanged: true});
  }
  const setStage = name => { stage = name; };
  const slices = process.argv.includes('--slices');
  if (!live && process.argv.includes('--create-recovery')) {
    const { verifyCreateRecovery } = await import('./slice-create-recovery.mjs');
    evidence.tests.push(await verifyCreateRecovery({
      evaluate, click, type, until, cdp, activeTool, appRequest, library, screenshot, stage: setStage,
    }));
  }
  if (!live && (slices || process.argv.includes('--assistant-flow'))) {
    const { verifyAssistant } = await import('./slice-assistant.mjs');
    evidence.tests.push(await verifyAssistant({
      evaluate, click, type, until, cdp, activeTool, appRequest, library, login, logout, accountA, accountB,
      screenshot, stage: setStage, accountBRegistered: !mainOnly,
    }));
  }
  if (!live && (slices || process.argv.includes('--table-flow'))) {
    const { verifyCsv } = await import('./slice-csv.mjs');
    evidence.tests.push(await verifyCsv({
      evaluate, click, type, until, cdp, activeTool, appRequest, library, stored, selectHistory,
      screenshot, stage: setStage,
    }));
  }
  }
  evidence.status = 'passed';
} catch (error) {
  evidence.status = 'failed';
  evidence.error = error.message;
  evidence.failedStage = stage;
  if (runtimeErrors.length) evidence.runtimeErrors = runtimeErrors;
  if (readPage) evidence.page = await readPage().catch(() => null);
  if (stagedFlow) {
    const usage = await readFile(join(root, 'runs', 'staged-usage.jsonl'), 'utf8').catch(() => '');
    if (usage.trim()) evidence.runs = usage.trim().split('\n').map(JSON.parse);
  }
  process.exitCode = 1;
} finally {
  if (writeCoverage) await writeCoverage().catch(error => { console.error(error.message); process.exitCode = 1; });
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
