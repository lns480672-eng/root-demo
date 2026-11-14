'use strict';

const get = id => document.getElementById(id);
const backend = get('backend');
const retryLoad = get('retry-load');
const retryAction = get('retry-action');
const toggle = get('toggle');
const inviteGenerate = get('invite-generate');
const inviteField = get('issued-code');
const confirmAction = get('confirm');
const auditClock = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
const percentage = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 });
let identity = null;
let state = null;
let busy = false;
let pending = null;
let pendingInvite = null;
let issuingInvite = false;
const generationSettingsPath = '/api/admin/generation-settings';
const generationSettingsForm = get('generation-settings-form');
const generationSettingKeys = ['sourceBytes', 'revisionContextBytes', 'outputTokens', 'timeoutSeconds'];
const generationSettingsFields = get('generation-settings-fields');
const generationSettingsRetry = get('generation-settings-retry');
const generationSettingsFeedback = get('generation-settings-feedback');
let generationSettings = null;
let generationSettingsBusy = false;

function element(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}

function pendingKey() {
  return 'root:admin-operation:' + identity.user;
}

function remember(operation) {
  pending = operation;
  try {
    if (pending) sessionStorage.setItem(pendingKey(), JSON.stringify(pending));
    else sessionStorage.removeItem(pendingKey());
  } catch {
    // Keep the pending action in memory if session storage is disabled.
  }
}

async function request(path, data) {
  const headers = { 'X-Root-Session': identity?.marker || '' };
  const options = { headers, signal: AbortSignal.timeout(10000) };
  if (data) {
    headers['Content-Type'] = 'application/json';
    options.method = 'POST';
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.message || '请求未完成。');
    error.code = payload.code;
    error.authorizationChanged = ['login_required', 'session_changed', 'admin_required'].includes(payload.code);
    error.knownRejection = !error.authorizationChanged && response.status >= 400 && response.status < 500;
    throw error;
  }
  return payload;
}

function showSection() {
  const locked = location.hash === '#accounts';
  const invites = location.hash === '#invites';
  const security = location.hash === '#security';
  const settings = location.hash === '#generation-settings';
  get('admin').hidden = locked || invites || security || settings;
  get('generation-settings-module').hidden = !settings;
  get('generation-settings-nav').setAttribute('aria-current', settings ? 'page' : 'false');
  if (settings && identity && !generationSettings && !generationSettingsBusy) loadGenerationSettings();
  get('account-preview').hidden = !locked;
  get('invite-module').hidden = !invites;
  get('security-module').hidden = !security;
  if (!security) get('password-form').reset();
  get('trial-nav').setAttribute('aria-current', locked || invites || security || settings ? 'false' : 'page');
  get('account-nav').setAttribute('aria-current', locked ? 'page' : 'false');
  get('invite-nav').setAttribute('aria-current', invites ? 'page' : 'false');
  get('security-nav').setAttribute('aria-current', security ? 'page' : 'false');
}

function showGenerationSettingsFeedback(text, error = false) {
  generationSettingsFeedback.textContent = text;
  if (error) generationSettingsFeedback.dataset.error = 'true';
  else delete generationSettingsFeedback.dataset.error;
}

function renderGenerationSettings(payload) {
  for (const key of generationSettingKeys) {
    const field = generationSettingsForm.elements.namedItem(key);
    field.value = payload.settings[key];
    field.max = payload.maximums[key];
    field.removeAttribute('aria-invalid');
    get('limit-' + key).textContent = '默认 ' + payload.defaults[key].toLocaleString('zh-CN') +
      '，最高 ' + payload.maximums[key].toLocaleString('zh-CN');
  }
  generationSettings = payload;
}

async function loadGenerationSettings(data) {
  if (generationSettingsBusy) return;
  generationSettingsBusy = true;
  generationSettingsFields.disabled = generationSettingsRetry.disabled = true;
  generationSettingsRetry.hidden = true;
  showGenerationSettingsFeedback(data ? '正在保存配置…' : '正在读取配置…');
  try {
    renderGenerationSettings(await request(generationSettingsPath, data));
    showGenerationSettingsFeedback(data?.reset ? '已恢复默认值。' : data ? '配置已保存。' : '');
  } catch (error) {
    if (error.authorizationChanged) {
      location.reload();
      return;
    }
    if (!data || !error.knownRejection) generationSettings = null;
    generationSettingsRetry.hidden = Boolean(generationSettings);
    showGenerationSettingsFeedback(data && !error.knownRejection
      ? '暂时无法确认保存结果，请重新读取配置后核对。' : error.message, true);
  } finally {
    generationSettingsBusy = false;
    generationSettingsRetry.disabled = false;
    generationSettingsFields.disabled = !generationSettings;
  }
}

generationSettingsForm.addEventListener('input', event => {
  event.target.removeAttribute('aria-invalid');
});
generationSettingsForm.addEventListener('submit', event => {
  event.preventDefault();
  if (!generationSettings || generationSettingsBusy) return;
  const settings = {};
  for (const key of generationSettingKeys) {
    const field = generationSettingsForm.elements.namedItem(key);
    field.removeAttribute('aria-invalid');
    const value = field.valueAsNumber;
    if (!Number.isSafeInteger(value) || value < 1 || value > generationSettings.maximums[key]) {
      showGenerationSettingsFeedback('请输入 1 到 ' + generationSettings.maximums[key].toLocaleString('zh-CN') + ' 之间的整数。', true);
      field.setAttribute('aria-invalid', 'true');
      field.focus();
      return;
    }
    settings[key] = value;
  }
  if (settings.revisionContextBytes < settings.sourceBytes) {
    const context = generationSettingsForm.elements.namedItem('revisionContextBytes');
    showGenerationSettingsFeedback('续改上下文上限须大于或等于源码大小上限。', true);
    context.setAttribute('aria-invalid', 'true');
    context.focus();
    return;
  }
  loadGenerationSettings({settings});
});
get('generation-settings-reset').addEventListener('click', () => loadGenerationSettings({reset:true}));
generationSettingsRetry.addEventListener('click', () => loadGenerationSettings());

get('password-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = get('password-submit');
  const feedback = get('password-feedback');
  if (submit.disabled) return;
  const newPassword = get('new-password').value;
  if (newPassword !== get('new-password-again').value) {
    feedback.dataset.error = 'true';
    feedback.textContent = '两次输入的新密码不一致。';
    get('new-password-again').focus();
    return;
  }
  submit.disabled = true;
  delete feedback.dataset.error;
  feedback.textContent = '正在保存新密码…';
  try {
    await request('/api/admin/password', { currentPassword: get('current-password').value, newPassword });
    event.target.reset();
    location.reload();
  } catch (error) {
    feedback.dataset.error = 'true';
    feedback.textContent = error.knownRejection ? error.message
      : '暂时无法确认修改结果。请重新打开后台，用新密码登录核对。';
  } finally {
    submit.disabled = false;
  }
});

function inviteKey() {
  return 'root:admin-invite-request:' + identity.user;
}

function rememberInvite(requestId) {
  pendingInvite = requestId;
  try {
    if (requestId) sessionStorage.setItem(inviteKey(), requestId);
    else sessionStorage.removeItem(inviteKey());
  } catch {
    // Keep the request identifier in memory if session storage is unavailable.
  }
}

function updateInviteButton() {
  inviteGenerate.disabled = issuingInvite;
  inviteGenerate.textContent = issuingInvite ? '正在发码…' : pendingInvite ? '确认上次发码' : '生成邀请码';
}

function clearInviteResult() {
  inviteField.value = '';
  get('invite-expires').textContent = '';
  get('invite-expires').removeAttribute('datetime');
  get('invite-result').hidden = true;
}

async function issueInvite() {
  if (issuingInvite) return;
  if (!pendingInvite) {
    clearInviteResult();
    rememberInvite(crypto.randomUUID());
  }
  issuingInvite = true;
  updateInviteButton();
  const feedback = get('invite-feedback');
  feedback.textContent = '';
  try {
    const issued = await request('/api/admin/invites', {requestId: pendingInvite});
    rememberInvite(null);
    inviteField.value = issued.code;
    const date = new Date(issued.expiresAt * 1000);
    const expires = get('invite-expires');
    expires.textContent = auditClock.format(date);
    expires.dateTime = date.toISOString();
    get('invite-result').hidden = false;
    feedback.textContent = '邀请码已生成。请与官网链接一起私发给受邀评审。';
    request('/api/trial/admin').then(updated => {
      state = updated;
      render();
    }).catch(() => {
      // The issue has committed. Leave its only visible code available if refreshing the audit fails.
    });
  } catch (error) {
    if (error.code === 'invite_already_issued') {
      rememberInvite(null);
      feedback.textContent = '本次邀请码已生成，原码无法恢复。若未收到，可重新生成一枚。';
    } else if (error.knownRejection) {
      rememberInvite(null);
      feedback.textContent = error.message;
    } else if (error.authorizationChanged) {
      feedback.textContent = '登录会话或权限已变化，请重新登录后确认上次发码。';
    } else {
      feedback.textContent = '暂时无法确认发码结果，请点击「确认上次发码」。';
    }
  } finally {
    issuingInvite = false;
    updateInviteButton();
  }
}

async function copyInvite() {
  const code = inviteField.value;
  if (!code) return;
  const feedback = get('invite-feedback');
  try {
    await navigator.clipboard.writeText(code);
    if (inviteField.value === code) feedback.textContent = '已复制邀请码。';
  } catch {
    if (inviteField.value !== code) return;
    inviteField.focus();
    inviteField.select();
    feedback.textContent = '剪贴板不可用，请手动复制已选中的邀请码。';
  }
}

function showUsage(kind, stats) {
  get(kind + '-total').replaceChildren(String(stats.used) + ' ', element('span', '/ ' + stats.limit + ' 次'));
  const progress = get(kind + '-progress');
  progress.max = stats.limit;
  progress.value = stats.used;
  const label = kind === 'generation' ? '生成' : '分析';
  progress.setAttribute('aria-label', '全站' + label + '调用已用 ' + stats.used + ' 次，共 ' + stats.limit + ' 次');
  get(kind + '-remaining').textContent = '试用期剩余 ' + stats.remaining + ' 次';
  const failure = get(kind + '-failure');
  if (stats.failureRate === null) {
    failure.replaceChildren(element('span', '调用失败率'), element('strong', '暂无已结束调用'));
  } else {
    failure.replaceChildren(
      element('span', '调用失败率'),
      element('strong', percentage.format(stats.failureRate)),
      element('span', stats.failed + ' 失败 / ' + stats.completed + ' 已结束'),
    );
  }
}

function accountRow(account) {
  const row = get('account-template').content.firstElementChild.cloneNode(true);
  const field = selector => row.querySelector(selector);
  const form = field('.top-up');
  const input = form.elements.amount;
  const open = field('.open-top-up');
  const error = field('.error');
  field('.account-name').textContent = account.username;
  field('.gen-used').textContent = account.generation.used;
  field('.gen-left').textContent = account.generation.remaining;
  field('.analysis-used').textContent = account.analysis.used;
  field('.analysis-left').textContent = account.analysis.remaining;

  function setOpen(value) {
    form.hidden = !value;
    open.setAttribute('aria-expanded', String(value));
    error.textContent = '';
    if (value) input.focus();
    else {
      form.reset();
      open.focus();
    }
  }

  open.addEventListener('click', () => setOpen(form.hidden));
  field('.cancel-top-up').addEventListener('click', () => setOpen(false));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const amount = Number(input.value);
    if (!/^[1-9][0-9]*$/.test(input.value.trim()) || !Number.isSafeInteger(amount)) {
      error.textContent = '请输入有效的正整数。';
      return;
    }
    error.textContent = '';
    await applyAction({ action: 'grant', username: account.username, amount }, error);
  });
  return row;
}

function auditRow(entry) {
  const date = new Date(entry.createdAt * 1000);
  const time = element('time', auditClock.format(date));
  time.dateTime = date.toISOString();
  const label = entry.action === 'grant' ? '为 ' + entry.username + ' 补发 ' + entry.amount + ' 次生成额度'
    : {pause: '暂停模型调用', resume: '恢复模型调用', issue_invite: '生成邀请码'}[entry.action];
  const action = element('span', label);
  action.className = 'audit-action';
  const row = document.createElement('li');
  row.append(time, element('strong', entry.actor), action);
  return row;
}

function updateControls() {
  const blocked = busy || Boolean(pending);
  toggle.disabled = blocked;
  retryAction.hidden = !pending;
  retryAction.disabled = busy;
  for (const control of backend.querySelectorAll('.open-top-up, .top-up [type="submit"]')) {
    control.disabled = blocked;
  }
}

function render() {
  showUsage('generation', state.generation);
  showUsage('analysis', state.analysis);
  const status = get('model-status');
  status.dataset.paused = String(state.paused);
  status.textContent = state.paused ? '模型调用已暂停' : '模型调用正常';
  toggle.textContent = state.paused ? '恢复模型调用' : '暂停模型调用';
  get('current-operator').textContent = '操作者 ' + identity.user;
  get('account-list').replaceChildren(...state.accounts.map(accountRow));
  get('audit-empty').hidden = Boolean(state.audit.length);
  get('audit-list').replaceChildren(...state.audit.map(auditRow));
  showSection();
  updateControls();
}

async function load(afterAction = false) {
  retryLoad.disabled = true;
  try {
    if (!identity) {
      const current = await request('/api/admin/session');
      identity = current;
      try {
        pending = JSON.parse(sessionStorage.getItem(pendingKey()));
      } catch {
        pending = null;
      }
      try {
        pendingInvite = sessionStorage.getItem(inviteKey());
      } catch {
        pendingInvite = null;
      }
      updateInviteButton();
      if (pendingInvite) get('invite-feedback').textContent = '上次发码结果未确认，请先核对。';
    }
    state = await request('/api/trial/admin');
    render();
    backend.hidden = false;
    get('admin-logout').disabled = false;
    get('load-feedback').hidden = true;
    if (pending) get('action-feedback').textContent = '上次操作的结果未确认，请核对。';
  } catch (error) {
    if (error.authorizationChanged) {
      try { sessionStorage.setItem('root:admin-expired', '1'); } catch {}
      location.reload();
      return;
    }
    backend.hidden = true;
    get('load-feedback').hidden = false;
    const prefix = afterAction ? '操作已保存，但额度与记录刷新失败。' : '用量与额度加载失败。';
    get('load-status').textContent = prefix + ' ' + error.message;
    retryLoad.hidden = false;
  } finally {
    retryLoad.disabled = false;
  }
}

async function applyAction(data, fieldError) {
  if (busy || (data && pending)) return;
  if (data) remember({ ...data, requestId: crypto.randomUUID() });
  if (!pending) return;
  busy = true;
  updateControls();
  const feedback = get('action-feedback');
  feedback.textContent = '';
  try {
    const result = await request('/api/trial/admin', pending);
    remember(null);
    feedback.textContent = result.action === 'grant' ? '已为 ' + result.username + ' 补发 ' + result.amount + ' 次生成额度。'
      : result.paused ? '已暂停模型调用。' : '已恢复模型调用。';
    await load(true);
  } catch (error) {
    if (error.knownRejection) {
      remember(null);
      (fieldError || feedback).textContent = error.message;
    } else if (error.authorizationChanged) {
      feedback.textContent = '登录会话或权限已变化。请重新打开后台登录，再确认上次操作。';
    } else {
      feedback.textContent = '暂时无法确认操作结果，请点击「确认上次操作」。';
    }
  } finally {
    busy = false;
    updateControls();
  }
}

toggle.addEventListener('click', () => {
  const title = state.paused ? '恢复模型调用' : '暂停模型调用';
  get('confirm-title').textContent = title;
  get('confirm-action').textContent = title;
  get('confirm-copy').textContent = state.paused ? '恢复后，符合额度条件的用户可重新发起生成和分析。'
    : '暂停同时影响新的生成和分析。已有工具及草稿仍可保存。';
  confirmAction.returnValue = '';
  confirmAction.showModal();
});
confirmAction.addEventListener('close', () => {
  if (confirmAction.returnValue === 'apply') applyAction({ action: state.paused ? 'resume' : 'pause' });
});
get('admin-logout').addEventListener('click', async () => {
  const button = get('admin-logout');
  button.disabled = true;
  try {
    await request('/api/admin/logout', {});
    location.reload();
  } catch (error) {
    if (error.authorizationChanged) {
      location.reload();
      return;
    }
    get('action-feedback').textContent = '暂时无法确认退出结果，请重试。';
    button.disabled = false;
  }
});

retryLoad.addEventListener('click', () => load());
retryAction.addEventListener('click', () => applyAction());
inviteGenerate.addEventListener('click', issueInvite);
get('invite-copy').addEventListener('click', copyInvite);
window.addEventListener('hashchange', showSection);
window.addEventListener('pagehide', () => {
  clearInviteResult();
  get('password-form').reset();
});
load();
