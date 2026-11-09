'use strict';

const byId = id => document.getElementById(id);
const product = document.querySelector('.elys-product');
const form = byId('composer');
const requirement = byId('requirement');
const generate = byId('generate');
const example = byId('example');
const preview = byId('preview');
const maxStorageBytes = 256 * 1024;
const validToolId = id => typeof id === 'string' && /^run-[a-z0-9_]{8}$/.test(id);
const previewPolicy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
  + "img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
let session = null;
let registering = false;
let configured = false;
let networkIsolated = false;
let active = null;
let candidate = null;
let phase = 'startup';
let failure = '';
let restored = false;

function render() {
  const pending = ['model', 'preview', 'restore'].includes(phase);
  product.dataset.state = pending ? 'loading' : failure ? 'failure' : active ? 'success' : 'initial';
  generate.textContent = pending ? '生成中…' : failure ? '重试' : '生成工具';
  generate.disabled = !session || !configured || !networkIsolated || phase === 'startup' || pending || !requirement.value.trim();
  requirement.readOnly = pending;
  byId('new-tool').disabled = pending;
  for (const button of byId('tool-list').querySelectorAll('button')) {
    button.disabled = pending || !networkIsolated;
    button.setAttribute('aria-current', String(button.dataset.id === active?.id));
  }
  example.hidden = pending || Boolean(failure);
  byId('error').textContent = failure;
  byId('error').hidden = !failure;
  byId('loading').hidden = !pending;
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
  status.toggleAttribute('data-usable', Boolean(active));
  status.textContent = active
    ? pending || failure ? '上次工具仍可用' : restored ? '已恢复' : '已就绪'
    : pending ? '等待预览' : '待生成';
  byId('empty').hidden = Boolean(active);
  byId('empty-title').textContent = pending ? '等待预览' : '还没有生成工具';
  byId('empty-copy').textContent = pending ? '工具准备好后，会在这里显示。' : '输入需求后，工具会在这里出现。';
}

async function api(path, options) {
  let response;
  let data;
  try {
    response = await fetch(path, options);
    data = await response.json();
  } catch {
    throw new Error('无法连接本机服务，请检查工作台是否正在运行。');
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

async function workspaceApi(path, data) {
  const options = data === undefined ? { headers: {} } : post(data);
  options.headers['X-Root-Session'] = session.marker;
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
  const report = (type, extra = {}) => send({ token, type, ...extra }, parentOrigin);
  function commit(next) {
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
      getItem(key) { return values.get(String(key)) ?? null; },
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
        revision: frame.revision, entries: JSON.parse(snapshot),
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
  }
});

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
    revision: tool.revision, saveTail: Promise.resolve(), saveFailed: false,
  };
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
    if (!restore) {
      savedSnapshot = frame.snapshot;
      const saved = await workspaceApi('/api/tools/' + frame.id + '/commit', {
        entries: JSON.parse(savedSnapshot),
      });
      frame.revision = saved.revision;
    } else {
      await workspaceApi('/api/tools/' + frame.id + '/open', {});
    }
    active?.iframe.remove();
    active = frame;
    iframe.dataset.tool = frame.id;
    iframe.removeAttribute('tabindex');
    iframe.inert = false;
    warnStorage('');
    restored = restore;
    if (frame.snapshot !== savedSnapshot) enqueueSave(frame, frame.snapshot);
  } finally {
    clearTimeout(deadline);
    if (active !== frame) iframe.remove();
    candidate = null;
  }
}

requirement.addEventListener('input', render);
example.addEventListener('click', () => {
  requirement.value = '做一个读书清单，可以输入并添加书名。刷新后保留已添加的书。';
  requirement.focus();
  render();
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (generate.disabled) return;
  failure = '';
  phase = 'model';
  render();
  try {
    const tool = await workspaceApi('/api/generate', { requirement: requirement.value.trim() });
    phase = 'preview';
    render();
    await openTool(tool);
    await loadLibrary().catch(error => warnStorage(error.message));
  } catch (error) {
    failure = error.message;
  } finally {
    phase = 'idle';
    render();
  }
});

async function selectTool(id) {
  if (!networkIsolated || phase !== 'idle' || !validToolId(id)) return;
  phase = 'restore';
  failure = '';
  render();
  try {
    await active?.saveTail;
    await openTool(await workspaceApi('/api/tools/' + id), true);
  } catch (error) {
    warnStorage(error.message);
  } finally {
    phase = 'idle';
    render();
  }
}

byId('new-tool').addEventListener('click', () => {
  requirement.value = '';
  failure = '';
  requirement.focus();
  render();
});

function authMode(next) {
  registering = next;
  byId('auth-title').textContent = next ? '凭邀请注册' : '登录工作台';
  byId('auth-submit').textContent = next ? '注册' : '登录';
  byId('auth-mode').textContent = next ? '已有账号，去登录' : '凭邀请码注册';
  byId('invite-field').hidden = !next;
  byId('password').autocomplete = next ? 'new-password' : 'current-password';
  byId('auth-status').textContent = '';
}

byId('auth-mode').addEventListener('click', () => authMode(!registering));
byId('auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = byId('auth-submit');
  if (submit.disabled) return;
  const isRegistration = registering;
  submit.disabled = byId('auth-mode').disabled = true;
  byId('auth-status').textContent = '';
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
    byId('auth-status').textContent = error.message;
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
    const identity = await api('/api/session');
    if (!identity.user) {
      byId('auth').hidden = false;
      return;
    }
    session = identity;
    byId('account-name').textContent = session.user;
    byId('account').hidden = byId('workspace').hidden = false;
    ({ configured } = await api('/api/config'));
    networkIsolated = await confirmsNetworkPolicy();
    if (!networkIsolated) {
      byId('config').textContent = '当前浏览器未启用预览网络隔离。请使用支持 Connection-Allowlist 的 Chrome。';
      byId('config').hidden = false;
    } else if (!configured) {
      byId('config').textContent = '本机服务尚未配置 DeepSeek Key。';
      byId('config').hidden = false;
    }
    const latest = await loadLibrary();
    if (networkIsolated && latest) {
      phase = 'idle';
      await selectTool(latest);
    }
  } catch (error) {
    if (!session) {
      byId('auth').hidden = false;
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
