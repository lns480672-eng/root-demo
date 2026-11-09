import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const run = resolve(process.argv[2]);
const sample = '检查三号舱营养液液位';
const steps = [];
const denied = [];
const servers = [];
const profile = await mkdtemp(join(tmpdir(), 'root-preview-'));
const evidence = { sample, steps, blockedRequests: denied };
let chrome;
let socket;

async function until(check) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await delay(80);
  }
  throw new Error('Timed out waiting for the preview');
}

async function serve(path, body, policy) {
  const server = createServer((req, res) => {
    if (req.method !== 'GET' || req.url !== path) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Security-Policy': policy,
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
    }).end(body);
  });
  servers.push(server);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return 'http://127.0.0.1:' + server.address().port + path;
}

try {
  const html = await readFile(join(run, 'app.html'));
  evidence.appSha256 = createHash('sha256').update(html).digest('hex');
  const appUrl = await serve('/app.html', html,
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'");
  // Enable submit events inside the frame. The app CSP still blocks form navigation.
  const previewUrl = await serve('/preview', '<!doctype html><meta charset="utf-8">'
    + '<title>ROOT 隔离预览</title><style>body{margin:0}iframe{display:block;width:100vw;height:100vh;border:0}</style>'
    + '<iframe title="应用预览" sandbox="allow-scripts allow-same-origin allow-forms" src="' + appUrl + '"></iframe>',
    "default-src 'none'; style-src 'unsafe-inline'; frame-src " + new URL(appUrl).origin);

  chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
    '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
    '--disable-extensions', '--disable-component-update', '--no-proxy-server',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
    '--user-data-dir=' + profile, 'about:blank',
  ], { stdio: 'ignore' });
  let launchError;
  chrome.once('error', error => { launchError = error; });
  const port = await until(async () => {
    if (launchError) throw launchError;
    if (chrome.exitCode !== null) throw new Error('Chrome exited during startup');
    const file = await readFile(join(profile, 'DevToolsActivePort'), 'utf8').catch(() => '');
    return file.split('\n')[0];
  });
  const tabs = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
  await Promise.race([
    once(socket, 'open'),
    delay(5000, null, { ref: false }).then(() => { throw new Error('Chrome debugger connection timed out'); }),
  ]);
  const pending = new Map();
  const allow = new Set([appUrl, previewUrl]);
  let nextId = 0;
  function cdp(method, params = {}) {
    return new Promise((resolveResult, reject) => {
      const id = ++nextId;
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error(method + ' timed out'));
      }, 7000);
      pending.set(id, reply => {
        clearTimeout(timeout);
        if (reply.error) reject(new Error(method + ': ' + reply.error.message));
        else resolveResult(reply.result);
      });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const handler = pending.get(message.id);
      pending.delete(message.id);
      handler?.(message);
    }
    if (message.method === 'Fetch.requestPaused') {
      const { request, requestId } = message.params;
      if (request.method === 'GET' && allow.has(request.url)) {
        void cdp('Fetch.continueRequest', { requestId }).catch(() => {});
      } else {
        denied.push({ method: request.method, url: request.url });
        void cdp('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' }).catch(() => {});
      }
    }
  });

  const browser = await cdp('Browser.getVersion');
  evidence.browser = browser.product;
  evidence.isolation = 'Fresh Chrome profile, loopback-only servers, cross-origin sandboxed iframe, CSP and request allowlist';
  await cdp('Page.enable');
  await cdp('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
  await cdp('Emulation.setDeviceMetricsOverride', {
    width: 1000, height: 760, deviceScaleFactor: 1, mobile: false,
  });

  async function evaluate(expression, contextId) {
    const result = await cdp('Runtime.evaluate', { expression, contextId, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ': ' + result.result.description);
    return result.result.value;
  }

  async function waitForApp(navigationType) {
    return until(async () => {
      const topNavigation = await evaluate('performance.getEntriesByType("navigation")[0]?.type');
      if (topNavigation !== navigationType) return;
      const { frameTree } = await cdp('Page.getFrameTree');
      const frame = frameTree.childFrames?.find(child => child.frame.url === appUrl);
      if (!frame) return;
      const { executionContextId } = await cdp('Page.createIsolatedWorld', {
        frameId: frame.frame.id, worldName: 'root-verification',
      });
      const ready = await evaluate('document.readyState === "complete"', executionContextId);
      return ready ? { context: executionContextId, loader: frame.frame.loaderId } : undefined;
    });
  }

  async function readApp(context) {
    return evaluate(`({
      title: document.title,
      heading: document.querySelector('h1')?.innerText,
      emptyState: document.querySelector('#list > li.empty')?.innerText ?? null,
      records: Array.from(document.querySelectorAll('#list > li:not(.empty)'))
        .filter(row => row.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true }))
        .map(row => row.innerText),
      storage: Object.keys(localStorage).map(key => ({ key, value: localStorage.getItem(key) })),
      navigation: performance.getEntriesByType('navigation')[0]?.type,
    })`, context);
  }

  async function click(selector, context) {
    const target = await evaluate(`(() => {
      const rect = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    })()`, context);
    const frame = await evaluate(`(() => {
      const rect = document.querySelector('iframe').getBoundingClientRect();
      return { x: rect.x, y: rect.y };
    })()`);
    const point = { x: frame.x + target.x, y: frame.y + target.y, button: 'left', clickCount: 1 };
    await cdp('Input.dispatchMouseEvent', { ...point, type: 'mousePressed', buttons: 1 });
    await cdp('Input.dispatchMouseEvent', { ...point, type: 'mouseReleased', buttons: 0 });
  }

  await cdp('Page.navigate', { url: previewUrl });
  const { context: beforeContext, loader: beforeLoader } = await waitForApp('navigate');
  const initial = await readApp(beforeContext);
  assert.equal(initial.title, '月球温室巡检');
  assert.equal(initial.heading, '月球温室巡检');
  assert.deepEqual(initial.records, []);
  assert.equal(initial.emptyState, '暂无巡检记录。');
  assert.deepEqual(initial.storage, []);
  steps.push({ step: 'open_isolated_preview', status: 'passed', observed: initial });

  await click('input[name=item]', beforeContext);
  assert.equal(await evaluate('document.activeElement?.name', beforeContext), 'item');
  await cdp('Input.insertText', { text: sample });
  assert.equal(await evaluate('document.activeElement.value', beforeContext), sample);
  await click('button[type=submit]', beforeContext);
  const added = await until(async () => {
    const state = await readApp(beforeContext);
    return state.records.length ? state : undefined;
  });
  assert.equal(added.emptyState, null);
  assert.equal(added.records.length, 1);
  assert.ok(added.records[0].includes(sample));
  assert.ok(added.storage.some(entry => entry.value.includes(sample)));
  steps.push({ step: 'add_one_record', status: 'passed', input: 'Chrome mouse and text input', observed: added });

  await cdp('Page.reload', { ignoreCache: true });
  const { context: afterContext, loader: afterLoader } = await waitForApp('reload');
  assert.notEqual(afterLoader, beforeLoader);
  const restored = await readApp(afterContext);
  assert.equal(restored.title, initial.title);
  assert.equal(restored.heading, initial.heading);
  assert.deepEqual(restored.records, added.records);
  assert.deepEqual(restored.storage, added.storage);
  steps.push({
    step: 'reload_and_restore', status: 'passed', previewNavigation: 'reload',
    appDocumentReplaced: true, observed: restored,
  });
  const { data } = await cdp('Page.captureScreenshot', { format: 'png' });
  await writeFile(join(run, 'after-reload.png'), Buffer.from(data, 'base64'));
  evidence.status = 'passed';
} catch (error) {
  evidence.status = 'failed';
  evidence.error = error.message;
  process.exitCode = 1;
} finally {
  await writeFile(join(run, 'preview.json'), JSON.stringify(evidence, null, 2) + '\n');
  console.log(JSON.stringify(evidence, null, 2));
  socket?.close();
  if (chrome?.pid && chrome.exitCode === null && chrome.signalCode === null) {
    const exited = once(chrome, 'exit');
    chrome.kill();
    await exited;
  }
  for (const server of servers) {
    server.close();
    server.closeAllConnections();
  }
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
