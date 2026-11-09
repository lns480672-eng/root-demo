'use strict';

const byId = id => document.getElementById(id);
const product = document.querySelector('.elys-product');
const form = byId('composer');
const requirement = byId('requirement');
const generate = byId('generate');
const example = byId('example');
const preview = byId('preview');
const workspace = byId('workspace');
const composer = form.closest('section');
const library = byId('tool-list').closest('section');
const toolHistory = byId('tool-version-panel');
const maxStorageBytes = 256 * 1024;
const publicationKey = 'root:pending-publication';
const publicationTimeout = 6000;
const validToolId = id => typeof id === 'string' && /^run-[a-z0-9_]{8}$/.test(id);
const previewPolicy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
  + "img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
let session = null;
let identityResolved = false;
let registering = false;
let configured = false;
let networkIsolated = false;
let active = null;
let candidate = null;
let phase = 'startup';
let failure = '';
let restored = false;
let editing = false;
let pendingPublication = null;
let entryMode = 'direct';
let templateKey = 'board';
let sampleTemplate = null;
let guideStep = -1;
let guideTool = null;
let guideTarget = null;

function render() {
  const pending = ['model', 'preview', 'restore', 'sync'].includes(phase);
  const syncing = Boolean(pendingPublication);
  const blocked = pending || syncing;
  const showingSample = Boolean(sampleTemplate);
  const showingTool = Boolean(active) && !showingSample;
  product.dataset.state = pending ? 'loading' : failure ? 'failure' : active ? 'success' : 'initial';
  generate.textContent = pending ? '生成中…' : failure ? '重试' : editing ? '生成新版' : '直接生成';
  generate.disabled = !session || !configured || !networkIsolated || phase === 'startup' || blocked || !requirement.value.trim();
  requirement.readOnly = blocked;
  byId('new-tool').disabled = blocked;
  let currentTitle = '工具预览';
  for (const button of byId('tool-list').querySelectorAll('button')) {
    button.disabled = blocked || !networkIsolated;
    const selected = button.dataset.id === active?.id;
    button.setAttribute('aria-current', String(selected));
    if (selected) currentTitle = button.textContent;
  }
  byId('preview-title').textContent = showingSample ? quickTemplates[sampleTemplate].title : currentTitle;
  byId('composer-title').textContent = editing ? '要怎样修改这个工具？' : '你想做什么工具？';
  byId('revise-tool').hidden = byId('tool-history').hidden = byId('publish-lock').hidden = syncing || !showingTool;
  byId('revise-tool').disabled = pending || !configured;
  byId('tool-history').textContent = active ? '工具 v' + active.version + ' · 版本历史' : '版本历史';
  const sync = byId('sync-tool');
  sync.hidden = !syncing;
  sync.disabled = pending;
  const publicationMessage = byId('publication-message');
  publicationMessage.hidden = !syncing;
  publicationMessage.textContent = pending ? '正在核对已发布版本，编辑暂时暂停。'
    : '暂时无法确认发布结果。编辑已暂停，请点击「同步工具」后继续。';
  example.hidden = editing || blocked || Boolean(failure);
  byId('error').textContent = failure;
  byId('error').hidden = !failure;
  byId('loading').hidden = syncing || !pending;
  byId('phase').textContent = {
    model: '正在等待 DeepSeek 响应',
    preview: '正在准备预览',
    restore: '正在恢复上次的工具',
  }[phase] || '';
  for (const step of ['model', 'preview']) {
    const item = byId(step + '-step');
    if (step === phase) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
  }
  const status = byId('preview-status');
  status.toggleAttribute('data-usable', showingTool && !syncing);
  status.textContent = syncing ? '待同步 · 暂停编辑' : showingSample ? '样例试用 · 不保存' : active
    ? pending || failure ? '上次工具仍可用' : restored ? '已恢复' : '已就绪'
    : pending ? '等待预览' : '待生成';
  byId('empty').hidden = showingTool || showingSample || syncing;
  byId('empty-title').textContent = pending ? '等待预览' : '还没有生成工具';
  byId('empty-copy').textContent = pending ? '工具准备好后，会在这里显示。' : '输入需求后，工具会在这里出现。';
  const fast = entryMode === 'fast';
  byId('quick-panel').hidden = !fast;
  form.hidden = fast && !failure;
  byId('quick-entry').setAttribute('aria-pressed', String(fast));
  byId('direct-entry').setAttribute('aria-pressed', String(!fast && !editing));
  for (const button of [byId('quick-entry'), byId('direct-entry'), byId('template-sample'),
    byId('template-customize'), ...document.querySelectorAll('[data-entry-template]')]) {
    button.disabled = blocked;
  }
  byId('template-generate').disabled = blocked || !configured || !networkIsolated;
  byId('guide-start').disabled = blocked;
  renderGuide();
}

async function api(path, options) {
  let response;
  let data;
  try {
    response = await fetch(path, options);
    data = await response.json();
  } catch {
    const error = new Error('无法连接本机服务，请检查工作台是否正在运行。');
    error.code = 'transport';
    throw error;
  }
  if (!response.ok) {
    const error = new Error(data.message || '请求未完成，请重试。');
    error.code = data.code;
    throw error;
  }
  return data;
}

function post(data) {
  return { method: 'POST', body: JSON.stringify(data), headers: { 'Content-Type': 'application/json' } };
}

async function workspaceApi(path, data, timeout) {
  const options = data === undefined ? { headers: {} } : post(data);
  options.headers['X-Root-Session'] = session.marker;
  if (timeout) options.signal = AbortSignal.timeout(timeout);
  try {
    return await api(path, options);
  } catch (error) {
    if (error.code === 'login_required' || error.code === 'session_changed') {
      byId('workspace').hidden = true;
      preview.replaceChildren();
      location.reload();
    }
    throw error;
  }
}

async function loadLibrary() {
  const { items, latest } = await workspaceApi('/api/tools');
  const buttons = items.map(tool => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.id = tool.id;
    button.textContent = tool.title;
    button.addEventListener('click', () => selectTool(tool.id));
    li.append(button);
    return li;
  });
  byId('tool-list').replaceChildren(...buttons);
  byId('library-empty').hidden = items.length > 0;
  render();
  return latest;
}

function compose(enabled) {
  workspace.dataset.mode = enabled ? 'compose' : 'browse';
  if (enabled) {
    workspace.prepend(composer);
    workspace.append(library);
  } else {
    workspace.prepend(library);
    workspace.append(composer);
  }
}

const guideSteps = [
  ['quick-entry', '第 1 步 · 点「快速体验」，先挑一个示例。'],
  ['template-generate', '第 2 步 · 选一个模板，点「用此模板生成」。也可以先试用样例。'],
  ['preview', '第 3 步 · 在预览中实际操作一次，例如添加一条记录或计算结果。'],
  ['tool-list', '第 4 步 · 到「我的工具」重新打开刚才生成的工具。'],
];

function renderGuide() {
  const panel = byId('workspace-guide');
  const step = guideSteps[guideStep];
  const visible = Boolean(step) && !pendingPublication &&
    (guideStep !== 1 || entryMode === 'fast') && (guideStep !== 2 || !sampleTemplate);
  panel.hidden = !visible;
  const nextTarget = visible ? byId(step[0]) : null;
  if (nextTarget !== guideTarget) {
    guideTarget?.removeAttribute('data-guide-target');
    nextTarget?.setAttribute('data-guide-target', '');
    guideTarget = nextTarget;
  }
  if (!nextTarget) return;
  const anchor = [byId('quick-entry').parentElement, byId('quick-panel'), preview, byId('tool-list')][guideStep];
  if (anchor.previousElementSibling !== panel) anchor.before(panel);
  const copy = byId('guide-copy');
  if (copy.textContent !== step[1]) copy.textContent = step[1];
}

function guidePreference() {
  return 'root:workbench-guide:' + session.user;
}

function startGuide() {
  guideTool = null;
  guideStep = 0;
}

function dismissGuide() {
  guideTool = null;
  guideStep = -1;
  try {
    localStorage.setItem(guidePreference(), 'seen');
  } catch {
    // A restricted browser can still dismiss the guide in this document.
  }
}

function restoreGuide() {
  try {
    if (localStorage.getItem(guidePreference()) === 'seen') return;
  } catch {
    // Offer the guide when a preference cannot be read.
  }
  startGuide();
}

function hideSample() {
  if (!sampleTemplate) return;
  sampleTemplate = null;
  closeSample();
  preview.hidden = false;
}

function enterQuickExperience() {
  editing = false;
  entryMode = 'fast';
  failure = '';
  compose(true);
  if (guideStep === 0) guideStep = 1;
  render();
  byId('quick-panel').querySelector('[aria-pressed="true"]')?.focus();
}

byId('quick-entry').addEventListener('click', enterQuickExperience);
byId('direct-entry').addEventListener('click', () => beginComposition(false, editing ? '' : requirement.value));
byId('guide-start').addEventListener('click', () => {
  if (phase !== 'idle' || pendingPublication) return;
  startGuide();
  beginComposition(false);
  byId('quick-entry').focus();
});
byId('guide-skip').addEventListener('click', () => {
  dismissGuide();
  render();
  byId('guide-start').focus();
});
for (const button of document.querySelectorAll('[data-entry-template]')) {
  button.addEventListener('click', () => {
    templateKey = button.dataset.entryTemplate;
    renderEntryTemplate(templateKey);
  });
}
byId('template-sample').addEventListener('click', () => {
  sampleTemplate = templateKey;
  showToolHistory(false);
  preview.hidden = true;
  openSample(templateKey);
  render();
  byId('sample-close').focus();
});
byId('sample-close').addEventListener('click', () => {
  hideSample();
  render();
  byId('template-sample').focus();
});
byId('template-customize').addEventListener('click', () => {
  beginComposition(false, quickTemplates[templateKey].requirement);
});
byId('template-generate').addEventListener('click', () => {
  requirement.value = quickTemplates[templateKey].requirement;
  render();
  form.requestSubmit();
});

function versionRecord({ version, note, current, date, label, build }) {
  const row = document.createElement('li');
  const header = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = version + (current ? ' · ' + current : '');
  if (current) row.setAttribute('aria-current', 'true');
  const time = document.createElement('time');
  if (date) time.dateTime = date;
  time.textContent = label;
  header.append(title, time);
  const description = document.createElement('p');
  description.textContent = note;
  row.append(header, description);
  if (build) {
    const code = document.createElement('code');
    code.textContent = build;
    row.append(code);
  }
  return row;
}

function showToolHistory(open) {
  toolHistory.hidden = !open;
  byId('tool-history').setAttribute('aria-expanded', String(open));
  if (open) loadToolHistory();
}

let historyRequest = 0;
async function loadToolHistory() {
  const request = ++historyRequest;
  const id = active.id;
  const list = byId('tool-version-list');
  const status = byId('tool-version-status');
  byId('version-tool-name').textContent = byId('preview-title').textContent;
  list.replaceChildren();
  status.textContent = '正在读取工具版本…';
  try {
    const { current, items } = await workspaceApi('/api/tools/' + id + '/versions');
    if (request !== historyRequest || active?.id !== id) return;
    list.replaceChildren(...items.map(item => {
      const date = item.effectiveAt && new Date(item.effectiveAt * 1000);
      return versionRecord({
        version: 'v' + item.version, note: item.note,
        current: item.version === current ? '当前' : '',
        date: date && date.toISOString(),
        label: date ? date.toLocaleString('zh-CN') + (item.approximate ? '（文件时间）' : '') : '时间未记录',
      });
    }));
    status.textContent = '';
  } catch (error) {
    if (request === historyRequest && active?.id === id) status.textContent = error.message;
  }
}

let releasesRequest = null;
function loadReleases() {
  if (releasesRequest) return;
  releasesRequest = api('/api/releases').then(({ current, items }) => {
    byId('updates-link').textContent = '工作台版本 ' + current + ' · 更新记录';
    byId('release-list').replaceChildren(...items.map(item => versionRecord({
      ...item, label: item.date, current: item.version === current ? '当前发布' : '',
    })));
    byId('release-status').textContent = '';
  }).catch(error => {
    releasesRequest = null;
    byId('release-status').textContent = error.message;
  });
}

function renderRoute(focus = false) {
  const updates = location.pathname === '/updates';
  byId('updates').hidden = byId('updates-back').hidden = !updates;
  byId('updates-link').hidden = updates;
  byId('auth').hidden = updates || !identityResolved || Boolean(session);
  byId('account').hidden = workspace.hidden = updates || !session;
  document.title = updates ? '工作台更新记录 · ROOT' : 'ROOT 工具工作台';
  if (updates) loadReleases();
  if (focus) byId(updates ? 'updates-title' : 'updates-link').focus();
}

for (const id of ['updates-link', 'updates-back']) {
  byId(id).addEventListener('click', event => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (id === 'updates-link') history.pushState({ fromWorkbench: true }, '', '/updates');
    else if (history.state?.fromWorkbench) {
      history.back();
      return;
    } else history.replaceState(null, '', '/');
    renderRoute(true);
  });
}
window.addEventListener('popstate', () => renderRoute(true));
byId('tool-history').addEventListener('click', () => showToolHistory(toolHistory.hidden));

async function confirmsNetworkPolicy() {
  try {
    // CSP permits this same-origin request. Only the browser's egress allowlist should reject it.
    await fetch('/api/egress-check', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    return false;
  } catch (error) {
    return error instanceof TypeError;
  }
}

function warnStorage(message) {
  const notice = byId('storage-warning');
  notice.textContent = message;
  notice.hidden = !message;
}

function serializeEntries(entries) {
  if (!Array.isArray(entries) || !entries.every(
    entry => Array.isArray(entry) && entry.length === 2 && entry.every(value => typeof value === 'string'),
  )) throw new TypeError('无效的工具存储数据');
  const json = JSON.stringify(entries);
  if (new TextEncoder().encode(json).length > maxStorageBytes) throw new RangeError('工具存储已满');
  return json;
}

// Runs inside the opaque frame before any generated code. It has only this tool's snapshot.
function initializeFrame(entries, token, parentOrigin, limit) {
  const send = parent.postMessage.bind(parent);
  const values = new Map(entries);
  const existing = new Set(values.keys());
  const reads = new Set();
  let paused = false;
  const report = (type, extra = {}) => send({ token, type, ...extra }, parentOrigin);
  const reportOperation = event => {
    if (paused || !event.isTrusted) return;
    if (event.type === 'click') {
      const control = event.target.closest?.('button, [role="button"]');
      if (!control || control.disabled || (control.type === 'submit' && control.form)) return;
    }
    report('operation');
  };
  for (const type of ['click', 'change', 'submit']) window.addEventListener(type, reportOperation, true);
  window.addEventListener('message', event => {
    if (event.source !== parent || event.origin !== parentOrigin || event.data?.token !== token) return;
    if (event.data.type === 'pause-storage') {
      paused = true;
      report('storage-paused', { reads: Array.from(reads) });
    } else if (event.data.type === 'resume-storage') {
      paused = false;
    }
  });
  function commit(next) {
    if (paused) throw new DOMException('正在切换工具版本，请稍后重试。', 'InvalidStateError');
    const snapshot = Array.from(next);
    if (new TextEncoder().encode(JSON.stringify(snapshot)).length > limit) {
      report('quota');
      throw new DOMException('工具存储已满', 'QuotaExceededError');
    }
    values.clear();
    for (const [key, value] of next) values.set(key, value);
    report('storage', { entries: snapshot });
  }
  Object.defineProperty(window, 'localStorage', {
    value: Object.freeze({
      get length() { return values.size; },
      getItem(key) {
        const name = String(key);
        if (existing.has(name)) reads.add(name);
        return values.get(name) ?? null;
      },
      setItem(key, value) { commit(new Map(values).set(String(key), String(value))); },
      removeItem(key) {
        const next = new Map(values);
        next.delete(String(key));
        commit(next);
      },
      clear() { commit(new Map()); },
      key(index) { return Array.from(values.keys())[index >>> 0] ?? null; },
    }),
  });
  let startupError = false;
  const markError = () => { startupError = true; };
  window.addEventListener('error', markError);
  window.addEventListener('unhandledrejection', markError);
  const schedule = window.setTimeout.bind(window);
  window.addEventListener('load', () => schedule(() => {
    window.removeEventListener('error', markError);
    window.removeEventListener('unhandledrejection', markError);
    report(startupError || !document.body?.childElementCount ? 'init-error' : 'ready');
  }), { once: true });
}

function enqueueSave(frame, snapshot) {
  frame.saveTail = frame.saveTail.then(async () => {
    if (frame.saveFailed) return;
    if (frame === active) warnStorage('正在保存…');
    try {
      const saved = await workspaceApi('/api/tools/' + frame.id + '/data', {
        revision: frame.revision, version: frame.version, entries: JSON.parse(snapshot),
      });
      frame.revision = saved.revision;
      if (frame === active) warnStorage('');
    } catch (error) {
      frame.saveFailed = true;
      if (frame === active) warnStorage(error.message + ' 本次修改未保存。');
    }
  });
}

window.addEventListener('message', event => {
  if (event.origin !== 'null' || !event.data || typeof event.data !== 'object') return;
  const frame = [candidate, active].find(
    item => item && event.source === item.iframe.contentWindow && event.data.token === item.token,
  );
  if (!frame) return;
  const { type, entries } = event.data;
  if (type === 'storage') {
    if (frame === active && pendingPublication) return;
    try {
      const snapshot = serializeEntries(entries);
      frame.snapshot = snapshot;
      if (frame === active) enqueueSave(frame, snapshot);
    } catch {
      if (frame === candidate) frame.reject(new Error('无法保存新工具的数据，旧工具已保留。'));
      else warnStorage('这次更改未能保存，刷新后可能丢失。');
    }
  } else if (type === 'quota') {
    if (frame === candidate) frame.reject(new Error('新工具数据超出浏览器存储限制。'));
    else warnStorage('工具存储空间已满，这次更改未保存。');
  } else if (frame === candidate && type === 'ready') {
    frame.resolve();
  } else if (frame === candidate && type === 'init-error') {
    frame.reject(new Error('新工具预览初始化失败，需求已保留。'));
  } else if (type === 'storage-paused') {
    frame.acknowledgePause?.(event.data.reads);
  } else if (type === 'operation' && guideStep === 2 && frame === active && frame.id === guideTool && !pendingPublication) {
    guideStep = 3;
    renderGuide();
  }
});

function signalStorage(frame, type) {
  frame.iframe.contentWindow.postMessage({ type, token: frame.token }, '*');
}

async function pauseStorage(frame) {
  frame.iframe.inert = true;
  const acknowledgement = new Promise(resolve => { frame.acknowledgePause = resolve; });
  let timeout;
  try {
    signalStorage(frame, 'pause-storage');
    return await Promise.race([acknowledgement, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error('切换版本超时，旧版仍可用。')), 2000);
    })]);
  } finally {
    clearTimeout(timeout);
    frame.acknowledgePause = null;
  }
}

function resumeStorage(frame) {
  signalStorage(frame, 'resume-storage');
  frame.iframe.inert = false;
}

async function publishRevision(frame, old, tool) {
  const reads = await pauseStorage(frame);
  if (!Array.isArray(reads) || !tool.entries.every(([key]) => reads.includes(key))) {
    throw new Error('新版无法读取全部已有数据，旧版仍可用。');
  }
  // The old frame's acknowledgement follows its queued storage messages.
  await pauseStorage(old);
  await old.saveTail;
  if (old.saveFailed) throw new Error('旧版还有未保存的修改，请先解决保存问题再试。');
  const path = '/api/tools/' + frame.id + '/publish';
  const payload = { runId: tool.runId, revision: frame.revision, entries: JSON.parse(frame.snapshot) };
  const operation = { id: frame.id, runId: tool.runId, marker: session.marker };
  sessionStorage.setItem(publicationKey, JSON.stringify(operation));
  pendingPublication = operation;
  showToolHistory(false);
  render();
  let uncertain = false;
  try {
    const saved = await workspaceApi(path, payload, publicationTimeout).catch(error => {
      if (error.code !== 'transport') throw error;
      uncertain = true;
      // A single replay can recover an acknowledgement without rebuilding the candidate.
      return workspaceApi(path, payload, publicationTimeout);
    });
    clearPublication();
    return saved;
  } catch (error) {
    if (!uncertain && error.code !== 'version_conflict') clearPublication();
    throw error;
  }
}

function clearPublication() {
  pendingPublication = null;
  sessionStorage.removeItem(publicationKey);
}

function pendingPublicationForSession() {
  try {
    const operation = JSON.parse(sessionStorage.getItem(publicationKey));
    return operation?.marker === session.marker && validToolId(operation.id) && validToolId(operation.runId)
      ? operation : null;
  } catch {
    return null;
  }
}

async function synchronizePublication() {
  const { id, runId } = pendingPublication;
  try {
    const current = await workspaceApi('/api/tools/' + id + '/resolve-publish', { runId }, publicationTimeout);
    if (active?.id === id && active.version === current.version && active.revision === current.revision) {
      resumeStorage(active);
    } else {
      await openTool(current, true);
    }
    clearPublication();
    if (current.applied) finishComposition();
    else if (editing) failure = '本次修改未发布，已同步当前工具。你可以重试。';
    else warnStorage('本次修改未发布，已恢复当前工具。');
  } catch {
    // Keep the preview and its writes paused until a later synchronization succeeds.
  }
}

async function openTool(tool, restore = false) {
  if (!validToolId(tool.id) || typeof tool.html !== 'string' || !/^<!doctype html>/i.test(tool.html)) {
    throw new Error('工具文件不完整，无法打开预览。');
  }
  const entries = tool.entries;
  const original = serializeEntries(entries);

  const iframe = document.createElement('iframe');
  iframe.title = '生成的工具';
  iframe.setAttribute('sandbox', 'allow-scripts allow-forms');
  iframe.referrerPolicy = 'no-referrer';
  iframe.tabIndex = -1;
  iframe.inert = true;
  const frame = {
    id: tool.id, token: crypto.randomUUID(), iframe, snapshot: original,
    revision: tool.revision, version: tool.version, saveTail: Promise.resolve(), saveFailed: false,
  };
  const old = active;
  candidate = frame;
  const ready = new Promise((resolve, reject) => Object.assign(frame, { resolve, reject }));
  const deadline = setTimeout(() => frame.reject(new Error('准备预览超时，需求已保留。')), 5000);
  const args = JSON.stringify([entries, frame.token, location.origin, maxStorageBytes]).replaceAll('<', '\\u003c');
  const bootstrap = '(' + initializeFrame.toString() + ')(...' + args + ')';
  iframe.srcdoc = '<!doctype html><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width, initial-scale=1">'
    + '<meta http-equiv="Content-Security-Policy" content="' + previewPolicy + '">'
    + '<script>' + bootstrap + '</script>' + tool.html.slice('<!doctype html>'.length);
  try {
    preview.append(iframe);
    await ready;
    let savedSnapshot = original;
    if (tool.runId) {
      const saved = await publishRevision(frame, old, tool);
      savedSnapshot = frame.snapshot;
      frame.revision = saved.revision;
    } else if (!restore) {
      savedSnapshot = frame.snapshot;
      const saved = await workspaceApi('/api/tools/' + frame.id + '/commit', {
        entries: JSON.parse(savedSnapshot),
      });
      frame.revision = saved.revision;
    } else {
      await workspaceApi('/api/tools/' + frame.id + '/open', { version: frame.version },
        pendingPublication ? publicationTimeout : undefined);
    }
    old?.iframe.remove();
    active = frame;
    iframe.dataset.tool = frame.id;
    iframe.removeAttribute('tabindex');
    resumeStorage(frame);
    warnStorage('');
    restored = restore;
    if (frame.snapshot !== savedSnapshot) enqueueSave(frame, frame.snapshot);
    if (old?.id !== frame.id) showToolHistory(false);
    else if (!toolHistory.hidden) loadToolHistory();
  } finally {
    clearTimeout(deadline);
    if (active !== frame) {
      iframe.remove();
      if (tool.runId && !pendingPublication && old.iframe.inert && old.iframe.isConnected) resumeStorage(old);
    }
    candidate = null;
  }
}

async function openRevision(tool) {
  try {
    await openTool(tool);
  } catch (error) {
    if (pendingPublication || error.code !== 'revision_conflict') throw error;
    const current = await workspaceApi('/api/tools/' + tool.id);
    if (current.version !== tool.version - 1) throw new Error('工具已更新，请重新打开后再修改。');
    await openTool({ ...tool, entries: current.entries, revision: current.revision });
  }
}

requirement.addEventListener('input', render);
example.addEventListener('click', () => {
  requirement.value = '做一个读书清单，可以输入并添加书名。刷新后保留已添加的书。';
  requirement.focus();
  render();
});
function finishComposition() {
  editing = false;
  entryMode = 'direct';
  requirement.value = '';
  compose(false);
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (generate.disabled) return;
  const guidedGeneration = guideStep === 1 && entryMode === 'fast' && !editing;
  hideSample();
  compose(true);
  failure = '';
  phase = 'model';
  render();
  try {
    const path = editing ? '/api/tools/' + active.id + '/revise' : '/api/generate';
    const tool = await workspaceApi(path, {
      requirement: requirement.value.trim(), version: editing ? active.version : undefined,
    });
    phase = 'preview';
    render();
    if (editing) await openRevision(tool);
    else await openTool(tool);
    if (guidedGeneration && guideStep === 1) {
      guideTool = tool.id;
      guideStep = 2;
    }
    finishComposition();
    await loadLibrary().catch(error => warnStorage(error.message));
  } catch (error) {
    if (pendingPublication) await synchronizePublication();
    else failure = error.message;
  } finally {
    phase = 'idle';
    render();
  }
});

byId('sync-tool').addEventListener('click', async () => {
  if (!pendingPublication || phase !== 'idle') return;
  phase = 'sync';
  render();
  await synchronizePublication();
  phase = 'idle';
  render();
});

async function selectTool(id) {
  if (!networkIsolated || pendingPublication || phase !== 'idle' || !validToolId(id)) return;
  hideSample();
  phase = 'restore';
  failure = '';
  render();
  try {
    await active?.saveTail;
    await openTool(await workspaceApi('/api/tools/' + id), true);
    if (editing) requirement.value = '';
    editing = false;
    entryMode = 'direct';
    compose(false);
    if (guideStep === 3 && guideTool === id) dismissGuide();
  } catch (error) {
    warnStorage(error.message);
  } finally {
    phase = 'idle';
    render();
  }
}

function beginComposition(revise, value = '') {
  hideSample();
  editing = revise;
  entryMode = 'direct';
  compose(true);
  requirement.value = value;
  failure = '';
  requirement.focus();
  render();
}
byId('new-tool').addEventListener('click', () => beginComposition(false));
byId('revise-tool').addEventListener('click', () => beginComposition(true));

function authMode(next) {
  registering = next;
  byId('auth-title').textContent = next ? '邀请注册' : '登录';
  byId('auth-description').textContent = next ? '凭邀请码创建你的工作台账号。' : '登录后打开你的工具和记录。';
  byId('auth-submit').textContent = next ? '创建账号' : '登录';
  byId('auth-mode').textContent = next ? '已有账号？去登录' : '凭邀请码注册';
  byId('invite-field').hidden = !next;
  byId('invite').required = next;
  byId('username-hint').hidden = byId('password-hint').hidden = !next;
  byId('password').autocomplete = next ? 'new-password' : 'current-password';
  clearAuthErrors();
}

const authFields = ['invite', 'username', 'password'];

function setAuthError(field, message = '') {
  const input = byId(field);
  const feedback = byId(field + '-error');
  if (message) input.setAttribute('aria-invalid', 'true');
  else input.removeAttribute('aria-invalid');
  feedback.textContent = message;
  feedback.hidden = !message;
}

function clearAuthErrors() {
  for (const field of authFields) setAuthError(field);
  byId('auth-status').textContent = '';
  byId('auth-status').classList.remove('elys-error');
}

for (const field of authFields) {
  byId(field).addEventListener('input', () => setAuthError(field));
}

byId('auth-mode').addEventListener('click', () => authMode(!registering));
byId('auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = byId('auth-submit');
  if (submit.disabled) return;
  const isRegistration = registering;
  submit.disabled = byId('auth-mode').disabled = true;
  clearAuthErrors();
  const password = byId('password');
  try {
    await api(isRegistration ? '/api/register' : '/api/login', post({
      username: byId('username').value, password: password.value,
      invite: isRegistration ? byId('invite').value : undefined,
    }));
    password.value = '';
    if (isRegistration) {
      byId('invite').value = '';
      authMode(false);
      byId('auth-status').textContent = '注册成功，请登录。';
      password.focus();
    } else {
      location.reload();
    }
  } catch (error) {
    const field = {
      invalid_invite: 'invite',
      invalid_username: 'username',
      username_taken: 'username',
      invalid_password: 'password',
    }[error.code];
    if (field) {
      setAuthError(field, error.message);
      byId(field).focus();
    } else {
      byId('auth-status').classList.add('elys-error');
      byId('auth-status').textContent = error.message;
    }
  } finally {
    submit.disabled = byId('auth-mode').disabled = false;
  }
});

byId('logout').addEventListener('click', async () => {
  const button = byId('logout');
  button.disabled = true;
  byId('workspace').inert = true;
  try {
    await active?.saveTail;
    await workspaceApi('/api/logout', {});
    preview.replaceChildren();
    location.reload();
  } catch (error) {
    warnStorage(error.message);
    byId('workspace').inert = false;
    button.disabled = false;
  }
});

window.addEventListener('focus', async () => {
  if (!session) return;
  try {
    if ((await api('/api/session')).marker !== session.marker) {
      byId('workspace').hidden = true;
      preview.replaceChildren();
      location.reload();
    }
  } catch (error) {
    warnStorage(error.message);
  }
});

async function start() {
  try {
    compose(true);
    renderEntryTemplate(templateKey);
    loadReleases();
    renderRoute();
    const identity = await api('/api/session');
    identityResolved = true;
    session = identity.user ? identity : null;
    renderRoute();
    if (!session) return;
    byId('account-name').textContent = session.user;
    restoreGuide();
    ({ configured } = await api('/api/config'));
    networkIsolated = await confirmsNetworkPolicy();
    if (!networkIsolated) {
      byId('config').textContent = '当前浏览器未启用预览网络隔离。请使用支持 Connection-Allowlist 的 Chrome。';
      byId('config').hidden = false;
    } else if (!configured) {
      byId('config').textContent = '本机服务尚未配置 DeepSeek Key。';
      byId('config').hidden = false;
    }
    pendingPublication = networkIsolated ? pendingPublicationForSession() : null;
    const latest = await loadLibrary();
    if (networkIsolated) {
      if (pendingPublication) {
        phase = 'sync';
        render();
        await synchronizePublication();
      } else if (latest) {
        phase = 'idle';
        await selectTool(latest);
      }
    }
  } catch (error) {
    if (!session) {
      identityResolved = true;
      renderRoute();
      byId('auth-status').textContent = error.message;
    } else {
      warnStorage(error.message);
    }
  } finally {
    phase = 'idle';
    render();
    document.documentElement.dataset.ready = 'true';
  }
}
start();
