// TEST-014 · AC-014. Use an isolated workbench with the existing model fixture.
import assert from 'node:assert/strict';
import { createHash, X509Certificate } from 'node:crypto';
import { request as httpsRequest } from 'node:https';
import { createServer } from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const root = await mkdtemp(join(tmpdir(), 'root-site-'));
const useHttps = process.argv.includes('--https');
const children = [];
const sockets = [];

function launch(command, args, env = process.env) {
  const child = spawn(command, args, { env, stdio: ['ignore', 'pipe', 'inherit'] });
  children.push(child);
  return child;
}

async function startServer(args) {
  const child = launch('python3', ['-B', ...args]);
  const [line] = await Promise.race([once(child.stdout, 'data'), once(child, 'exit').then(() => [''])]);
  const url = String(line).match(/http:\/\/(?:127\.0\.0\.1|\[::1\]):\d+/)?.[0];
  assert.ok(url, '本机服务应输出启动地址');
  return url;
}

async function output(command, args) {
  const child = launch(command, args);
  let stdout = '';
  child.stdout.setEncoding('utf8').on('data', chunk => { stdout += chunk; });
  const [code] = await once(child, 'close');
  assert.equal(code, 0, command + ' should succeed');
  return stdout.trim();
}

async function localPort() {
  const server = createServer().listen(0, '127.0.0.1');
  await once(server, 'listening');
  const { port } = server.address();
  await new Promise(resolve => server.close(resolve));
  return port;
}

function tlsRequest(url, ca, options, body) {
  return new Promise((resolve, reject) => {
    const request = httpsRequest(url, { ca, timeout: 5000, ...options }, response => {
      let text = '';
      response.setEncoding('utf8').on('data', chunk => { text += chunk; });
      response.on('end', () => resolve({status: response.statusCode, headers: response.headers, text}));
      response.on('error', reject);
    });
    request.on('timeout', () => request.destroy(new Error('HTTPS request timed out: ' + new URL(url).pathname + ' (' + (options?.headers?.['Content-Length'] || 0) + ' bytes)')));
    request.on('error', reject);
    request.end(body);
  });
}

async function until(check) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    await delay(100);
  }
  throw new Error('等待浏览器状态超时');
}

async function connect(page) {
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  sockets.push(socket);
  await once(socket, 'open');
  let id = 0;
  const pending = new Map();
  const documents = new Map();
  const requestCookies = new Map();
  const responses = new Map();
  const frames = new Map();
  socket.onmessage = ({ data }) => {
    const reply = JSON.parse(data);
    const { method, params } = reply;
    if (method === 'Target.attachedToTarget' && params.targetInfo.type === 'iframe') {
      frames.set(params.targetInfo.targetId, params.sessionId);
    }
    if (method === 'Target.detachedFromTarget') frames.delete(params.targetId);
    if (method === 'Network.requestWillBeSent' && params.type === 'Document') {
      documents.set(params.requestId, params.request.url);
    }
    if (method === 'Network.requestWillBeSentExtraInfo') {
      const cookie = Object.entries(params.headers).find(([name]) => name.toLowerCase() === 'cookie')?.[1] ?? '';
      requestCookies.set(params.requestId, /(?:^|;\s*)root_session=/.test(cookie));
    }
    if (method === 'Network.responseReceived' && params.type === 'Document') {
      responses.set(params.requestId, params.response.status);
    }
    pending.get(reply.id)?.(reply);
    pending.delete(reply.id);
  };
  const cdp = async (method, params = {}, sessionId) => {
    const request = ++id;
    const reply = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(request);
        reject(new Error(method + ' timed out'));
      }, 10000);
      pending.set(request, response => {
        clearTimeout(timer);
        resolve(response);
      });
      socket.send(JSON.stringify({ id: request, method, params, sessionId }));
    });
    assert.ok(!reply.error && !reply.result?.exceptionDetails, JSON.stringify(reply.error || reply.result?.exceptionDetails));
    return reply.result;
  };
  const evaluate = async (expression, context = {}) => (await cdp('Runtime.evaluate', {
    expression, contextId: context.id, awaitPromise: true, returnByValue: true, userGesture: true,
  }, context.session)).result.value;
  return {
    cdp,
    evaluate,
    async activeTool(previousTool) {
      const selector = '#preview iframe[data-tool]';
      return until(async () => {
        const state = await evaluate(`({
          id: document.querySelector(${JSON.stringify(selector)})?.dataset.tool,
          error: document.querySelector('#error')?.textContent,
        })`);
        if (state.error) throw new Error(state.error);
        if (!state.id || state.id === previousTool) return null;
        const {root} = await cdp('DOM.getDocument');
        const {nodeId} = await cdp('DOM.querySelector', {nodeId: root.nodeId, selector});
        if (!nodeId) return null;
        const {node} = await cdp('DOM.describeNode', {nodeId});
        const session = frames.get(node.frameId);
        const {frameTree} = await cdp('Page.getFrameTree', {}, session);
        const frame = session ? frameTree.frame : frameTree.childFrames?.find(item => item.frame.id === node.frameId)?.frame;
        if (frame?.url !== 'about:srcdoc') return null;
        const {executionContextId: id} = await cdp('Page.createIsolatedWorld', {
          frameId: frame.id, worldName: 'root-site-preflight',
        }, session);
        return {tool: state.id, loader: frame.loaderId, context: {id, session}};
      });
    },
    async reloadCookieCheck(url) {
      documents.clear();
      requestCookies.clear();
      responses.clear();
      await cdp('Page.reload', { ignoreCache: true });
      const requestId = await until(() => [...documents].find(([key, address]) =>
        address === url && requestCookies.has(key) && responses.has(key))?.[0]);
      assert.equal(responses.get(requestId), 200);
      return requestCookies.get(requestId);
    },
  };
}

try {
  const runs = join(root, 'runs');
  const upstream = await startServer(['tests/fixtures.py', 'fixture-key', '0', runs, 'http://127.0.0.1:1']);
  const edgePort = useHttps ? await localPort() : null;
  const workbench = useHttps ? 'https://127.0.0.1:' + edgePort : upstream;
  const siteServer = process.argv.includes('--baseline') ? upstream
    : await startServer(['public_site.py', '--port', '0', '--workbench-url', workbench + '/']);
  const site = useHttps ? 'https://localhost:' + edgePort : siteServer;
  const invite = await output('python3', ['-B', 'workbench.py', '--runs-dir', runs, '--create-invite', '--invite-uses', '1']);
  let ca;
  let spki;
  if (useHttps) {
    const caddy = process.env.ROOT_TEST_CADDY;
    assert.ok(caddy, '需要 ROOT_TEST_CADDY 指向本机 Caddy');
    const certificate = join(root, 'localhost.pem');
    const key = join(root, 'localhost.key');
    await output('openssl', ['req', '-x509', '-newkey', 'rsa:2048',
      '-nodes', '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
      '-keyout', key, '-out', certificate]);
    ca = await readFile(certificate);
    spki = createHash('sha256').update(new X509Certificate(ca).publicKey.export({type: 'spki', format: 'der'})).digest('base64');
    const proxy = launch(caddy, ['run', '--config', 'deploy-local/Caddyfile', '--adapter', 'caddyfile'], {
      ...process.env, XDG_DATA_HOME: join(root, 'caddy-data'), XDG_CONFIG_HOME: join(root, 'caddy-config'),
      ROOT_LOCAL_PORT: String(edgePort), ROOT_LOCAL_SITE_PORT: new URL(siteServer).port,
      ROOT_LOCAL_WORKBENCH_PORT: new URL(upstream).port, ROOT_LOCAL_CERT: certificate, ROOT_LOCAL_CERT_KEY: key,
      ROOT_LOCAL_INSPECT_LIMIT: '6356992',
    });
    await until(async () => {
      assert.equal(proxy.exitCode, null, '本机 Caddy 应保持运行');
      return (await tlsRequest(site, ca).catch(() => null))?.status === 200;
    });
    const shell = await tlsRequest(workbench, ca);
    assert.equal(shell.status, 200);
    assert.ok(shell.headers['connection-allowlist'].includes('"' + workbench + '/api/table/inspect"'));
    assert.ok(shell.headers['connection-allowlist'].endsWith('; webrtc=block'));
    assert.ok(!shell.headers['connection-allowlist'].includes(upstream));
    const forged = await tlsRequest(workbench, ca, {headers: {Host: 'localhost:' + (edgePort + 1)}});
    assert.equal(forged.status, 421, 'Host 错误端口不得命中候选');
    const rejected = async (origin) => {
      const body = '{}';
      const headers = ['Host', new URL(workbench).host, 'Content-Type', 'application/json', 'Content-Length', String(body.length)];
      for (const value of origin) headers.push('Origin', value);
      return tlsRequest(workbench + '/api/register', ca, {method: 'POST', headers}, body);
    };
    for (const origins of [[], ['null'], [site], [upstream], [workbench, site]]) {
      assert.equal((await rejected(origins)).status, 403, '非唯一的 HTTPS 工作台 Origin 必须被拒 ' + JSON.stringify(origins));
    }
  }

  async function get(url) {
    if (useHttps) return tlsRequest(url, ca);
    const response = await fetch(url);
    return {status: response.status, headers: Object.fromEntries(response.headers), text: await response.text()};
  }
  const response = await get(site);
  assert.equal(response.status, 200);
  assert.match(response.text, /<title>ROOT 官网<\/title>/, '打开官网地址应直接看到 ROOT 官网');
  assert.equal((await get(site + '/api/session')).status, 404, '官网不提供工作台接口');
  assert.equal((await get(site + '/.git/config')).status, 404, '官网不公开项目文件');
  assert.match(response.headers['content-security-policy'], /default-src 'none'/);
  assert.equal(response.headers['set-cookie'], undefined);

  const profile = join(root, 'chrome');
  launch(process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
    '--headless=new', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0',
    ...(spki ? ['--ignore-certificate-errors-spki-list=' + spki, '--host-resolver-rules=MAP localhost 127.0.0.1'] : []),
    '--user-data-dir=' + profile, site + '/',
  ]);
  const port = await until(async () => (await readFile(join(profile, 'DevToolsActivePort'), 'utf8').catch(() => '')).split('\n')[0]);
  const tabs = async () => (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  const page = await until(async () => (await tabs()).find(tab => tab.url === site + '/'));
  const browser = await connect(page);
  const { cdp, evaluate } = browser;
  await cdp('Network.enable');
  await until(() => evaluate('document.readyState === "complete"'));

  for (const width of [1920, 1440, 1024, 1023, 761, 760, 390, 320]) {
    await cdp('Emulation.setDeviceMetricsOverride', { width, height: 850, deviceScaleFactor: 1, mobile: false });
    const layout = await evaluate(`(() => {
      window.scrollTo({top: 0, behavior: 'instant'});
      const entry = document.querySelector('header .header-entry');
      const box = entry.getBoundingClientRect();
      return [entry.href, document.querySelector('.workspace-entry a').href,
        box.top >= 0 && box.bottom < 120,
        document.documentElement.scrollWidth === document.documentElement.clientWidth,
        ['需求助手', '直接开发', '我的工具'].every(name => document.body.textContent.includes(name))];
    })()`);
    assert.deepEqual(layout, [workbench + '/', workbench + '/', true, true, true]);
    const palette = await evaluate(`(() => {
      const style = selector => {
        const element = document.querySelector(selector);
        return element ? getComputedStyle(element) : {};
      };
      return [
        style('.intro').backgroundColor,
        style('.hero .primary').backgroundColor,
        style('.hero .link-button:not(.primary)').backgroundColor,
        style('#root-site-example').backgroundColor,
        style('.chapter-bar').backgroundColor,
        style('.result h3').color,
        style('[data-filter]').color,
        style('#root-site-assistant').backgroundColor,
        style('.workspace-entry').backgroundColor,
        style('.site-footer').backgroundColor,
      ];
    })()`);
    assert.deepEqual(palette, [
      'rgb(35, 58, 118)', 'rgb(246, 248, 255)', 'rgba(0, 0, 0, 0)',
      'rgb(234, 241, 252)', 'rgb(234, 241, 252)', 'rgb(35, 58, 118)', 'rgb(35, 58, 118)',
      'rgb(247, 249, 252)', 'rgb(255, 255, 255)', 'rgb(255, 255, 255)',
    ], `${width} px 的官网应使用已确认的 B 修订版配色`);
    const geometry = await evaluate(`(() => {
      const bounds = selector => document.querySelector(selector).getBoundingClientRect();
      const bands = [...document.querySelectorAll('.page, .intro, .chapter, .site-footer')];
      const content = ['.intro > header', '.hero > div', '.pipeline', '#root-site-about > div',
        '.site-footer > .brand'].map(selector => bounds(selector).left);
      const columns = selector => getComputedStyle(document.querySelector(selector))
        .gridTemplateColumns.split(' ').map(parseFloat).filter(Number.isFinite);
      return {
        fullWidth: bands.every(band => {
          const {left, right} = band.getBoundingClientRect();
          return Math.abs(left) < 1 && Math.abs(right - document.documentElement.clientWidth) < 1;
        }),
        inset: Math.round(content[0]),
        aligned: content.every(left => Math.abs(left - content[0]) < 1),
        hero: columns('.hero'),
        trial: columns('.pipeline'),
        about: columns('#root-site-about'),
        trialLabel: document.querySelector('.hero .primary').textContent.trim(),
        trialAnswer: document.querySelector('.faq details p').textContent,
      };
    })()`);
    assert.ok(geometry.fullWidth, `${width} px 的背景和章节分隔应铺满视口`);
    assert.ok(geometry.aligned, `${width} px 的导航、首屏、试玩、关于和页脚应对齐`);
    const expectedInset = {1920: 128, 1440: 102, 390: 23}[width];
    if (expectedInset) assert.ok(Math.abs(geometry.inset - expectedInset) <= 1);
    const columns = width <= 760 ? 1 : 2;
    assert.equal(geometry.hero.length, columns);
    assert.equal(geometry.trial.length, columns);
    assert.equal(geometry.about.length, columns);
    if (columns === 2) {
      const [left, right] = geometry.hero;
      const share = width >= 1024 ? 0.45 : 0.60;
      assert.ok(Math.abs(left / (left + right) - share) < 0.01, `${width} px 首屏应采用对应比例`);
    }
    assert.equal(geometry.trialLabel, '试用示例');
    assert.match(geometry.trialAnswer, /点击「试用示例」即可在本页操作交付跟进板，无需登录/);
    const announcement = await evaluate(`(() => {
      const notice = document.querySelector('#root-site-roadmap');
      if (!notice?.checkVisibility()) return null;
      const bounds = notice.getBoundingClientRect();
      return { text: notice.textContent, fits: bounds.left >= 0 && bounds.right <= innerWidth };
    })()`);
    assert.ok(announcement, `开发计划公告在 ${width} px 应直接可见`);
    assert.ok(announcement.fits, `开发计划公告在 ${width} px 应完整适配`);
    assert.match(announcement.text, /Demo[\s\S]*单页[\s\S]*多页[\s\S]*后续[\s\S]*预计 11 月底更新进度/);

    const invitation = await evaluate(`(() => {
      const question = [...document.querySelectorAll('.faq summary')]
        .find(summary => summary.textContent.trim() === '邀请码从哪里获取？');
      if (!question) return null;
      const answer = question.parentElement;
      question.click();
      const visible = answer.open && answer.querySelector('p').checkVisibility();
      const text = answer.querySelector('p').textContent;
      question.click();
      return {visible, text};
    })()`);
    assert.ok(invitation?.visible, '官网应能展开查看邀请码来源和注册指引');
    assert.match(invitation.text, /向邀请你体验 ROOT 的项目方获取邀请码/);
    assert.match(invitation.text, /凭邀请码注册/);
    assert.match(invitation.text, /新码自生成起 72 小时有效.*此前发放的邀请码以原到期时间为准/);

    const capabilities = await evaluate(`(() => {
      const faq = document.querySelector('#root-site-faq');
      document.querySelector('header a[href="#root-site-about"]').click();
      const summaries = [...faq.querySelectorAll('summary')];
      const questions = ['ROOT 适合做哪些工具？', '工具里的数据会保存吗？', '首版有哪些限制？',
        '可以把生成的工具公开发布吗？'];
      return questions.map(label => {
        const summary = summaries.find(item => item.textContent.trim() === label);
        if (!summary) return null;
        summary.click();
        const answer = summary.parentElement;
        const paragraphs = [...answer.querySelectorAll('p')];
        const visible = answer.open && paragraphs.length > 0 && paragraphs.every(paragraph => {
          const rect = paragraph.getBoundingClientRect();
          return paragraph.checkVisibility() && rect.left >= 0 && rect.right <= innerWidth
            && paragraph.scrollWidth <= paragraph.clientWidth;
        });
        const text = paragraphs.map(paragraph => paragraph.textContent.trim()).join(' ');
        summary.click();
        return {visible, text};
      });
    })()`);
    assert.ok(capabilities.every(answer => answer?.visible), `${width} px 应能展开并完整阅读工具能力、保存、限制与发布问答`);
    const [tools, storage, limits, publishing] = capabilities.map(answer => answer.text);
    assert.match(tools, /^把你的文字需求做成可操作的网页工具。.*登记和筛选记录.*跟踪进度.*按自己的规则做测算/);
    assert.match(tools, /带表头的 CSV 或 XLSX.*确认字段.*第一个工作表/);
    assert.match(storage, /登录工作台.*保存到你的账号.*我的工具.*官网试用.*刷新后会恢复原样/);
    assert.match(limits, /暂不支持通用数据库、附件存储和多人共享编辑/);
    assert.match(limits, /暂不支持音视频播放、录音和语音功能.*摄像头、麦克风、蓝牙或 USB 设备/);
    assert.match(limits, /暂不支持同步原表、连接外部系统和定时后台任务/);
    assert.match(publishing, /工作台内.*公开发布暂未开放/);
  }

  const sample = await evaluate(`(() => {
    const q = selector => document.querySelector(selector);
    const source = [...document.querySelectorAll('.sample-table tbody tr')].map(row => row.cells[0].textContent);
    const output = [...document.querySelectorAll('[data-item] label')].map(label => label.textContent);
    q('.hero .primary').click();
    const focused = document.activeElement === q('[data-filter]');
    q('[data-filter]').click();
    const filtered = document.querySelectorAll('[data-item]:not([hidden])').length;
    q('[data-item] input').click();
    const remaining = q('[data-count]').textContent;
    q('header a[href="#root-site-about"]').click();
    q('.faq summary').click();
    return { source, output, focused, filtered, remaining, faqOpened: q('.faq details').open,
      storage: localStorage.length, resources: performance.getEntriesByType('resource').map(entry => entry.name) };
  })()`);
  assert.equal(sample.source.length, 3);
  assert.deepEqual(sample.output, sample.source);
  assert.deepEqual([sample.focused, sample.filtered, sample.remaining, sample.faqOpened, sample.storage],
    [true, 2, '还有 1 项未完成', true, 0]);
  assert.ok(sample.resources.every(url => url === site + '/favicon.ico'), '试用不得请求模型或账号接口');
  for (const width of [390, 760, 761, 1023, 1024, 1440, 1920]) {
    await cdp('Emulation.setDeviceMetricsOverride', { width, height: 850, deviceScaleFactor: 1, mobile: false });
    const state = await evaluate(`[
      document.querySelector('[data-count]').textContent,
      document.querySelectorAll('[data-item]:not([hidden])').length,
      document.querySelectorAll('[data-item] input:checked').length,
      document.querySelector('[data-filter]').getAttribute('aria-pressed'),
      document.querySelector('.faq details').open
    ]`);
    assert.deepEqual(state, ['还有 1 项未完成', 1, 2, 'true', true], `${width} px 重排应保留试玩和问答状态`);
  }
  assert.equal(await browser.reloadCookieCheck(site + '/'), false, '未登录的官网对照请求不带工作台会话');
  await until(() => evaluate('document.querySelector("[data-count]")?.textContent === "还有 2 项未完成"'));

  await evaluate('document.querySelector("header .header-entry").click()');
  const first = await until(async () => (await tabs()).find(tab => tab.url === workbench + '/'));
  const account = await connect(first);
  await until(() => account.evaluate('document.documentElement?.dataset.ready === "true" && document.querySelector("#auth")?.hidden === false'));
  assert.match(await account.evaluate('document.querySelector("#auth-mode").textContent'), /凭邀请码注册/);
  await account.evaluate('document.querySelector("#auth-mode").click()');
  assert.equal(await account.evaluate('document.querySelector("#invite").checkVisibility()'), true);
  await account.evaluate(`(() => {
    document.querySelector('#invite').value = ${JSON.stringify(invite)};
    document.querySelector('#username').value = 'site-check';
    document.querySelector('#password').value = 'root-browser-test-only!';
    document.querySelector('#auth-form').requestSubmit();
  })()`);
  await until(() => account.evaluate('document.querySelector("#workspace")?.hidden === false'));
  assert.equal(await account.evaluate("fetch('/api/session').then(response => response.json()).then(session => session.user)"),
    'site-check', '同一浏览器已有可用的工作台会话');
  let savedTableId;
  if (useHttps) {
    const { cookies } = await account.cdp('Network.getCookies', { urls: [workbench + '/', site + '/'] });
    const session = cookies.find(cookie => cookie.name === 'root_session');
    assert.ok(session?.secure && session.httpOnly && session.sameSite === 'Strict');
    assert.equal(session.domain, '127.0.0.1');
    const repeat = await account.evaluate(`fetch('/api/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({username:'second-check',password:'root-browser-test-only!',invite:${JSON.stringify(invite)}}),
    }).then(response => response.json()).then(error => error.code)`);
    assert.equal(repeat, 'invalid_invite', '专用邀请码只允许成功注册一次');

    const marker = await account.evaluate("fetch('/api/session').then(response => response.json()).then(session => session.marker)");
    const post = (path, payload) => {
      const body = JSON.stringify(payload);
      return tlsRequest(workbench + path, ca, {method: 'POST', headers: {
        'Origin': workbench, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body),
        'Cookie': 'root_session=' + session.value, 'X-Root-Session': marker,
      }}, body);
    };
    const csv = '记录\n' + ('\u0001'.repeat(2095) + '\n').repeat(500);
    assert.ok(Buffer.byteLength(csv) < 1024 * 1024);
    const input = {source: 'upload', filename: 'edge.csv', csv};
    assert.ok(Buffer.byteLength(JSON.stringify(input)) > 2 * 1024 * 1024, '代理回归必须超过旧上限');
    const checked = await post('/api/table/inspect', input);
    assert.equal(checked.status, 200);
    const table = JSON.parse(checked.text);
    assert.equal(table.rows.length, 500);
    const draft = await post('/api/table/generate', {table});
    assert.equal(draft.status, 200);
    const candidate = JSON.parse(draft.text);
    savedTableId = candidate.id;
    const saved = await post('/api/tools/' + candidate.id + '/commit', {entries: candidate.entries});
    assert.equal(saved.status, 200);
    const readBack = await tlsRequest(workbench + '/api/tools/' + candidate.id, ca, {headers: {
      'Cookie': 'root_session=' + session.value, 'X-Root-Session': marker,
    }});
    assert.equal(readBack.status, 200);
    assert.equal(JSON.parse(JSON.parse(readBack.text).entries[0][1]).rows.at(-1)[0].length, 2095);
    for (const [path, cap] of [['/api/table/inspect', 6356992], ['/api/table/generate', 16777216], ['/api/register', 16000]]) {
      const oversized = await tlsRequest(workbench + path, ca, {
        method: 'POST', headers: {'Origin': workbench, 'Content-Type': 'application/json', 'Content-Length': cap + 1},
      }, Buffer.alloc(cap + 1, ' '));
      assert.equal(oversized.status, 413, '代理应拒绝超过路径限制的请求 ' + path);
    }
  }
  assert.equal(await browser.reloadCookieCheck(site + '/'), false, '登录工作台后刷新官网不得发送 root_session');
  await until(() => evaluate('document.readyState === "complete"'));
  await evaluate('document.querySelector("header .header-entry").click()');
  const returning = await until(async () => (await tabs()).find(tab => tab.url === workbench + '/' && tab.id !== first.id));
  const returnPage = await connect(returning);
  await until(() => returnPage.evaluate('document.querySelector("#account-name")?.textContent === "site-check" && document.querySelector("#workspace")?.hidden === false'));
  if (useHttps) {
    await returnPage.cdp('Page.enable');
    await returnPage.cdp('Target.setAutoAttach', {autoAttach: true, waitForDebuggerOnStart: false, flatten: true});
    await until(() => returnPage.evaluate(`document.documentElement.dataset.ready === 'true'
      && document.querySelector('#preview iframe[data-tool]')?.dataset.tool === ${JSON.stringify(savedTableId)}`));
    const canGenerate = await returnPage.evaluate(`(() => {
      document.querySelector('#guide-skip').click();
      document.querySelector('#new-tool').click();
      const input = document.querySelector('#requirement');
      input.value = '生成一个简单的读书清单';
      input.dispatchEvent(new Event('input'));
      const button = document.querySelector('#generate');
      if (button.disabled) return false;
      button.click();
      return true;
    })()`);
    assert.equal(canGenerate, true, '恢复表格工具后可以从同一工作台生成');
    const active = await returnPage.activeTool(savedTableId);
    const record = '从官网进入后新增的一本书';
    await returnPage.evaluate('document.querySelector("#book").focus()', active.context);
    await returnPage.cdp('Input.insertText', {text: record});
    await returnPage.evaluate('document.querySelector("form button").click()', active.context);
    assert.match(await returnPage.evaluate('document.querySelector("#list").textContent', active.context), new RegExp(record));
    await until(() => returnPage.evaluate(`(async () => {
      const session = await fetch('/api/session').then(response => response.json());
      const saved = await fetch('/api/tools/' + ${JSON.stringify(active.tool)}, {
        headers: {'X-Root-Session': session.marker},
      }).then(response => response.json());
      return JSON.stringify(saved.entries).includes(${JSON.stringify(record)});
    })()`));
    await returnPage.cdp('Page.reload');
    await until(() => returnPage.evaluate('document.documentElement?.dataset.ready === "true"'));
    const restored = await returnPage.activeTool();
    assert.equal(restored.tool, active.tool);
    assert.notEqual(restored.loader, active.loader);
    assert.match(await returnPage.evaluate('document.querySelector("#list").textContent', restored.context), new RegExp(record));
    console.log('TEST-003 local HTTPS passed · one-off invitation · proxy boundaries and large CSV save · browser generated, operated and restored one tool');
  }
  console.log('TEST-014 passed · full-width layout and FAQ at 1920/1440/1024/1023/761/760/390/320 px · trial state across resize · anonymous and returning entry · workbench session absent from both site document requests');
} finally {
  for (const socket of sockets) socket.close();
  await Promise.all(children.map(async child => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = once(child, 'exit');
    child.kill();
    await exited;
  }));
  await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
