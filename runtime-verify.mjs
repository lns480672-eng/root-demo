import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';

const chromePath = process.argv[2];
const profile = process.argv[3];
const input = [];
for await (const part of process.stdin) input.push(part);
const { html, entries, backup, limit, website, recordField } = JSON.parse(Buffer.concat(input).toString());
const recordCode = html.startsWith('<!doctype html><!--root:implementation=record-code-v1-->');
const storageDisabled = html.startsWith('<!doctype html><!--root:storage=none-->');
if (storageDisabled && entries.length) {
  process.stdout.write('failed:storage_not_allowed\n');
  process.exit(0);
}
const workbench = await readFile(new URL('./workbench/workbench.js', import.meta.url), 'utf8');
const start = workbench.indexOf('function initializeFrame(');
const end = workbench.indexOf('\nfunction enqueueSave(', start);
if (start < 0 || end < start) process.exit(1);
const initializeFrame = workbench.slice(start, end).trim();
const token = randomUUID();
const wrapperPath = '/verification/' + token;
const canaryPath = '/canary/' + token;
const server = createServer((request, response) => {
  if (request.method !== 'GET') return response.writeHead(404).end();
  if (request.url === wrapperPath) {
    return response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Connection-Allowlist': '(); webrtc=block',
      'Referrer-Policy': 'no-referrer',
      'Content-Security-Policy': "default-src 'none'; connect-src 'self'; frame-src about:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:",
    }).end('<!doctype html><title>ROOT runtime verifier</title><main></main>');
  }
  if (request.url === canaryPath) return response.writeHead(200).end('reachable');
  response.writeHead(404).end();
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const origin = 'http://127.0.0.1:' + server.address().port;
const wrapper = origin + wrapperPath;
const canary = origin + canaryPath;
const previewPolicy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
  + "img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
const blocksWebRTC = `(() => {
  try {
    const pc = new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:9'}]});
    try { return pc.getConfiguration().iceServers.length === 0; }
    finally { pc.close(); }
  } catch (error) { return error.name === 'NotAllowedError' || error.name === 'SecurityError'; }
})()`;
const args = JSON.stringify([entries, token, origin, limit, backup && !storageDisabled, storageDisabled, recordCode]).replaceAll('<', '\\u003c');
const srcdoc = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
  + '<meta http-equiv="Content-Security-Policy" content="' + previewPolicy + '">'
  + '<script>(' + initializeFrame + ')(...' + args + ')</script>' + html.slice('<!doctype html>'.length);
const chrome = spawn(chromePath, [
  '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
  '--disable-extensions', '--disable-component-update', '--disable-sync', '--disable-domain-reliability',
  '--disable-breakpad', '--disable-crash-reporter', '--disable-default-apps', '--disable-features=MediaRouter',
  '--use-mock-keychain', '--password-store=basic', '--disable-quic',
  '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1',
  '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
  '--remote-debugging-pipe', '--user-data-dir=' + profile, 'about:blank',
], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });

let closed = false;
let failure = '';
let candidateStarted = false;
let served = false;
let canaryEnabled = true;
let serial = 0;
let targetSession;
const pending = new Map();
const targets = new Map();
const wire = chrome.stdio[3];
const reply = chrome.stdio[4];
let candidateFrameId;
const deadline = Date.now() + 8500;

function close() {
  if (closed) return;
  closed = true;
  for (const { reject } of pending.values()) reject(new Error('Chromium closed'));
  pending.clear();
}
chrome.once('error', close);
chrome.once('exit', close);
wire.on('error', close);
reply.on('error', close);

function cdp(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    if (closed) return reject(new Error('Chromium closed'));
    const id = ++serial;
    pending.set(id, { resolve, reject });
    wire.write(JSON.stringify({ id, method, params, sessionId }) + '\0');
  });
}

const autoAttach = { autoAttach: true, waitForDebuggerOnStart: true, flatten: true };
async function attached(params) {
  const id = params.sessionId;
  if (params.targetInfo.type !== 'iframe') {
    // The candidate never needs workers, popups, or another browsing target.
    failure ||= 'isolation_event';
    return;
  }
  const ready = (async () => {
    await cdp('Runtime.enable', {}, id);
    await cdp('Page.enable', {}, id);
    await cdp('Fetch.enable', {}, id);
    await cdp('Target.setAutoAttach', autoAttach, id);
    await cdp('Runtime.runIfWaitingForDebugger', {}, id);
  })();
  targets.set(params.targetInfo.targetId, { id, ready });
  await ready;
}

async function requestPaused(params, id) {
  const { requestId, request } = params;
  if (id === targetSession && !served && request.method === 'GET' && request.url === wrapper) {
    served = true;
    return cdp('Fetch.continueRequest', { requestId }, id);
  }
  // A reachable positive control if the network-layer policy was ignored.
  if (id === targetSession && canaryEnabled && request.method === 'GET' && request.url === canary) {
    return cdp('Fetch.continueRequest', { requestId }, id);
  }
  if (candidateStarted && params.resourceType === 'Document') failure ||= 'frame_changed';
  return cdp('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' }, id);
}

function receive(message) {
  if (message.id) {
    const request = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) request?.reject(new Error('CDP command failed'));
    else request?.resolve(message.result);
    return;
  }
  const { method, params, sessionId } = message;
  if (method === 'Runtime.exceptionThrown' && candidateStarted) failure ||= 'script_exception';
  if (method === 'Page.frameNavigated' && candidateStarted) failure ||= 'frame_changed';
  if (method === 'Page.frameAttached' && sessionId === targetSession) {
    if (candidateFrameId) failure ||= 'isolation_event';
    else candidateFrameId = params.frameId;
  }
  if (method === 'Page.frameDetached' && candidateStarted && params.reason !== 'swap') failure ||= 'frame_changed';
  if (method === 'Target.attachedToTarget' && params.targetInfo.type !== 'page') {
    void attached(params).catch(() => { failure ||= 'isolation_event'; });
  }
  if (method === 'Fetch.requestPaused') void requestPaused(params, sessionId).catch(() => { failure ||= 'isolation_event'; });
}

let buffer = '';
reply.setEncoding('utf8');
reply.on('data', chunk => {
  if (buffer.length + chunk.length > 2 * 1024 * 1024) {
    failure ||= 'isolation_event';
    close();
    reply.destroy();
    chrome.kill();
    return;
  }
  buffer += chunk;
  let boundary;
  while ((boundary = buffer.indexOf('\0')) !== -1) {
    const message = buffer.slice(0, boundary);
    buffer = buffer.slice(boundary + 1);
    try { receive(JSON.parse(message)); } catch { close(); }
  }
});

async function evaluate(expression, contextId, sessionId = targetSession) {
  const result = await cdp('Runtime.evaluate', {
    expression, contextId, returnByValue: true, awaitPromise: true,
  }, sessionId);
  if (result.exceptionDetails) throw new Error('Trusted observation failed');
  return result.result.value;
}

async function candidateFrame() {
  const target = targets.get(candidateFrameId);
  if (target) await target.ready;
  const sessionId = target?.id ?? targetSession;
  const { frameTree } = await cdp('Page.getFrameTree', {}, sessionId);
  const tree = target ? frameTree : frameTree.childFrames?.find(node => node.frame.id === candidateFrameId);
  if (tree?.frame.url !== 'about:srcdoc' || tree.frame.id !== candidateFrameId) return;
  return { sessionId, frame:tree.frame };
}

async function verifyWebsiteTabs() {
  const groupSelector = '[role="tablist"]';
  const visible = node => node?.checkVisibility({visibilityProperty:true});
  const tabs = group => [...group.querySelectorAll('[role="tab"]')].filter(
    tab => tab.closest(groupSelector) === group && visible(tab)
      && !tab.matches(':disabled, [aria-disabled="true"]'),
  );
  const selected = items => items.filter(tab => tab.getAttribute('aria-selected') === 'true');
  const panelIds = tab => (tab.getAttribute('aria-controls') || '').trim().split(/\s+/).filter(Boolean);
  const panelsMatch = (group, items, active) => {
    const activePanels = new Set(panelIds(active));
    for (const id of new Set(items.flatMap(panelIds))) {
      const panel = document.getElementById(id);
      if (!panel || panel.contains(group) || visible(panel) !== activePanels.has(id)) return false;
    }
    return true;
  };
  const contentText = ids => {
    const scopes = ids.size
      ? [...ids].map(id => document.getElementById(id)).filter(Boolean)
      : [document.getElementById('root-website') || document.body];
    const text = [];
    for (const scope of scopes) {
      const nodes = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
      for (let node; (node = nodes.nextNode());) {
        const element = node.parentElement;
        const value = node.textContent.trim();
        if (value && !element.closest(groupSelector) && visible(element)) text.push(value);
      }
    }
    return text.join(' ').replace(/\s+/g, ' ');
  };
  const budget = performance.now() + 2500;
  for (const [position, original] of [...document.querySelectorAll(groupSelector)].entries()) {
    const findGroup = () => original.id
      ? document.getElementById(original.id)
      : document.querySelectorAll(groupSelector)[position];
    const items = tabs(original);
    if (items.length < 2) continue;
    const initial = selected(items);
    if (initial.length !== 1) return false;
    const first = items.indexOf(initial[0]);
    for (let step = 1; step <= items.length; step++) {
      if (performance.now() >= budget) return false;
      const index = (first + step) % items.length;
      const findTab = current => items[index].id
        ? current.find(tab => tab.id === items[index].id)
        : current[index];
      const group = findGroup();
      if (!group) return false;
      const previous = tabs(group);
      const target = findTab(previous);
      const activeBefore = selected(previous);
      if (!target || activeBefore.length !== 1) return false;
      const previousPanels = new Set(panelIds(activeBefore[0]));
      const targetPanels = new Set(panelIds(target));
      const hasDistinctPanels = previousPanels.size && targetPanels.size
        && (previousPanels.size !== targetPanels.size || [...previousPanels].some(id => !targetPanels.has(id)));
      const sharedPanels = previousPanels.size && targetPanels.size ? targetPanels : new Set();
      const before = hasDistinctPanels ? null : contentText(sharedPanels);
      target.click();
      const changed = () => {
        const currentGroup = findGroup();
        if (!currentGroup) return false;
        const current = tabs(currentGroup);
        const active = selected(current);
        return active.length === 1 && active[0] === findTab(current)
          && panelsMatch(currentGroup, current, active[0])
          && (before === null || contentText(sharedPanels) !== before);
      };
      const deadline = Math.min(budget, performance.now() + 200);
      while (!changed() && performance.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 25));
      }
      if (!changed()) return false;
    }
  }
  return true;
}

async function run() {
  await cdp('Browser.getVersion');
  const { targetInfos } = await cdp('Target.getTargets');
  const { targetId } = targetInfos.find(target => target.type === 'page');
  ({ sessionId: targetSession } = await cdp('Target.attachToTarget', { targetId, flatten: true }));
  await cdp('Page.enable', {}, targetSession);
  await cdp('Runtime.enable', {}, targetSession);
  await cdp('Fetch.enable', {}, targetSession);
  await cdp('Target.setAutoAttach', autoAttach, targetSession);
  await cdp('Browser.setDownloadBehavior', { behavior: 'deny' });
  const top = await cdp('Page.navigate', { url: wrapper }, targetSession);
  if (top.errorText) throw new Error('Could not load verification wrapper');
  const { executionContextId } = await cdp('Page.createIsolatedWorld', {
    frameId: top.frameId, worldName: 'root-host-observer',
  }, targetSession);
  const policy = await evaluate(`(async () => {
    if (location.href !== ${JSON.stringify(wrapper)}) return false;
    let httpBlocked = false;
    try { await fetch(${JSON.stringify(canary)}); } catch (error) { httpBlocked = error instanceof TypeError; }
    return httpBlocked && ${blocksWebRTC};
  })()`);
  if (!policy || failure) throw new Error('Chromium does not enforce the verification network policy');
  canaryEnabled = false;
  server.close();

  // Attach Runtime to the opaque frame before any untrusted script can run.
  await evaluate(`(() => {
    const frame = document.createElement('iframe');
    frame.setAttribute('sandbox', ${JSON.stringify('allow-scripts allow-forms' + (backup && !storageDisabled ? ' allow-downloads' : ''))});
    frame.referrerPolicy = 'no-referrer';
    frame.srcdoc = '<!doctype html><title>Empty verification frame</title>';
    document.body.append(frame);
    return true;
  })()`, executionContextId);
  let candidate;
  while (!candidate && !failure && Date.now() < deadline) {
    if (candidateFrameId) candidate = await candidateFrame();
    if (!candidate) await delay(25);
  }
  if (!candidate || failure) throw new Error('Could not attach to the isolated verification frame');
  const { sessionId, frame } = candidate;
  const observer = await cdp('Page.createIsolatedWorld', {
    frameId: frame.id, worldName: 'root-candidate-observer',
  }, sessionId);
  if (!await evaluate("self.origin === 'null'", observer.executionContextId, sessionId)) {
    throw new Error('Verification frame is not opaque');
  }
  if (recordCode) {
    const initial = new Map(entries).get('root-structured-records-v1') || '[]';
    await evaluate(`(() => {
      const frame = document.querySelector('iframe');
      const records = JSON.parse(${JSON.stringify(initial)});
      document.documentElement.dataset.recordProof = 'waiting';
      window.addEventListener('message', event => {
        if (event.source !== frame.contentWindow || event.origin !== 'null' ||
            event.data?.token !== ${JSON.stringify(token)} || event.data.type !== 'record-connect' ||
            event.ports.length !== 1 || document.documentElement.dataset.recordProof !== 'waiting') return;
        const port = event.ports[0];
        document.documentElement.dataset.recordProof = 'connected';
        port.onmessage = ({data}) => {
          if (data?.type !== 'record-operation') return;
          const action = data.operation?.action;
          const field = ${JSON.stringify(recordField ?? null)};
          const sample = field?.type === 'date' ? '2026-01-17' : field?.type === 'integer' ? 17 : 'ROOT test value';
          const matched = field && document.documentElement.dataset.recordCheck === 'active' && action === 'create' &&
            data.operation.values?.[field.id] === sample;
          const error = action === 'list' || matched ? undefined : '候选验证期间不能写入记录。';
          if (matched) document.documentElement.dataset.recordCheck = 'passed';
          const proof = document.documentElement.dataset.recordProof;
          document.documentElement.dataset.recordProof = error || proof === 'write' ? 'write' : 'read';
          port.postMessage({type: 'record-result', request: data.request, records, error});
        };
      });
    })()`, executionContextId);
  }
  candidateStarted = true;
  await cdp('Page.setDocumentContent', { frameId: frame.id, html: srcdoc }, sessionId);
  await delay(250);
  const current = await candidateFrame();
  if (failure) return 'failed:' + failure;
  if (!current || current.frame.loaderId !== frame.loaderId) return 'failed:frame_changed';
  const result = await evaluate(
    `document.readyState === 'complete' && Boolean(document.body?.childElementCount) && ${blocksWebRTC}`,
    observer.executionContextId, sessionId);
  if (failure) return 'failed:' + failure;
  if (!result) return 'failed:document_not_ready';
  if (recordCode) {
    const deadline = Date.now() + 1000;
    let proof;
    do {
      proof = await evaluate("document.documentElement.dataset.recordProof", executionContextId);
      if (failure || proof === 'read' || proof === 'write') break;
      await delay(25);
    } while (Date.now() < deadline);
    if (failure || proof !== 'read') return 'failed:' + (failure || 'record_initialization');
  }
  if (recordField) {
    const prepared = await evaluate(`(() => {
      const field = document.getElementById(${JSON.stringify(recordField.id)});
      const kind = ${JSON.stringify(recordField.type)};
      const expected = {date: 'date', integer: 'number', text: 'text'}[kind];
      if (!field?.form || field.type !== expected || field.required || field.disabled || field.readOnly) return false;
      for (const input of field.form.elements) {
        if (input === field || input.disabled || !input.required || input.value) continue;
        if (input instanceof HTMLSelectElement) {
          const option = [...input.options].find(option => option.value && !option.disabled);
          if (option) input.value = option.value;
        } else if (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement) {
          input.value = input.type === 'date' ? '2026-01-16' : input.type === 'number' ? '1' : 'ROOT example';
        }
        input.dispatchEvent(new Event('input', {bubbles: true}));
      }
      field.value = kind === 'date' ? '2026-01-17' : kind === 'integer' ? '17' : 'ROOT test value';
      field.dispatchEvent(new Event('input', {bubbles: true}));
      const submit = [...field.form.elements].find(input => input.type === 'submit' && !input.disabled);
      if (!submit) return null;
      submit.scrollIntoView({block:'center', inline:'center'});
      const box = submit.getBoundingClientRect();
      return box.width && box.height ? {x:box.x + box.width / 2, y:box.y + box.height / 2} : null;
    })()`, observer.executionContextId, sessionId);
    if (!prepared) return 'failed:record_field_behavior';
    await evaluate("document.documentElement.dataset.recordCheck = 'active'", executionContextId);
    const offset = await evaluate(`(() => {
      const iframe = document.querySelector('iframe');
      const box = iframe.getBoundingClientRect();
      return {x:box.x + iframe.clientLeft, y:box.y + iframe.clientTop};
    })()`, executionContextId);
    const point = {x:offset.x + prepared.x, y:offset.y + prepared.y, button:'left', clickCount:1};
    await cdp('Input.dispatchMouseEvent', {type:'mousePressed', ...point}, targetSession);
    await cdp('Input.dispatchMouseEvent', {type:'mouseReleased', ...point}, targetSession);
    const deadline = Date.now() + 1000;
    while (!failure && Date.now() < deadline) {
      if (await evaluate("document.documentElement.dataset.recordCheck", executionContextId) === 'passed') break;
      await delay(25);
    }
    if (failure || await evaluate("document.documentElement.dataset.recordCheck", executionContextId) !== 'passed') {
      return 'failed:' + (failure || 'record_field_behavior');
    }
  }
  if (website && !await evaluate(
    '(' + verifyWebsiteTabs.toString() + ')()', observer.executionContextId, sessionId,
  )) return 'failed:' + (failure || 'website_tabs');
  return failure ? 'failed:' + failure : 'ok';
}

let timer;
try {
  const verdict = await Promise.race([
    run(),
    new Promise(resolve => { timer = setTimeout(() => resolve(candidateStarted ? 'failed:' + (failure || 'runtime_timeout') : 'unavailable'), 9000); }),
  ]);
  process.stdout.write(verdict + '\n');
} catch {
  process.stdout.write(failure && candidateStarted ? 'failed:' + failure + '\n' : 'unavailable\n');
} finally {
  clearTimeout(timer);
  server.closeAllConnections();
  server.close();
  chrome.kill();
  wire.destroy();
  reply.destroy();
}
