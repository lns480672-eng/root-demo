// TEST-001 / TEST-002 / TEST-004 / TEST-005 / TEST-006. Default uses a deterministic provider fixture. --live uses DeepSeek.
import assert from 'node:assert/strict';
import { verifyTableJourney } from './verify-table-journey.mjs';
import { verifyStagedFlow, verifyStagedCalculator, verifyStagedRegistration, verifyStagedRevision, verifyStagedReconnect, verifyLedgerTheme, verifyStagedQuotaFeedback } from './verify-staged-flow.mjs';
import { execFileSync, spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const live = process.argv.includes('--live');
const stagedFlow = process.argv.includes('--staged-flow');
const ledgerTheme = stagedFlow && process.argv.includes('--ledger-theme');
const demoSite = stagedFlow && process.argv.includes('--demo-site');
const draftNaming = process.argv.includes('--draft-naming');
const stagedHistory = stagedFlow && process.argv.includes('--staged-history');
const quotaFeedback = process.argv.includes('--quota-feedback');
if (quotaFeedback && (!stagedFlow || process.env.ROOT_STAGED_LIVE === '1')) throw new Error('--quota-feedback requires the staged fixture');
if (live && stagedFlow) throw new Error('--staged-flow uses ROOT_STAGED_LIVE=1 for real staged generation');
const liveRevision = live && process.argv.includes('--revision');
const collectCoverage = process.argv.includes('--coverage');
const mainOnly = process.argv.includes('--main');
const entryFlow = process.argv.includes('--entry-flow');
const adminFlow = process.argv.includes('--admin-flow');
const adminSetup = process.argv.includes('--admin-setup');
const profileFlow = process.argv.includes('--profile-flow');
if ((entryFlow || adminFlow || adminSetup) && live) throw new Error('选定的流程使用模型替身，请移除 --live');
if (adminSetup && (adminFlow || stagedFlow)) throw new Error('--admin-setup requires a fresh instance without a legacy administrator');
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
    const fixture = stagedFlow || draftNaming ? 'tests/staged_fixtures.py' : 'tests/fixtures.py';
    const server = launch('python3', [fixture, 'fixture-key', '0', join(root, 'runs'), probeUrl,
      ...(adminFlow ? ['--admin-flow'] : []), ...(entryFlow ? ['--entry-flow'] : []),
      ...(ledgerTheme ? ['--ledger-theme'] : []), ...(quotaFeedback ? ['--quota-feedback'] : []),
      ...(demoSite ? ['--demo-site'] : []), ...(stagedHistory ? ['--staged-history'] : [])]);
    const [line] = await Promise.race([once(server.stdout, 'data'), once(server, 'exit').then(() => [''])]);
    base = String(line).match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
    assert.ok(base, '工作台应启动并提供本机地址');
  }
  const config = await (await fetch(base + '/api/config', { signal: AbortSignal.timeout(5000) })).json();
  evidence.build = config.build;
  assert.equal(config.configured, true, '需要配置本地工作台的 DeepSeek Key');
  assert.deepEqual(Object.keys(config).sort(), stagedFlow || draftNaming ? ['build', 'configured', 'staged'] : ['build', 'configured']);
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
  const coverageSources = ['/workbench.js', '/assistant.js', '/table.js'];
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

  async function activeTool(previousId = '', selector = '#preview iframe[data-tool]') {
    return until(async () => {
      const id = await evaluate(`document.querySelector('${selector}')?.checkVisibility() ? document.querySelector('${selector}').dataset.tool || 'trial' : null`);
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
    }, live ? 135000 : stagedFlow ? 20000 : 5000);
  }

  async function type(selector, value, context) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`, context);
    await cdp('Input.insertText', { text: value });
  }

  async function click(selector, context) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`, context);
  }

  async function pointerClick(selector, context) {
    if (context) await evaluate('document.querySelector("#preview iframe[data-tool]").scrollIntoView({block:"nearest",inline:"nearest"})');
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

  async function login(username, register = false, nickname = username) {
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
        !document.querySelector("#workspace").hidden && document.querySelector("#account-name").textContent === ${JSON.stringify(nickname)}`);
    });
    if (register) assert.equal(await evaluate('window.fixtureRegistrationDocument'), true, '首次引导应从同一注册页面进入');
  }

  async function logout() {
    await sampleCoverage();
    if (!await visible('#logout')) await click('#account-name');
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
  async function escapeCoachmark() {
    await cdp('Input.dispatchKeyEvent', {type:'rawKeyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await cdp('Input.dispatchKeyEvent', {type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    assert.equal(await evaluate('document.querySelector("#entry-coachmark").hidden'), true);
  }

  async function openGuide() {
    await click('#account-name');
    await click('#guide-start');
    await until(() => visible('#entry-coachmark'));
  }

  async function assertCenteredNonWorkspaceShell(route) {
    assert.equal(await visible(route), true);
    assert.equal(await evaluate(`(() => {
      const product = document.querySelector('.elys-product');
      const shell = product.getBoundingClientRect();
      const right = document.documentElement.clientWidth - shell.right;
      return document.querySelector('#workspace').hidden &&
        shell.left > 0 && Math.abs(shell.left - right) <= 1 &&
        getComputedStyle(product).maxWidth === '1350px';
    })()`), true);
  }

  async function openLatest() {
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    await click('#library-entry');
    return activeTool();
  }

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
  if (profileFlow) {
    stage = 'AC-022 personal profile';
    const username = 'profile-' + 'a'.repeat(24);
    const displayName = '甲'.repeat(20) + ' 😀甲';
    const text = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).textContent`);
    const openProfile = async () => {
      await click('#account-name');
      await click('#profile-link');
      await until(() => evaluate('document.querySelector("#edit-nickname").checkVisibility() && !document.querySelector("#edit-nickname").disabled'));
    };
    const value = nickname => evaluate(`document.querySelector("#nickname").value = ${JSON.stringify(nickname)}`);
    const save = () => click('#profile-form button[type="submit"]');
    await cdp('Page.navigate', {url:base});
    await login(username, true);
    if (await visible('#workspace-guide')) await click('#guide-skip');
    await openProfile();
    assert.equal(await text('#profile-username'), username);
    assert.equal(await visible('#profile-form'), false);
    const quota = (await appRequest('/api/trial')).body;
    assert.equal(await text('#profile-generation'), `${quota.generation.remaining} / ${quota.generation.limit}`);
    assert.equal(await text('#profile-analysis'), `${quota.analysis.remaining} / ${quota.analysis.limit}`);
    await click('#edit-nickname');
    assert.equal(await evaluate('document.querySelector("#nickname").value'), username);
    await save();
    await click('#edit-nickname');
    await value('放弃更改');
    await click('#cancel-nickname');
    assert.equal(await text('#profile-nickname'), username);
    await click('#edit-nickname');
    await value(displayName + '末');
    await save();
    assert.match(await text('#profile-status'), /最多 24/);
    assert.equal(await text('#account-name'), username);
    await value(' ' + displayName + ' ');
    await save();
    await until(async () => (await text('#account-name')) === displayName);
    await screenshot('profile-desktop');
    await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
    await cdp('Page.reload');
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    assert.equal(await text('#profile-nickname'), displayName);
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    await screenshot('profile-390');
    await logout();
    await login(username, false, displayName);
    if (!await visible('#profile')) await openProfile();
    await click('#edit-nickname');
    await value('   ');
    await save();
    await until(async () => (await text('#account-name')) === username);
    await click('#new-tool');
    assert.equal(await visible('#profile'), false);
    await logout();
    await login(accountB, true);
    if (await visible('#workspace-guide')) await click('#guide-skip');
    await openProfile();
    assert.equal(await text('#profile-nickname'), accountB);
    evidence.tests.push('AC-022: 额度、预填、取消、长度、保存、刷新、重登、清空和账号隔离');
  } else if (draftNaming) {
    stage = 'AC-011/012/013 named requirement handoff';
    await cdp('Page.navigate', {url:base});
    await login(accountA, true);
    if (await visible('#workspace-guide')) await click('#guide-skip');
    const oldId = 'ed4eed11-3ff4-4268-8df1-76c4a30dab08';
    const body = '做一个读书清单，可以新增书名并保存。';
    const path = '/api/requirement-drafts/' + oldId;
    assert.equal((await appRequest(path, {text:body, revision:0})).status, 200);
    for (const width of [1440, 390]) {
      await cdp('Emulation.setDeviceMetricsOverride', {width,height:844,deviceScaleFactor:1,mobile:width===390});
      await click('#idea-entry');
      assert.equal(await evaluate('document.querySelectorAll(".workspace-navigation nav button").length'), 3);
      assert.equal(await visible('#assistant-draft-name'), false);
      await click('#assistant-drafts');
      await until(() => visible('[data-draft-id="' + oldId + '"]'));
      assert.match(await evaluate('document.querySelector(".assistant-draft-list button").textContent'), /未命名需求/);
      assert.equal(await evaluate('document.querySelector("#idea-entry").getAttribute("aria-current")'), 'page');
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
      await screenshot('requirements-' + width);
      if (width === 1440) { await click('#assistant-idea-tab'); await click('#new-tool'); }
    }
    await click('[data-draft-id="' + oldId + '"]');
    await until(() => visible('#assistant-draft-name'));
    await type('#assistant-draft-name', '图书借阅需求');
    await click('#assistant-save');
    await until(async () => (await appRequest(path)).body.title === '图书借阅需求');
    await click('#assistant-build');
    assert.equal((await appRequest('/api/staged')).body.tasks.length, 0);
    await click('#generate');
    const task = await until(async () => {
      const active = (await appRequest('/api/staged')).body.tasks;
      const archived = (await appRequest('/api/staged/history')).body.tasks;
      return [...active, ...archived].find(item => item.title === '图书借阅需求');
    });
    assert.equal(task.requirement, body);
    await cdp('Page.reload');
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    const readback = (await appRequest('/api/staged/' + task.id)).body.task;
    assert.equal(readback.title, task.title);
    assert.equal(readback.requirement, body);
    evidence.tests.push({id:'AC-011/012/013', title:readback.title, fullText:readback.requirement, task:task.id});
  } else if (stagedFlow) {
    evidence.mode = process.env.ROOT_STAGED_LIVE === '1' ? 'deepseek-staged' : 'fixture-staged';
    const tableJourney = process.argv.includes('--table-journey');
    const calculator = process.env.ROOT_STAGED_CALCULATOR === '1';
    const registration = process.env.ROOT_STAGED_REGISTRATION === '1';
    const revision = process.env.ROOT_STAGED_REVISION === '1';
    const reconnect = process.argv.includes('--reconnect-fallback');
    stage = demoSite ? 'CHG-046 website generation' : stagedHistory ? 'TEST-018-09 generation history' :
      quotaFeedback ? 'TEST-017-06 shared generation quota feedback' : ledgerTheme ? 'ROOT-REV-VIS-01 ledger theme' : reconnect ? 'AC-020 staged reconnect fallback' : tableJourney ? 'CHG-029 same imported tool journey' : registration ? 'staged workbench registration' : revision ? 'staged workbench revision' :
      calculator ? 'staged workbench calculator' : 'staged workbench board flow';
    const verify = demoSite ? (await import('./verify-demo-site.mjs')).verifyDemoSite :
      stagedHistory ? (await import('./verify-staged-flow.mjs')).verifyStagedHistory :
      quotaFeedback ? verifyStagedQuotaFeedback : ledgerTheme ? verifyLedgerTheme : reconnect ? verifyStagedReconnect : tableJourney ? verifyTableJourney : registration ? verifyStagedRegistration : revision ? verifyStagedRevision :
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
  if (process.argv.includes('--workbench-base-path')) {
    stage = 'shared-domain workbench navigation and reload';
    await cdp('Network.enable');
    for (const home of ['/workbench', '/']) {
      await cdp('Network.setExtraHTTPHeaders', {headers: home === '/' ? {} : {'X-Root-Workbench-Path': home}});
      await cdp('Page.navigate', {url:base + '/updates'});
      await until(() => visible('#updates-back'));
      assert.equal(await evaluate('document.querySelector("#updates-back").getAttribute("href")'), home);
      await click('#updates-back');
      await until(() => evaluate(`location.pathname === ${JSON.stringify(home)}`));
      await until(() => visible('#auth'));
      const previousDocument = await evaluate('performance.timeOrigin');
      await cdp('Page.reload');
      await until(() => evaluate(`performance.timeOrigin !== ${previousDocument} && document.documentElement.dataset.ready === "true"`));
      assert.equal(await evaluate('location.pathname'), home);
      assert.equal(await visible('#auth'), true);
      assert.equal(await evaluate('document.querySelector("#auth-home").href'),
        home === '/workbench' ? base + '/' : 'http://[::1]:8788/');
      await cdp('Page.navigate', {url:base + '/admin'});
      await until(() => visible('.workbench-link'));
      assert.equal(await evaluate('document.querySelector(".workbench-link").getAttribute("href")'), home);
      await click('.workbench-link');
      await until(() => visible('#auth'));
      assert.equal(await evaluate('location.pathname'), home);
    }
    evidence.tests.push({id:'AC-003-workbench-base-path', directUpdates:true, reload:true, adminReturn:true,
      supportedPaths:['/workbench', '/']});
  }
  if (adminSetup) {
    stage = 'administrator setup, invite and password change';
    const code = execFileSync('python3', ['-B', 'workbench.py', '--runs-dir', join(root, 'runs'),
      '--create-admin-setup-code'], { encoding: 'utf8', env: { ...process.env, ROOT_TRIAL_ADMINS: '' } }).trim();
    await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await cdp('Page.navigate', { url: base + '/admin' });
    await until(() => visible('#setup-form'));
    await screenshot('admin-first-setup');
    await type('#setup-code', code);
    await type('#setup-username', 'qa-admin-owner');
    await type('#setup-password', password);
    await type('#setup-password-again', password);
    await click('#setup-submit');
    await until(() => visible('#invite-generate'));
    await click('#invite-generate');
    await until(() => visible('#invite-result'));
    const trialInvite = await evaluate('document.querySelector("#issued-code").value');
    await click('#back-to-workbench');
    await until(() => visible('#auth-mode'));
    await click('#auth-mode');
    await type('#invite', trialInvite);
    await type('#username', 'qa-admin-trial');
    await type('#password', password);
    await click('#auth-submit');
    await until(() => evaluate('document.querySelector("#account-name")?.textContent === "qa-admin-trial"'));
    await cdp('Page.navigate', { url: base + '/admin#security' });
    await until(() => visible('#security-nav'));
    await click('#security-nav');
    await until(() => visible('#password-form'));
    await screenshot('admin-password-change');
    const newPassword = 'new-root-browser-test-only!';
    await type('#current-password', password);
    await type('#new-password', newPassword);
    await type('#new-password-again', newPassword);
    await click('#password-submit');
    await until(() => visible('#admin-login-submit'));
    await screenshot('admin-independent-login');
    await type('#admin-username', 'qa-admin-trial');
    await type('#admin-password', password);
    await click('#admin-login-submit');
    await until(() => evaluate('document.querySelector("#admin-login-feedback").textContent.includes("没有管理员权限")'));
    await evaluate('document.querySelector("#admin-username").value = ""');
    await type('#admin-username', 'qa-admin-owner');
    await type('#admin-password', newPassword);
    await click('#admin-login-submit');
    await until(() => visible('#password-form'));
    assert.equal(await evaluate('fetch("/api/session").then(r => r.json()).then(x => x.user)'), 'qa-admin-trial');
    await click('#admin-logout');
    await until(() => visible('#admin-login-submit'));
    assert.equal(await evaluate('fetch("/api/session").then(r => r.json()).then(x => x.user)'), 'qa-admin-trial');
    await cdp('Page.navigate', { url: base });
    await until(() => visible('#workspace'));
    await logout();
    await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
    evidence.tests.push('TEST-021-04 窄屏首次设置、发码注册、同浏览器保留试用账号、后台改密重新登录及单独退出。');
  }
  if (adminFlow) {
    const { verifyAdmin } = await import('./slice-admin.mjs');
    evidence.tests.push(...await verifyAdmin({
      evaluate, click, pointerClick, type, until, cdp, login, logout, visible, appRequest, screenshot,
      stage: name => { stage = name; },
    }));
  }
  if (!live) {
    stage = 'account visual layout and registration feedback';
    assert.equal(await evaluate('document.querySelector("#auth-intro-title").innerText.replace(/\\s/g, "")'), '把手头的事，做成小工具。');
    assert.equal(await evaluate('document.querySelector("#auth-intro > p").textContent'), '从一个想法、明确需求或已有表格开始。');
    assert.equal(await evaluate('document.querySelector("#auth-title").textContent'), '登录工作台');
    assert.equal(await evaluate('document.querySelector(".auth-illustration")'), null);
    assert.equal(await visible('#auth-home'), true);
    assert.equal(await evaluate('document.querySelector("#auth-home").href'), 'http://[::1]:8788/');
    assert.equal(await evaluate('document.querySelector(".auth-panel").getBoundingClientRect().width <= 430'), true);
    await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await screenshot('account-login-390');
    await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
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
      illustration: Boolean(document.querySelector('.auth-illustration')),
      belowTitle: document.querySelector('#auth-form').getBoundingClientRect().top >
        document.querySelector('#auth-intro').getBoundingClientRect().bottom,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    })`), { illustration: false, belowTitle: true, overflow: false });
    assert.equal(await evaluate(`matchMedia('(pointer: coarse)').matches &&
      [...document.querySelectorAll('#auth-form input, #auth-submit')]
        .every(element => element.getBoundingClientRect().height >= 44)`), true);
    assert.equal(await evaluate('document.querySelector("#auth-title").textContent'), '凭邀请码注册');
    await screenshot('account-register-invalid-390');
    await click('#auth-mode');
    await click('#auth-mode');
    await screenshot('account-register-390');
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
    stage = 'TEST-011/016 registration, navigation, and centered entry';
    assert.deepEqual(await evaluate('Array.from(document.querySelectorAll("#new-tool-options button strong"), node => node.textContent)'), ['需求助手', '从表格生成']);
    assert.deepEqual(await evaluate('Array.from(document.querySelectorAll("#requirement-examples button"), node => node.textContent)'), ['试试读书清单', '试试 AA 分账', '试试交付跟进板']);
    const navOrder = ['新建工具', '需求', '我的工具'];
    assert.deepEqual(await evaluate('Array.from(document.querySelectorAll(".workspace-navigation nav button"), button => button.textContent)'), navOrder);
    assert.equal(await visible('#requirement'), true);
    assert.equal(await visible('#entry-coachmark'), true);
    assert.equal(await visible('.elys-preview'), false);
    assert.equal(await evaluate('document.querySelector("#generate").textContent'), '开始');
    await screenshot('first-visit');
    for (const width of [1440, 1920, 390]) {
      await cdp('Emulation.setDeviceMetricsOverride', {width, height:980, deviceScaleFactor:1, mobile:width === 390});
      const layout = await evaluate(`(() => {
        const shell = document.querySelector('.elys-product').getBoundingClientRect();
        const workspace = document.querySelector('#workspace');
        const nav = document.querySelector('.workspace-sidebar');
        const contentLeft = nav.getBoundingClientRect().right + parseFloat(getComputedStyle(workspace).columnGap);
        const input = document.querySelector('#composer').getBoundingClientRect();
        const contentRight = workspace.getBoundingClientRect().right;
        return {
          left: shell.left,
          right: document.documentElement.clientWidth - shell.right,
          centerOffset: Math.abs((input.left + input.right - contentLeft - contentRight) / 2),
          navigationOnLeft: nav.checkVisibility() && contentLeft <= input.left + 1,
          coachmarkAboveExamples: document.querySelector('#entry-coachmark').getBoundingClientRect().bottom <=
            document.querySelector('#requirement-examples').getBoundingClientRect().top,
          modeColumns: getComputedStyle(document.querySelector('.mode-cards')).gridTemplateColumns.split(' ').length,
          overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth || input.right > shell.right,
        };
      })()`);
      assert.equal(layout.navigationOnLeft, true, '导航保持在左侧，视口 ' + width);
      assert.equal(layout.left, 0, '工作台左侧贴边，视口 ' + width);
      assert.equal(layout.right, 0, '工作台右侧贴边，视口 ' + width);
      assert.ok(layout.centerOffset <= 1, '输入区在导航右侧居中，视口 ' + width);
      assert.equal(layout.coachmarkAboveExamples, true, '引导不遮挡示例，视口 ' + width);
      assert.equal(layout.modeColumns, 2, '两张入口卡片并排，视口 ' + width);
      assert.equal(layout.overflow, false, '工作台无横向溢出，视口 ' + width);
      await screenshot('workspace-edge-' + width);
    }
    await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
    await cdp('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    assert.equal(await evaluate('getComputedStyle(document.querySelector("#entry-coachmark")).animationName'), 'none');
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    assert.equal(await evaluate(`(() => {
      const nav = document.querySelector('.workspace-sidebar').getBoundingClientRect();
      const prompt = document.querySelector('#composer').getBoundingClientRect();
      return nav.right <= prompt.left && getComputedStyle(document.querySelector('.mode-cards')).gridTemplateColumns.split(' ').length === 2;
    })()`), true);
    await screenshot('guide-narrow-reduced-motion');
    await escapeCoachmark();
    await openGuide();
    await type('#requirement', '引导关闭后保留输入');
    assert.equal(await evaluate('document.querySelector("#entry-coachmark").hidden'), true);
    assert.equal(await evaluate('document.querySelector("#requirement").value'), '引导关闭后保留输入');
    await cdp('Emulation.setEmulatedMedia', {features:[]});
    await cdp('Emulation.setDeviceMetricsOverride', {width:1360,height:980,deviceScaleFactor:1,mobile:false});

    stage = 'returning account uses the same page without an automatic coachmark';
    await sampleCoverage();
    const navigation = await evaluate('performance.timeOrigin');
    await cdp('Page.reload', { ignoreCache: true });
    await until(() => evaluate(`performance.timeOrigin !== ${navigation} && document.documentElement.dataset.ready === "true"`));
    assert.equal(await visible('#entry-coachmark'), false);
    assert.equal(await visible('#requirement'), true);
    const primary = socket;
    socket = await connectBrowser(join(root, 'chrome-guide-independent'));
    await startCoverage();
    await cdp('Page.enable');
    await cdp('Page.navigate', { url: base });
    await login(accountA);
    assert.equal(await visible('#entry-coachmark'), false);
    await logout();
    await login(accountB, true);
    assert.equal(await visible('#entry-coachmark'), true);
    await escapeCoachmark();
    await sampleCoverage();
    socket = primary;

    stage = 'new tool cards open the assistant and table';
    await click('#assistant-entry');
    await until(() => visible('#assistant-panel'));
    await click('#new-tool');
    await click('#table-entry');
    await until(() => visible('#table-panel'));
    await click('#table-exit');
    await until(() => visible('#requirement'));
    assert.deepEqual((await library()).items, []);
    await monitorGeneration();
    const beforeExamples = (await appRequest('/api/trial')).body.generation;
    for (const [id, expected] of [['example-reading', '读书清单'], ['example-split', 'AA 分账'], ['example-board', '交付跟进板']]) {
      await click('#' + id);
      const input = await evaluate('document.querySelector("#requirement").value');
      assert.match(input, new RegExp(expected));
      assert.equal(await evaluate('document.activeElement.id'), 'requirement');
      await type('#requirement', '。可编辑');
      assert.equal(await evaluate('document.querySelector("#requirement").value.endsWith("可编辑")'), true);
      assert.deepEqual(await evaluate('window.testGenerationPosts'), []);
    }
    assert.deepEqual((await appRequest('/api/trial')).body.generation, beforeExamples);
    await evaluate('document.querySelector("#requirement").value = ""; document.querySelector("#requirement").dispatchEvent(new Event("input"));');
  }
  assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
  await type('#requirement', '   ');
  assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
  await evaluate('document.querySelector("#requirement").value = ""');
  await type('#requirement', requirements[0]);
  assert.equal(await evaluate('document.querySelector("#generate").disabled'), false);
  stage = 'generate reading tool';
  await click('#generate');
  const first = await activeTool();
  await recordRun(first);
  assert.match(await evaluate('document.querySelector("h1").innerText', first.context), /读书清单/);
  if (entryFlow) {
    assert.match(await evaluate('window.testGenerationPosts.join(" ")'), /读书清单/);
    assert.equal(await evaluate('window.testGenerationPosts.length'), 1);
    assert.deepEqual((await library()).items.map(item => item.id), [first.id]);
    stage = 'first tool stays on the generation page after saving';
    assert.equal(await visible('#requirement'), true);
    assert.equal(await evaluate('document.querySelector("#requirement").value'), requirements[0]);
    assert.equal(await visible('#open-generated-tool'), true);
    assert.equal(await evaluate('document.querySelector("#new-tool").getAttribute("aria-current")'), 'page');
    assert.equal(await visible('#entry-coachmark'), false);
    assert.equal(await visible('#generation-saved'), true);
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
    stage = 'first saved tool is available from the library';
    await click('#open-generated-tool');
    assert.equal(await visible('#requirement'), false);
    operatedTool = await activeTool();
    assert.equal(operatedTool.id, first.id);
    assert.match(await evaluate('document.querySelector("#list").innerText', operatedTool.context), /星际旅行手记/);
    assert.equal(await visible('#entry-coachmark'), false);
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
  await until(() => evaluate('performance.getEntriesByType("navigation")[0]?.type === "reload" && document.documentElement.dataset.ready === "true"'));
  assert.equal(await visible('#requirement'), true);
  assert.equal(await visible('.elys-preview'), false);
  let restored = await openLatest();
  assert.equal(restored.id, first.id);
  assert.notEqual(restored.loader, operatedTool.loader);
  assert.equal(await visible('.elys-composer'), false, '刷新恢复已有工具后隐藏新建区');
  assert.match(await evaluate('document.querySelector("#list").innerText', restored.context), /星际旅行手记/);
  if (entryFlow) {
    assert.equal(await visible('#entry-coachmark'), false, '回访不再自动引导');
    evidence.tests.push({ id: 'TEST-016-03/04', result: 'passed', autoShownOncePerAccountAcrossBrowsers: true,
      secondFreshAccountShown: true, skippedAndReopened: true, sampleUncounted: true, firstSavedEndsGuide: true,
      reducedMotionAndNarrowScreen: true, escapeAndFocusDismiss: true, dismissalRetainsInput: true, foundSameTool: true, dismissedAfterReload: true });
  }
  evidence.tests.push({ id: 'TEST-002', result: 'passed', tool: first.id, record: sample,
    reloadedDocument: true, isolationFixtureChecked: !live });
  await screenshot('record-restored');

  if (process.argv.includes('--record-save-status')) {
    stage = 'TEST-002-A/B/C record saving acknowledgement';
    const saveState = () => evaluate('document.querySelector("#record-save-status")?.dataset.state || ""');
    const add = async name => {
      await type('#book', name, restored.context);
      await click('form button', restored.context);
      assert.match(await evaluate('document.querySelector("#list").innerText', restored.context), new RegExp(name));
    };
    const intercept = () => evaluate(`(() => {
      const fetch = window.fetch;
      window.saveReleases = [];
      window.fetch = async (path, options) => {
        if (options?.method !== 'POST' || !String(path).endsWith('/data')) return fetch(path, options);
        const outcome = await new Promise(resolve => window.saveReleases.push(resolve));
        if (outcome === 'rejected') {
          return new Response(JSON.stringify({code:'revision_conflict', message:'模拟明确拒绝'}), {
            status:409, headers:{'Content-Type':'application/json'}
          });
        }
        const response = await fetch(path, options);
        if (outcome === 'unknown') throw new TypeError('模拟服务端已写入但回执丢失');
        return response;
      };
    })()`);
    const waitForRequest = count => until(() => evaluate('window.saveReleases.length === ' + count));
    const release = (index, result) => evaluate('window.saveReleases[' + index + '](' + JSON.stringify(result) + ')');
    const assertStatus = async (state, text) => {
      assert.equal(await saveState(), state);
      assert.match(await evaluate('document.querySelector("#record-save-status").textContent'), text);
    };

    await intercept();
    await add('已保存队列第一条');
    await waitForRequest(1);
    await assertStatus('pending', /正在保存.*请等保存完成再刷新/s);
    await screenshot('record-saving-desktop');
    await add('已保存队列第二条');
    await release(0, 'saved');
    await waitForRequest(2);
    await assertStatus('pending', /正在保存/);
    await cdp('Emulation.setDeviceMetricsOverride', {width:390, height:844, deviceScaleFactor:1, mobile:true});
    assert.equal(await visible('#record-save-status'), true);
    await screenshot('record-saving-390');
    await release(1, 'saved');
    await until(async () => (await saveState()) === 'saved');
    await assertStatus('saved', /已保存.*我的工具/s);
    await screenshot('record-saved-390');

    async function reopen(delayStartup = false) {
      const before = await evaluate('performance.timeOrigin');
      let startupScript;
      if (delayStartup) {
        startupScript = await cdp('Page.addScriptToEvaluateOnNewDocument', {source: `
          if (window === top) {
            const fetchFromWorkbench = window.fetch;
            window.fetch = async (url, options) => {
              if (url === '/api/staged' && !window.releaseStartupTasks) {
                await new Promise(resolve => { window.releaseStartupTasks = resolve; });
                return new Response(JSON.stringify({tasks:[]}), {
                  headers:{'Content-Type':'application/json'},
                });
              }
              const response = await fetchFromWorkbench(url, options);
              if (url !== '/api/config' || !response.ok) return response;
              return new Response(JSON.stringify({...await response.json(), staged:true}), {
                headers:{'Content-Type':'application/json'},
              });
            };
          }
        `});
      }
      await cdp('Page.reload', {ignoreCache:true});
      if (startupScript) {
        await until(() => evaluate('performance.timeOrigin !== ' + before
          + ' && typeof window.releaseStartupTasks === "function" && Boolean(document.querySelector("#tool-list button"))'));
        await cdp('Page.removeScriptToEvaluateOnNewDocument', startupScript);
        assert.equal(await evaluate('document.querySelector("#library-entry").disabled'), true,
          '启动期间工具列表入口不可提前操作');
        assert.equal(await evaluate('document.querySelector("#tool-list button").disabled'), true,
          '启动期间已加载的工具不可提前操作');
        await evaluate('window.releaseStartupTasks()');
      }
      await until(() => evaluate('performance.timeOrigin !== ' + before + ' && document.documentElement.dataset.ready === "true"'));
      restored = await openLatest();
      assert.equal(restored.id, first.id);
      const books = await evaluate('document.querySelector("#list").innerText', restored.context);
      assert.match(books, /星际旅行手记.*已保存队列第一条.*已保存队列第二条/s);
      return books;
    }

    await reopen(true);
    await intercept();
    await add('明确失败不保留');
    await waitForRequest(1);
    await release(0, 'rejected');
    await until(async () => (await saveState()) === 'failed');
    await assertStatus('failed', /保存失败.*这次更改尚未保存/s);
    await screenshot('record-save-rejected');
    assert.doesNotMatch(await reopen(), /明确失败不保留/);

    await intercept();
    await add('回执丢失可核对');
    await waitForRequest(1);
    await release(0, 'unknown');
    await until(async () => (await saveState()) === 'unknown');
    await assertStatus('unknown', /无法确认是否已保存.*我的工具/s);
    await screenshot('record-save-unknown');
    assert.match(await reopen(), /回执丢失可核对/);
    evidence.tests.push({id:'TEST-002-A/B/C', queueWaitedForLatest:true, savedRowsReopened:true,
      rejectedWriteAbsent:true, missingAcknowledgementSeparated:true, widths:[1440,390]});
  }

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
    updated = await openLatest();
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
    await openGuide();
    assert.equal(await visible('#saved-tools'), false);
    await click('#account-name');
    await click('#updates-link');
    await until(() => evaluate('location.pathname === "/updates" && !document.querySelector("#updates").hidden'));
    await until(() => evaluate('document.querySelectorAll("#release-list li").length >= 2'));
    const config = await appRequest('/api/config');
    assert.equal(await evaluate('document.querySelector("#release-list li[aria-current=true] code").textContent'),
      config.body.build);
    assert.equal(await evaluate('document.querySelector("#workspace").hidden'), true);
    if (entryFlow) await assertCenteredNonWorkspaceShell('#updates');
    await screenshot('release-history');
    await click('#updates-back');
    await until(() => evaluate('location.pathname === "/" && !document.querySelector("#workspace").hidden'));
    assert.equal(await evaluate('document.querySelector("#requirement").value'), '返回后保留的草稿');
    assert.equal((await openLatest()).loader, beforeUpdates);
    evidence.tests.push({ id: 'TEST-009', result: 'passed', currentBuild: config.body.build, retainedPreview: true });
  }
  async function assertOldTool() {
    assert.equal(await evaluate('document.querySelector("#preview iframe[data-tool]").dataset.tool'), restored.id);
    assert.equal((await library()).latest, restored.id);
    assert.equal(await visible('.elys-preview'), false, '新建时预览为空');
    assert.equal(await evaluate('document.querySelector("#workspace").dataset.mode'), 'compose');
    assert.equal(await evaluate('document.querySelector("#requirement").value'), requirements[1]);
    assert.match(await evaluate('document.querySelector("#list").innerText', restored.context), /星际旅行手记/);
  }

  async function assertFailure(message) {
    const observed = await until(() => evaluate('document.querySelector("#error").textContent'));
    assert.match(observed, message);
    assert.equal(await visible('#error'), true);
    assert.equal(await evaluate('document.querySelector("#generate").textContent'), '重试');
    await assertOldTool();
    assert.equal(await evaluate('document.querySelectorAll("#preview iframe").length'), 1);
    assert.equal((await library()).items.length, 1);
  }

  async function addOldRecord(name) {
    await type('#book', name, restored.context);
    await click('form button', restored.context);
    await until(() => evaluate('document.querySelector("#list").innerText.includes(' + JSON.stringify(name) + ')', restored.context));
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
    const refreshed = await openLatest();
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
      restored = await openLatest();
      await addOldRecord('失败后新增的书');
      await screenshot('failure-with-old-tool');

      stage = 'reload after failure before candidate switch';
      await sampleCoverage();
      const previousLoader = restored.loader;
      await cdp('Page.reload', { ignoreCache: true });
      await until(() => evaluate('performance.getEntriesByType("navigation")[0]?.type === "reload"'));
      restored = await openLatest();
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

      stage = 'retry keeps the new preview empty in both phases';
      await evaluate('window.holdFixturePreview = true');
      await click('#generate');
      await until(() => evaluate('document.querySelector("#phase").textContent === "正在等待 DeepSeek 响应"'));
      assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
      assert.equal(await visible('#phase'), true);
      await assertOldTool();
      stage = 'add during model wait';
      await until(() => readFile(join(root, 'model-started')).then(() => true, () => false));
      const duplicate = await appRequest('/api/generate', { requirement: requirements[1] });
      assert.equal(duplicate.status, 409);
      assert.equal(duplicate.body.code, 'busy');
      evidence.tests.push({ id: 'concurrent-generation', result: 'passed', secondRequestStatus: 409 });
      await writeFile(join(root, 'release-model'), '');
      stage = 'wait for candidate preview phase';
      await until(() => evaluate('document.querySelector("#phase").textContent === "正在准备预览"'));
      assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
      assert.equal(await visible('#phase'), true);
      await assertOldTool();
      assert.equal(await evaluate('document.querySelectorAll("#preview iframe").length'), 2);
      stage = 'reject storage messages from the hidden preview';
      const beforeForgery = await stored(restored.id);
      await evaluate(`(() => {
        const previous = {source: active.iframe.contentWindow, token: active.token};
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
        interaction: ['old tool reopened after failure'], emptyNewPreview: true });
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
      for (const record of ['星际旅行手记', '失败后新增的书']) {
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
      assert.equal(await visible('#open-generated-tool'), true);
      assert.equal(await evaluate('document.querySelector("#requirement").value'), requirements[1]);
      await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
      await screenshot('second-generated-result-390');
      await click('#open-generated-tool');
      assert.equal((await activeTool()).id, second.id);
      for (const width of [1360, 390]) {
        await cdp('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: width === 390 });
        assert.equal(await visible('.elys-composer'), false, '浏览已保存工具时隐藏整个新建区');
        assert.equal(await visible('#new-tool'), true);
        assert.equal(await visible('#tool-history'), true);
        await evaluate('document.querySelector("#new-tool").focus(); document.querySelector("#requirement").focus()');
        assert.equal(await evaluate('document.activeElement.id'), 'new-tool', '隐藏表单不能获取焦点');
        await cdp('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
        await cdp('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
        assert.equal(await evaluate('document.activeElement.id'), 'idea-entry');
        assert.equal(await evaluate('document.querySelector("#library-entry").getAttribute("aria-current")'), 'page');
        assert.match(await evaluate('document.querySelector("#tool-list button[aria-current=true]").dataset.version'), /^v\d+$/);
        assert.equal(await evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'), false);
        if (width === 390) {
          assert.equal(await evaluate(`document.querySelector('.elys-preview').getBoundingClientRect().left >=
            document.querySelector('.workspace-sidebar').getBoundingClientRect().right`), true);
        }
        await screenshot('library-' + width);

        await click('#new-tool');
        assert.equal(await visible('#table-entry'), true);
        assert.equal(await visible('#idea-entry'), true);
        assert.equal(await evaluate('document.activeElement.id'), 'requirement');
        assert.equal(await visible('.elys-preview'), false);
        await click('#library-entry');
        assert.equal(await visible('.elys-composer'), false);
        assert.equal(await evaluate('document.activeElement.id'), 'library-entry');

        await click('#revise-tool');
        assert.equal(await visible('#requirement'), true);
        assert.equal(await evaluate('document.activeElement.id'), 'requirement');
        assert.match(await evaluate('document.querySelector("#composer-title").textContent'), /修改/);
        for (const id of ['table-entry', 'assistant-entry', 'staged-task-list']) {
          assert.equal(await visible('#' + id), false, '修改时隐藏 ' + id);
        }
        await type('#requirement', '保持这条修改草稿');
        await click('#idea-entry');
        assert.equal(await visible('#assistant-panel'), true);
        assert.equal(await visible('.elys-composer'), false);
        await click('#library-entry');
        await click('#revise-tool');
        assert.equal(await visible('.elys-preview'), true);
        assert.equal(await evaluate(`(() => {
          const composer = document.querySelector('.elys-composer').getBoundingClientRect();
          const preview = document.querySelector('.elys-preview').getBoundingClientRect();
          return preview.width > 0 && preview.height > 0 && (${width} === 390
            ? Math.abs(preview.left - composer.left) <= 1 && preview.top >= composer.bottom
            : preview.left >= composer.right);
        })()`), true, '同屏显示的预览与编辑区分开布局');
        assert.equal(await evaluate('editing'), true);
        assert.equal(await evaluate('document.querySelector("#guide-start").disabled'), true);
        await screenshot('revision-' + width);
        await click('#return-to-library');
        assert.equal(await visible('.elys-composer'), false);
        assert.equal(await evaluate('document.activeElement.id'), 'revise-tool');
        await click('#new-tool');
        assert.equal(await visible('#saved-tools'), false);
        await click('#library-entry');
        await evaluate(`document.querySelector('#tool-list button[data-id="${first.id}"]').focus()`);
        await selectHistory(first.id);
        assert.equal(await visible('.elys-composer'), false);
        assert.equal(await evaluate('document.activeElement.dataset.id'), first.id);
        await selectHistory(second.id);
        const previousNavigation = await evaluate('performance.timeOrigin');
        await cdp('Page.reload', { ignoreCache: true });
        await until(() => evaluate(`performance.timeOrigin !== ${previousNavigation} &&
          document.documentElement.dataset.ready === "true"`));
        const reopened = await openLatest();
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
      let tool = await openLatest();
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
      stage = 'TEST-006-03 cross-browser account restores the last opened tool and records';
      const beforeReturn = await evaluate('performance.timeOrigin');
      await cdp('Page.reload', {ignoreCache: true});
      await until(() => evaluate(`performance.timeOrigin !== ${beforeReturn} &&
        document.documentElement.dataset.ready === "true"`));
      restored = await openLatest();
      assert.equal(restored.id, first.id);
      assert.equal(await visible('#entry-coachmark'), false);
      assert.equal(await evaluate('document.querySelector("#library-entry").getAttribute("aria-current")'), 'page');
      assert.match(await evaluate('document.querySelector("#list").innerText', restored.context), /独立浏览器保存的书/);
      evidence.tests.push({id: 'TEST-006-03', result: 'passed', lastToolSharedByAccount: true, savedRecordRecovered: true});

      stage = 'tool revision incompatibility preserves version and allows writing';
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
      assert.equal(await evaluate(`document.querySelector('.elys-preview').contains(document.querySelector('#tool-version-panel'))`), true);
      await screenshot('tool-version-history');
      await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      assert.equal(await evaluate(`document.querySelector('#tool-version-panel').getBoundingClientRect().top >
        document.querySelector('#preview').getBoundingClientRect().bottom &&
        document.documentElement.scrollWidth === document.documentElement.clientWidth`), true);
      await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });
      await click('#revise-tool');
      await generate('回归：修改输出失败');
      assert.match(await until(() => evaluate('document.querySelector("#error").textContent')), /模型输出不完整/);
      assert.deepEqual((await appRequest(versionPath)).body, history);
      await logout();
      await login(accountA);
      tool = await openLatest();
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

      stage = 'failure preserves two tools that can be reopened';
      await click('#new-tool');
      await generate('回归：强制新建失败');
      await until(() => evaluate('document.querySelector("#error").textContent'));
      assert.equal((await library()).items.length, 2);
      assert.equal(await visible('.elys-preview'), false);
      tool = await openLatest();
      assert.equal(tool.id, second.id);
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
    assert.equal(await visible('#requirement'), true);
    await evaluate('document.querySelector("#requirement").value = ""');
    const custom = '做一个夜读笔记的读书清单，支持新增书名并保存。';
    await type('#requirement', custom);
    assert.equal(await evaluate('document.querySelector("#generate").textContent'), '开始');
    assert.deepEqual(await evaluate('window.testGenerationPosts'), []);
    await cdp('Emulation.setDeviceMetricsOverride', {width:1920,height:980,deviceScaleFactor:1,mobile:false});
    await evaluate(`(() => {
      const monitored = window.fetch;
      window.fetch = (path, options) => String(path).endsWith('/api/generate')
        ? new Promise(resolve => {
          window.releaseLayoutGeneration = () => {
            window.fetch = monitored;
            resolve(monitored(path, options));
          };
        })
        : monitored(path, options);
    })()`);
    await click('#generate');
    await until(() => evaluate('typeof window.releaseLayoutGeneration === "function"'));
    try {
      for (const width of [1920, 390]) {
        await cdp('Emulation.setDeviceMetricsOverride', {width, height:980, deviceScaleFactor:1, mobile:width === 390});
        const layout = await evaluate(`(() => {
          const workspace = document.querySelector('#workspace');
          const shell = document.querySelector('.elys-product').getBoundingClientRect();
          const nav = document.querySelector('.workspace-sidebar');
          const composer = document.querySelector('.elys-composer');
          const preview = document.querySelector('.elys-preview');
          const viewport = document.documentElement;
          const style = getComputedStyle(workspace);
          const columns = style.gridTemplateColumns.split(' ').map(parseFloat).filter(Number.isFinite);
          return {
            mode: workspace.dataset.mode,
            columns: columns.length,
            usableContentTracks: columns.slice(1).every(width => width >= 200),
            composerArea: getComputedStyle(composer).gridArea,
            previewArea: getComputedStyle(preview).gridArea,
            areas: style.gridTemplateAreas,
            navigation: nav.checkVisibility(),
            progress: document.querySelector('#loading').checkVisibility(),
            fits: shell.left === 0 && shell.right === viewport.clientWidth &&
              composer.getBoundingClientRect().right <= shell.right &&
              composer.getBoundingClientRect().left >= nav.getBoundingClientRect().right &&
              composer.scrollWidth <= composer.clientWidth && workspace.scrollWidth <= workspace.clientWidth &&
              viewport.scrollWidth === viewport.clientWidth,
          };
        })()`);
        assert.deepEqual(layout, {
          mode:'compose', columns:width === 390 ? 2 : 3,
          usableContentTracks:true, composerArea:'composer', previewArea:'content',
          areas:width === 390 ? '"sidebar composer" "sidebar content"' : '"sidebar composer content"',
          navigation:true, progress:true, fits:true,
        });
        assert.equal(await visible('.elys-preview'), false);
        await screenshot('workspace-generation-' + width);
      }
    } finally {
      await evaluate('window.releaseLayoutGeneration(); void 0');
    }
    const directTool = await activeTool(previous);
    await cdp('Emulation.setDeviceMetricsOverride', {width:1360,height:980,deviceScaleFactor:1,mobile:false});
    await recordRun(directTool);
    assert.deepEqual(await evaluate('window.testGenerationPosts'), [custom]);
    assert.equal((await library()).items.length, before.length + 1);
    evidence.tests.push({ id: 'TEST-011', result: 'passed', sampleTemplates: ['board', 'list', 'budget'],
      zeroSampleGenerations: true, noSavedSampleTools: true, customizeWithoutRequest: true,
      templateGenerationRequests: 1, directGenerationRequests: 1 });

    for (const path of ['direct', 'table', 'assistant']) {
      stage = 'TEST-016-03/04 first saved tool ends guide from ' + path;
      await logout();
      await login('qa-guide-' + path + '-' + Date.now().toString(36), true);
      const narrow = path !== 'direct';
      await cdp('Emulation.setDeviceMetricsOverride', {width:narrow ? 390 : 1360,height:844,deviceScaleFactor:1,mobile:narrow});
      if (path === 'assistant') {
        await click('#assistant-entry');
        assert.equal(await evaluate('document.querySelector("#idea-entry").getAttribute("aria-current")'), 'page');
        await type('#assistant-idea-text', '做一个读书清单，可以新增书名并保存。');
        await click('#assistant-analyze button');
        await until(() => visible('#assistant-build'));
        await click('#assistant-save');
        await until(async () => (await evaluate('document.querySelector("#assistant-status")?.textContent || ""')).includes('已保存'));
        await click('#assistant-drafts');
        await until(() => evaluate('document.querySelector(".assistant-draft-list button")?.checkVisibility()'));
        await click('#idea-entry');
        assert.equal(await evaluate('document.querySelector("#idea-entry").getAttribute("aria-current")'), 'page');
        await click('#assistant-drafts');
        await until(() => evaluate('document.querySelector(".assistant-draft-list button")?.checkVisibility()'));
        assert.equal(await evaluate('document.querySelector("#idea-entry").getAttribute("aria-current")'), 'page');
        await click('.assistant-draft-list button');
        await until(() => visible('#assistant-build'));
        await screenshot('saved-draft-from-left-navigation');
        await click('#assistant-build');
        assert.equal(await evaluate('document.querySelector("#new-tool").getAttribute("aria-current")'), 'page');
        assert.equal(await evaluate('document.querySelector("#requirement").readOnly'), false);
        await click('#generate');
      } else {
        if (path === 'table') {
          await type('#requirement', '一段与导表无关的原需求');
          await evaluate(`(() => {
            const fetch = window.fetch;
            window.fetch = async (path, options) => {
              const response = await fetch(path, options);
              if (options?.method === 'POST' && String(path).endsWith('/commit')) {
                window.fetch = fetch;
                throw new TypeError('fixture: lost table commit response');
              }
              return response;
            };
          })()`);
          await click('#table-entry');
          await click('#table-example');
          await until(() => evaluate('document.querySelector("#table-confirm")?.checkVisibility() && !document.querySelector("#table-confirm").disabled'));
          await click('#table-confirm');
        } else {
          await generate('回归：强制新建失败');
          await until(async () => (await evaluate('document.querySelector("#error").textContent')).length > 0);
          assert.equal((await library()).items.length, 0);
          await generate(requirements[0]);
        }
      }
      await activeTool();
      assert.equal(await visible('#entry-coachmark'), false);
      assert.equal(await visible(path === 'table' ? '#first-tool-saved' : '#generation-saved'), true);
      if (path === 'table') assert.equal(await visible('#generation-saved'), false);
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
      await screenshot('first-saved-' + path);
    }
    evidence.tests.push({id: 'TEST-016-03/04-paths', result:'passed',
      freshAccounts: ['direct','table','assistant'], directFailureUncounted: true});

  }
  if (live && process.argv.includes('--analysis-live')) {
    const balance = async () => (await appRequest('/api/trial')).body;
    const initialBalance = await balance();
    assert.equal(initialBalance.analysisMode, 'deepseek');
    await click('#new-tool');
    await click('#idea-entry');
    const idea = '#assistant-idea-text';
    const draft = '#assistant-draft-editor';
    async function awaitAnalysis(...selectors) {
      return until(async () => {
        const error = await evaluate('document.querySelector("#assistant-status.elys-error")?.textContent');
        if (error) throw new Error(error);
        for (const selector of selectors) if (await visible(selector)) return selector;
      }, 35000);
    }
    async function analyzeIdea(value, result) {
      await type(idea, value);
      await click('#assistant-analyze button');
      await awaitAnalysis(result);
    }
    async function verifyBalance(personal, dispatches) {
      const current = await balance();
      assert.equal(current.analysis.used - initialBalance.analysis.used, personal);
      assert.equal(current.analysis.totalUsed - initialBalance.analysis.totalUsed, dispatches);
      assert.deepEqual(current.generation, initialBalance.generation);
    }
    stage = 'DeepSeek analysis of a clear idea reaches an editable draft';
    await analyzeIdea('做一个社区图书借阅清单。记录书名和借阅人，能新增借阅并标记已归还，刷新后保留记录。', draft);
    assert.match(await evaluate('document.querySelector("#assistant-draft-editor").value'), /借阅|归还/);
    await verifyBalance(1, 1);
    await screenshot('live-analysis-draft');
    await click('#assistant-new-round');
    stage = 'DeepSeek analysis of a vague idea charges only after delivering a draft';
    await analyzeIdea('每周都在群里追问项目进度，经常不知道哪里卡住了。', '#assistant-directions');
    const count = await evaluate('document.querySelectorAll("#assistant-directions [data-direction]").length');
    assert.ok(count >= 2 && count <= 3);
    await verifyBalance(1, 2);
    await screenshot('live-analysis-directions');
    await click('#assistant-directions [data-direction="0"]');
    const questions = '#assistant-questions select';
    let dispatches = 3;
    if (await awaitAnalysis(questions, draft) === questions) {
      await verifyBalance(1, 3);
      await evaluate(`for (const input of document.querySelectorAll('#assistant-questions select')) {
        input.selectedIndex = 1;
        input.dispatchEvent(new Event('change', {bubbles: true}));
      }`);
      await click('#assistant-organize');
      await awaitAnalysis(draft);
      dispatches += 1;
    }
    await verifyBalance(2, dispatches);
    await screenshot('live-vague-analysis-draft');
    const log = await readFile(join(resolve(process.env.WORKBENCH_RUNS_DIR || 'runs'), 'analysis-usage.jsonl'), 'utf8');
    const calls = log.trim().split('\n').map(JSON.parse);
    assert.equal(calls.length, dispatches);
    assert.ok(calls.every(call => call.status === 'ok' && call.request_id && call.usage?.total_tokens > 0));
    evidence.analysisCalls = calls;
    evidence.tests.push({id: 'TEST-012-05-live', result: 'passed', clearDraft: true, vagueDraft: true, directions: count,
      personalDrafts: 2, analysisRequests: dispatches, generationBalanceUnchanged: true});
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
  evidence.errorStack = error.stack;
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
