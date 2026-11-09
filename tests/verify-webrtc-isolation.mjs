// TEST-002 / ROOT-V4-IA-01. A loopback UDP control distinguishes isolation from a broken probe.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createSocket } from 'node:dgram';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';



// Run from the frozen candidate directory. All endpoints bind only to loopback.
const artifacts = resolve(process.argv[2]);
const withoutAllowlist = process.argv.includes('--without-allowlist');
const withNested = process.argv.includes('--nested');
let controlServer;
const probes = [];
async function listen(label) {
  const socket = createSocket('udp4');
  probes.push(socket);
  const packets = [];
  socket.on('message', (message, remote) => {
    const stun = message.length >= 20 && message.readUInt32BE(4) === 0x2112a442;
    packets.push(stun
      ? { type: 'STUN', messageType: message.readUInt16BE(0), length: message.length,
          source: remote.address, transaction: message.subarray(8, 20).toString('hex') }
      : { type: 'UDP', length: message.length, source: remote.address });
  });
  socket.bind(0, '127.0.0.1');
  await once(socket,'listening');
  return { label, port:socket.address().port, packets };
}
function code(port, relay = '') {
  return `
    (async () => {
      const pc = new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:${port}'}]});
      pc.createDataChannel('acceptance-probe');
      await pc.setLocalDescription(await pc.createOffer());
      document.body.dataset.offer = 'created';
      setTimeout(() => pc.close(), 2700);
    })().catch(err => { document.body.dataset.offer = err.name + ': ' + err.message; })
      .finally(() => { ${relay} });
  `;
}
const root = await mkdtemp(join(tmpdir(), 'root-workbench-'));
const profile = join(root, 'chrome');
const evidence = { mode: 'fixture', observations: {} };
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


try {
  const control = await listen('browser top-level');
  const preview = await listen('sandbox preview');
  const nested = withNested ? await listen('nested srcdoc') : null;
  evidence.observations.probes = [control, preview];
  if (nested) evidence.observations.probes.push(nested);
  const requirement = '本机网络隔离验收探针（无真实模型调用）';
  const controlMarkup = '<!doctype html><body><h1>Local control</h1><script>' + code(control.port) + '</script>';
  const nestedScript = nested ? code(nested.port, "parent.postMessage({nestedOffer: document.body.dataset.offer}, '*')") : '';
  const nestedHtml = JSON.stringify('<!doctype html><body><script>' + nestedScript + '</script>').replaceAll('<', '\\u003c');
  const fixtureMarkup = '<!doctype html><html><body><h1>Local sandbox probe</h1><script>' + `
    const probeDocument = Math.random().toString(36);
    function reportProbe() {
      parent.postMessage({
        kind: 'webrtc-probe-observation', probeDocument,
        origin: self.origin,
        csp: document.querySelector('meta[http-equiv="Content-Security-Policy"]').content,
        offer: document.body.dataset.offer,
        nestedOffer: document.body.dataset.nestedOffer ?? null,
      }, '*');
    }
    ${nested ? `
      const child = document.createElement('iframe');
      window.addEventListener('message', event => {
        if (event.source === child.contentWindow && typeof event.data?.nestedOffer === 'string') {
          document.body.dataset.nestedOffer = event.data.nestedOffer;
          reportProbe();
        }
      });
      child.srcdoc = ${nestedHtml};
      document.body.append(child);
    ` : ''}
  ` + code(preview.port, 'reportProbe()') + '</script></body></html>';
  controlServer = createServer((req,res) => {
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});
    res.end(controlMarkup);
  });
  controlServer.listen(0,'127.0.0.1');
  await once(controlServer,'listening');
  const modelFixture = `
import generate, workbench, json, sys
from pathlib import Path
from accounts import Store
html = sys.argv[1]
runs = Path(sys.argv[2])
store = Store(runs / 'workspace.sqlite3')
store.create_invite('fixture-webrtc-invite')
store.register({'username':'webrtc-test','password':'fixture-webrtc-password!','invite':'fixture-webrtc-invite'})
if sys.argv[3] == 'true':
    class WithoutAllowlist(workbench.BaseHTTPRequestHandler):
        def send_header(self, name, value):
            if name != 'Connection-Allowlist':
                super().send_header(name, value)
    workbench.BaseHTTPRequestHandler = WithoutAllowlist
    saved = runs / 'run-existing'
    saved.mkdir(parents=True)
    (saved / 'app.html').write_text(html, encoding='utf-8')
    seed = store.login({'username':'webrtc-test','password':'fixture-webrtc-password!'}, '')
    with store.connection() as db:
        marker = store.session(db, seed)['marker']
    store.draft(seed, marker, 'run-existing', 'saved preview')
    store.change_tool(seed, marker, 'run-existing', 'commit', {'entries':[]})
    store.logout(seed, marker)
generate.request_completion = lambda *a: (200, json.dumps({
    'id':'fixture-webrtc-isolation','model':generate.MODEL,
    'choices':[{'message':{'content':html},'finish_reason':'stop'}]
}).encode())
workbench.serve('fixture-key', 0, runs)
`;
  const server = launch('python3',['-B','-c',modelFixture,fixtureMarkup,join(root,'runs'),String(withoutAllowlist)]);
  const [line] = await Promise.race([once(server.stdout,'data'),once(server,'exit').then(()=>[''])]);
  const base = String(line).match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
  assert.ok(base);
  evidence.base = base;
  const cfg = await (await fetch(base+'/api/config')).json();
  assert.match(cfg.build, /^v4-[0-9a-f]{12}$/);
  evidence.build = cfg.build;
  const shell = await fetch(base);
  evidence.observations.connectionAllowlist = shell.headers.get('Connection-Allowlist');
  assert.equal(Boolean(evidence.observations.connectionAllowlist), !withoutAllowlist);
  const canary = await fetch(base + '/api/egress-check');
  assert.equal(canary.status, 200);
  const login = await fetch(base + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Origin: base },
    body: JSON.stringify({ username: 'webrtc-test', password: 'fixture-webrtc-password!' }),
  });
  assert.equal(login.status, 200);
  const cookie = login.headers.get('Set-Cookie').match(/^root_session=([^;]+)/)[1];
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
  function cdp(method, params = {}) {
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
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  socket.addEventListener('message', event => {
    const reply = JSON.parse(event.data);
    if (reply.method === 'Runtime.exceptionThrown') runtimeErrors.push(reply.params.exceptionDetails.exception?.description);
    const callback = pending.get(reply.id);
    pending.delete(reply.id);
    callback?.(reply);
  });

  async function evaluate(expression) {
    const result = await cdp('Runtime.evaluate', { expression, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.result.description || result.exceptionDetails.text);
    return result.result.value;
  }

  readPage = () => evaluate(`({
    phase: document.querySelector('#phase')?.textContent,
    status: document.querySelector('#preview-status')?.textContent,
    error: document.querySelector('#error')?.textContent,
    tool: document.querySelector('#preview iframe[data-tool]')?.dataset.tool,
    disabled: document.querySelector('#generate')?.disabled,
    config: document.querySelector('#config')?.textContent,
    storageWarning: document.querySelector('#storage-warning')?.textContent,
    savedTool: session?.latest,
    observation: window.previewProbe ?? null,
  })`);

  async function activeTool() {
    return until(() => evaluate(`(() => {
      const frame = document.querySelector('#preview iframe[data-tool]');
      const probe = window.previewProbe;
      if (frame && probe?.offer) return { id: frame.dataset.tool, sandbox: frame.getAttribute('sandbox'), ...probe };
    })()`));
  }

  async function type(selector, value) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    await cdp('Input.insertText', { text: value });
  }

  await cdp('Page.enable');
  await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `
    if (self === top && location.origin === ${JSON.stringify(base)}) {
      window.addEventListener('message', event => {
        const frame = document.querySelector('#preview iframe');
        if (event.origin === 'null' && event.source === frame?.contentWindow &&
            event.data?.kind === 'webrtc-probe-observation') {
          window.previewProbe = event.data;
        }
      });
    }
  ` });
  evidence.browser = (await cdp('Browser.getVersion')).product;
  stage = 'prove probe reachable with same browser';
  await cdp('Page.navigate',{url:'http://127.0.0.1:'+controlServer.address().port+'/'});
  await until(()=>evaluate('document.body?.dataset.offer === "created"'));
  await until(() => control.packets.some(packet => packet.type === 'STUN' && packet.messageType === 1), 3500);
  assert.equal(preview.packets.length,0);
  if (nested) assert.equal(nested.packets.length, 0);
  stage = 'submit controlled output through formal workbench';
  await cdp('Network.setCookie', { name: 'root_session', value: cookie, url: base,
    httpOnly: true, sameSite: 'Strict' });
  await cdp('Page.navigate',{url:base});
  await until(()=>evaluate('document.documentElement.dataset.ready === "true"'));
  await type('#requirement',requirement);
  await evaluate('document.querySelector("#generate").click()');
  if (withoutAllowlist) {
    stage = 'unsupported browser fails closed for generation and restoration';
    evidence.observations.unsupported = await evaluate(`({
      generateDisabled: document.querySelector('#generate').disabled,
      frameCount: document.querySelectorAll('#preview iframe').length,
      savedTool: session?.latest,
      warning: document.querySelector('#config').textContent,
    })`);
    assert.equal(evidence.observations.unsupported.generateDisabled, true);
    assert.equal(evidence.observations.unsupported.frameCount, 0);
    assert.equal(evidence.observations.unsupported.savedTool, 'run-existing');
    assert.match(evidence.observations.unsupported.warning, /网络隔离/);
  } else {
    const tool = await activeTool();
    evidence.observations.tool = tool.id;
    evidence.observations.sandbox = tool.sandbox;
    evidence.observations.policy = tool.csp;
    evidence.observations.previewOrigin = tool.origin;
    assert.equal(evidence.observations.previewOrigin, 'null');
    stage = 'check sandbox STUN egress';
    await delay(3000);
    evidence.observations.offer = tool.offer;
    evidence.observations.nestedOffer = await evaluate('window.previewProbe.nestedOffer');
    assert.ok(evidence.observations.offer, '生成预览已执行 WebRTC 探针');
    assert.equal(preview.packets.length, 0, '隔离预览不应发送 STUN 数据包');
    if (nested) {
      assert.ok(evidence.observations.nestedOffer, '嵌套预览已执行 WebRTC 探针');
      assert.equal(nested.packets.length, 0, '嵌套预览不应发送 STUN 数据包');
    }
    stage = 'check restored sandbox STUN egress';
    await cdp('Page.reload', { ignoreCache: true });
    await until(() => evaluate('performance.getEntriesByType("navigation")[0]?.type === "reload"'));
    await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
    const state = await readPage();
    if (nested && !state.tool) {
      assert.match(state.storageWarning, /准备预览超时/);
      assert.equal(state.savedTool, tool.id);
      evidence.observations.restored = { tool: tool.id, refused: state.storageWarning };
    } else {
      const restored = await activeTool();
      assert.equal(restored.id, tool.id);
      assert.notEqual(restored.probeDocument, tool.probeDocument);
      await delay(3000);
      const nestedOffer = await evaluate('window.previewProbe.nestedOffer');
      evidence.observations.restored = { tool: restored.id, newDocument: true, offer: restored.offer, nestedOffer };
    }
  }
  assert.equal(preview.packets.length,0,'隔离预览不应向工作台以外的探针发送 STUN 数据包');
  if (nested) assert.equal(nested.packets.length, 0, '嵌套预览不应发送 STUN 数据包');
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
    await mkdir(artifacts, { recursive: true, mode: 0o700 });
    await writeFile(join(artifacts, 'webrtc-isolation.json'), JSON.stringify(evidence, null, 2) + '\n')
      .catch(error => { console.error(error.message); process.exitCode = 1; });
  }
  console.log(JSON.stringify(evidence, null, 2));
  socket?.close();
  controlServer?.closeAllConnections();
  controlServer?.close();
  for (const udp of probes) udp.close();
  await Promise.all(children.map(async child => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = once(child, 'exit');
    child.kill();
    await exited;
  }));
  await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
