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
const library = byId('saved-tools');
const previewSection = preview.closest('section');
const coachmark = byId('entry-coachmark');
const primaryNavigation = ['new-tool', 'idea-entry', 'drafts-entry', 'library-entry'];
form.append(coachmark);
const toolHistory = byId('tool-version-panel');
const restoreDialog = byId('restore-version-dialog');
const maxStorageBytes = 256 * 1024;
const tableStorageBytes = 8 * 1024 * 1024;
const publicationKey = 'root:pending-publication';
const publicationTimeout = 6000;
const runtimeVerificationTimeout = 20000;
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
let assistantDestination = 'idea-entry';
let trial = null;
let trialRequest = 0;
const validStagedId = id => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id);
const stagedStepLabels = {
  'revision-plan': '理解修改', spec: '理解需求', implementation: '生成工具', entry: '设计录入和明细',
  filters: '设计筛选', summary: '设计统计', export: '设计导出', assemble: '组装工具',
};
const stagedStates = {
  queued: '等待生成', running: '正在生成工具', failed: '阶段未完成', interrupted: '生成已中断',
  complete: '等待检查预览', published: '已保存', expired: '已过期', exhausted: '已达调用上限',
  clarifying: '等待补充说明', unsupported: '暂不支持此需求', cancelled: '已取消修改',
};
const generationRestrictions = new Map([
  ['generation_total_limit', {
    label: '全站共享的生成额度已用完',
    message: '所有账号共用的生成额度已用完。请联系管理员补充全站额度后再试。',
  }],
  ['generation_account_limit', {
    label: '个人生成额度已用完',
    message: '你的个人生成额度已用完。请联系管理员补充个人额度后再试。',
  }],
  ['model_paused', {
    label: '全站生成已暂停',
    message: '管理员已暂停模型调用。恢复后可以继续生成。',
  }],
]);
let stagedEnabled = false;
let stagedTasks = [];
let stagedHistory = [];
let stagedHistoryRead = 0;
let stagedHistoryStatus = '';
let stagedHistoryFailed = false;
let selectedStagedId = null;
let stagedAutoId = null;
let stagedRead = 0;
let stagedTimer = null;
let stagedMessage = '';
let stagedActivity = '';
let stagedLoadError = '';
let stagedPendingId = null;
let stagedPendingNonce = null;
let stagedSubmission = null;
let pendingRevision = null;

function currentGenerationRestriction() {
  if (!trial) return;
  if (trial.paused) return generationRestrictions.get('model_paused');
  if (!trial.generation.totalRemaining) return generationRestrictions.get('generation_total_limit');
  if (!trial.generation.remaining) return generationRestrictions.get('generation_account_limit');
}

function generationError(error) {
  return generationRestrictions.get(error.code)?.message
    || error.message + (error.issue ? ' 问题编号 ' + error.issue + '。' : '');
}

function render() {
  const starting = phase === 'startup';
  const pending = ['model', 'preview', 'restore', 'sync', 'table'].includes(phase);
  const syncing = Boolean(pendingPublication);
  const pausingEdits = syncing && Boolean(pendingPublication.runId);
  const stagedEditsPaused = Boolean(stagedPendingId && active?.iframe.inert);
  const blocked = pending || syncing || Boolean(stagedPendingId);
  const stagedTask = selectedStagedTask();
  const generationCategory = byId('generation-category');
  const returning = !editing && stagedTask?.requirement === requirement.value.trim() &&
    isActiveStagedTask(stagedTask) && (stagedTask.category || 'auto') === generationCategory.value;
  const tableMode = entryMode === 'table';
  const showingSample = Boolean(sampleTemplate);
  const showingTool = Boolean(active) && !showingSample;
  product.dataset.state = pending ? 'loading' : failure ? 'failure' : active ? 'success' : 'initial';
  const atStart = workspace.dataset.mode === 'start';
  generate.textContent = pending ? '生成中…' : failure ? '重试' : editing && stagedEnabled ? '查看修改方案' : editing ? '生成新版' : atStart ? '开始' : '直接生成';
  generate.disabled = !session || !configured || !networkIsolated || starting || blocked ||
    tableMode || returning || Boolean(pendingRevision) || !requirement.value.trim();
  requirement.readOnly = blocked;
  byId('generation-scope').hidden = !stagedEnabled || editing || tableMode;
  generationCategory.disabled = blocked;
  const assistantOpen = !byId('assistant-panel').hidden;
  const browsing = !assistantOpen && (workspace.dataset.mode === 'browse' || editing);
  library.hidden = !browsing;
  byId('library-entry').setAttribute('aria-expanded', String(browsing));
  markNavigation(assistantOpen ? assistantDestination : browsing ? 'library-entry' : 'new-tool');
  for (const id of primaryNavigation) byId(id).disabled = blocked || (starting && id === 'library-entry');
  byId('new-tool-options').hidden = !atStart && !['fast', 'table'].includes(entryMode);
  previewSection.hidden = !browsing && !editing && !showingSample && !syncing && !stagedPendingId;
  const returnToLibrary = byId('return-to-library');
  returnToLibrary.hidden = !active;
  returnToLibrary.disabled = blocked;
  let currentTitle = '工具预览';
  for (const button of byId('tool-list').querySelectorAll('button')) {
    button.disabled = blocked || starting || !networkIsolated;
    const selected = button.dataset.id === active?.id;
    button.setAttribute('aria-current', String(selected));
    if (selected) {
      currentTitle = button.dataset.title;
      labelTool(button, active.version);
    }
  }
  byId('preview-title').textContent = showingSample ? quickTemplates[sampleTemplate].title : currentTitle;
  byId('composer-title').textContent = editing ? '要怎样修改这个工具？' :
    atStart || entryMode !== 'direct' ? '你想做什么工具？' : '生成工具';
  byId('requirement-label').textContent = editing ? '修改「' + currentTitle + '」' : '工具需求';
  requirement.placeholder = editing ? '例如：增加一个按负责人筛选。'
    : '描述你想做的工具、怎么用、需要保存哪些数据。';
  byId('revise-tool').hidden = byId('tool-history').hidden = byId('publish-lock').hidden = syncing || !showingTool;
  byId('revise-tool').disabled = blocked || !configured;
  byId('tool-history').textContent = active ? '工具 v' + active.version + ' · 版本历史' : '版本历史';
  const sync = byId('sync-tool');
  sync.hidden = !syncing;
  sync.disabled = pending;
  const publicationMessage = byId('publication-message');
  publicationMessage.hidden = !syncing;
  publicationMessage.textContent = pausingEdits
    ? pending ? '正在核对已发布版本，编辑暂时暂停。'
      : '暂时无法确认发布结果。编辑已暂停，请点击「同步工具」后继续。'
    : pending ? '正在核对新工具是否已保存。' : '暂时无法确认新工具是否已保存，请先点击「同步工具」。';
  example.hidden = editing || blocked || Boolean(failure);
  byId('error').textContent = failure;
  byId('error').hidden = !failure;
  byId('admin-entry').hidden = !session || !trial?.admin;
  const balance = byId('generation-balance');
  const restriction = currentGenerationRestriction();
  balance.hidden = tableMode || !trial;
  if (trial) {
    const usage = trial.generation;
    balance.textContent = '个人生成额度剩余 ' + usage.remaining + ' / ' + usage.limit + ' 次';
  }
  const generationBlock = byId('generation-block');
  generationBlock.hidden = tableMode || !restriction;
  generationBlock.textContent = restriction?.label || '';
  byId('generation-retry-note').hidden = tableMode || !failure;
  byId('loading').hidden = syncing || !pending || Boolean(stagedActivity);
  byId('phase').textContent = {
    model: '正在等待 DeepSeek 响应',
    preview: '正在准备预览',
    restore: '正在恢复上次的工具',
    table: '正在准备表格工具',
  }[phase] || '';
  byId('model-step').hidden = tableMode;
  for (const step of ['model', 'preview']) {
    const item = byId(step + '-step');
    if (step === phase || (step === 'preview' && phase === 'table')) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
  }
  const status = byId('preview-status');
  status.toggleAttribute('data-usable', showingTool && !pausingEdits && !stagedEditsPaused);
  status.textContent = stagedEditsPaused ? '待核对保存 · 暂停编辑'
    : pausingEdits ? '待同步 · 暂停编辑' : showingSample ? '样例试用 · 不保存' : active
    ? pending || failure || syncing ? '上次工具仍可用' : tableMode ? '上次工具 · 可用' : restored ? '已恢复' : '已就绪'
    : syncing ? '新工具待同步' : pending ? '等待预览' : '待生成';
  byId('empty').hidden = showingTool || showingSample || syncing;
  byId('empty-title').textContent = pending ? '等待预览' : '还没有生成工具';
  byId('empty-copy').textContent = pending ? '工具准备好后，会在这里显示。' : '输入需求后，工具会在这里出现。';
  const fast = entryMode === 'fast';
  byId('quick-panel').hidden = !fast;
  byId('table-panel').hidden = !tableMode;
  byId('table-panel').inert = syncing;
  form.hidden = tableMode || (fast && !failure);
  byId('quick-entry').setAttribute('aria-pressed', String(fast));
  byId('direct-entry').setAttribute('aria-pressed', String(entryMode === 'direct' && !editing));
  byId('table-entry').setAttribute('aria-pressed', String(tableMode));
  for (const button of [byId('quick-entry'), byId('direct-entry'), byId('table-entry'), byId('assistant-entry'), byId('template-sample'),
    byId('template-customize'), ...document.querySelectorAll('[data-entry-template]')]) {
    button.disabled = blocked;
  }
  byId('template-generate').disabled = blocked || !configured || !networkIsolated;
  byId('guide-start').disabled = blocked || editing;
  renderStagedTasks(blocked, restriction);
}

async function api(path, options) {
  let response;
  let data;
  try {
    response = await fetch(path, options);
    data = await response.json();
    if (stagedEnabled && options?.signal && response.status >= 500 && !data?.code) {
      throw new Error('Invalid workbench error response');
    }
  } catch {
    const error = new Error('无法连接本机服务，请检查工作台是否正在运行。');
    error.code = 'transport';
    throw error;
  }
  if (!response.ok) {
    const error = new Error(data.message || '请求未完成，请重试。');
    error.code = data.code;
    error.issue = data.issue;
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
  } finally {
    if (path === '/api/assistant/analyze' || path === '/api/assistant/claim-draft') refreshTrial();
  }
}

function stagedPendingKey() {
  return 'root:pending-staged:' + session.user;
}

function rememberStagedPublication(id, nonce = null) {
  stagedPendingId = id;
  stagedPendingNonce = nonce;
  try {
    if (id) sessionStorage.setItem(stagedPendingKey(), nonce ? id + ':' + nonce : id);
    else sessionStorage.removeItem(stagedPendingKey());
  } catch {
    // The server's task list can still recover the candidate without local storage.
  }
}

function restoredStagedPublication() {
  try {
    const [id, nonce] = (sessionStorage.getItem(stagedPendingKey()) || '').split(':');
    return validStagedId(id) && (!nonce || validStagedId(nonce)) ? { id, nonce } : null;
  } catch {
    return null;
  }
}

function selectedStagedTask() {
  return stagedTasks.find(task => task.id === selectedStagedId);
}

function isActiveStagedTask(task) {
  return ['queued', 'running', 'interrupted', 'complete', 'clarifying'].includes(task?.state);
}

function clearStagedSelection() {
  selectedStagedId = stagedAutoId = null;
  pendingRevision = null;
  stagedMessage = '';
}

function rememberStagedTask(task) {
  stagedRead += 1;
  const other = stagedTasks.filter(item => item.id !== task.id);
  stagedTasks = task.state === 'published' ? other : [task, ...other];
}

function stagedStatus(task, restriction) {
  if (!task) return '';
  if (task.reason?.endsWith('_clarification_required')) return '需要明确修改目标';
  if (task.resumable) {
    if (restriction) return restriction.label;
    const previous = generationRestrictions.get(task.reason);
    if (previous) return trial ? '上次生成被限制' : previous.label;
  }
  return stagedStates[task.state] || '';
}

function stagedReason(task, restriction) {
  if (!task) return '';
  if (task.resumable && restriction) return restriction.message;
  const previous = generationRestrictions.get(task.reason);
  if (previous) return trial && task.resumable
    ? '上次生成时，' + previous.label + '。可以重新提交需求。' : previous.message;
  if (task.state === 'expired') return '任务已过期。可用保留的原需求重新生成。';
  if (task.state === 'exhausted') return '已达到此任务的调用上限。可用保留的原需求重新生成。';
  if (task.state === 'clarifying') return '';
  if (task.guidance) return task.guidance;
  if (task.reason === 'revision_risky') return '此版本暂不支持删除、转换或批量改写已有数据。旧版仍可用。';
  if (task.reason === 'revision_clarification_required') return '你想改整体背景、某个分栏的颜色，还是字段显示名？请写明修改对象，再查看方案。';
  if (task.reason === 'revision_no_change') return '这次修改没有产生可应用的变化。旧版仍可用，未扣除个人生成次数。';
  if (task.state === 'unsupported') return task.baseToolId
    ? '这项修改暂不支持。旧版仍可用，未扣除个人生成次数。'
    : '此需求含当前工作台暂不支持的功能。可编辑原需求后重新生成。';
  if (task.state === 'interrupted') return '执行中断。可从首个未完成阶段继续。';
  return {
    staged_busy: '模型正在处理其他请求，请稍后继续。',
    staged_timeout: '本阶段模型请求超时。',
    staged_balance: '模型服务余额不足，请联系管理员。',
    staged_rate_limit: '模型请求过多，请稍后继续。',
    invalid_staged_result: '本阶段的模型结果未通过校验。',
    staged_runtime_failed: '这次生成的网站标签无法切换。可用保留的需求重新生成。',
    staged_storage: '本阶段的结果未能保存。',
  }[task.reason] || (task.state === 'failed' ? '本阶段未完成，可编辑原需求后重新提交。' : '');
}

function renderStagedHistory(blocked) {
  const list = byId('staged-history-list');
  byId('staged-history-status').textContent = stagedHistoryStatus;
  byId('staged-history-refresh').hidden = !stagedHistoryFailed;
  const signature = JSON.stringify(stagedHistory);
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    list.replaceChildren(...stagedHistory.map(task => {
      const row = document.createElement('li');
      const title = document.createElement('strong');
      const original = document.createElement('p');
      const reason = document.createElement('p');
      const button = document.createElement('button');
      title.textContent = stagedStatus(task);
      original.textContent = task.requirement;
      reason.className = 'auth-muted';
      reason.textContent = stagedReason(task) + (task.issue && !generationRestrictions.has(task.reason)
        ? ' 问题编号 ' + task.issue + '。' : '');
      button.type = 'button';
      button.dataset.historyTaskId = task.id;
      button.textContent = '带回需求';
      row.append(title, original, reason, button);
      return row;
    }));
  }
  for (const button of list.querySelectorAll('button')) button.disabled = blocked;
}

const revisionFieldTypes = {date:'日期（年月日）', integer:'整数', text:'文本'};

function setRevisionOptions(select, options, selected = '') {
  select.replaceChildren(new Option('请选择', ''), ...options.map(([value, label]) => new Option(label, value)));
  select.value = selected;
}

function renderRevisionQuestion(task, canAct) {
  const panel = byId('revision-clarification');
  const detail = task?.state === 'clarifying' ? task.clarification : null;
  panel.hidden = !detail;
  if (!detail) return;
  if (panel.dataset.question !== detail.id) {
    panel.dataset.question = detail.id;
    byId('revision-question').textContent = detail.question || '已达到追问上限。请选择可用选项，或改写需求。';
    const history = byId('revision-answers');
    history.hidden = !detail.answers.length;
    history.querySelector('ol').replaceChildren(...detail.answers.map(item => {
      const row = document.createElement('li');
      row.textContent = item.question + ' ' + (revisionFieldTypes[item.answer] || item.answer);
      return row;
    }));
    byId('revision-answer').value = '';
    const options = detail.choices.length ? detail.choices.map(item => [item, item])
      : detail.field ? Object.entries(revisionFieldTypes) : [];
    setRevisionOptions(byId('revision-choice'), options);
    byId('revision-choice-label').hidden = !options.length;
    byId('revision-answer-label').hidden = detail.limited || Boolean(detail.choices.length);
  }
  byId('revision-answer-submit').disabled = !canAct ||
    (byId('revision-answer-label').hidden && byId('revision-choice-label').hidden);
  byId('revision-rewrite').disabled = !canAct;
  byId('revision-question-cancel').disabled = !canAct;
}

function renderStagedTasks(blocked, restriction) {
  const panel = byId('staged-panel');
  const task = selectedStagedTask();
  panel.hidden = !session || !stagedEnabled;
  if (panel.hidden) return;
  const canAct = phase === 'idle' && networkIsolated && !pendingPublication;
  const list = byId('staged-task-list');
  list.hidden = editing;
  byId('staged-list-status').hidden = editing && !stagedLoadError;
  const activeTasks = stagedTasks.filter(isActiveStagedTask);
  const signature = JSON.stringify(activeTasks.map(item => [item.id, item.requirement, stagedStatus(item, restriction)]));
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    const focusedId = document.activeElement?.dataset.taskId;
    list.replaceChildren(...activeTasks.map(item => {
      const row = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.taskId = item.id;
      button.textContent = (stagedStatus(item, restriction) || '待确认') + ' · ' + item.requirement.slice(0, 70);
      row.append(button);
      return row;
    }));
    if (focusedId && !blocked) {
      [...list.querySelectorAll('button')].find(button => button.dataset.taskId === focusedId)?.focus();
    }
  }
  for (const button of list.querySelectorAll('button')) {
    button.disabled = blocked;
    button.setAttribute('aria-current', String(button.dataset.taskId === selectedStagedId));
  }
  byId('staged-list-status').textContent = stagedLoadError ||
    (activeTasks.length || stagedPendingId ? '' : '暂无未完成任务。');
  const quotaBlocked = Boolean(restriction);
  byId('staged-refresh').hidden = !stagedLoadError && !(isActiveStagedTask(task) && task.resumable && quotaBlocked);
  byId('staged-refresh').disabled = phase !== 'idle';
  byId('staged-detail').hidden = !task && !stagedPendingId && !stagedActivity && !stagedMessage;
  const status = byId('staged-status');
  const statusText = stagedActivity || (stagedPendingId ? '保存结果待核对' : stagedStatus(task, restriction));
  if (status.textContent !== statusText) status.textContent = statusText;
  const steps = byId('staged-steps');
  const completed = task?.completed || [];
  const stage = task?.stage || (task?.state === 'queued' ? 'spec' : null);
  const plan = task ? (task.steps?.length ? task.steps : [...new Set([...completed, stage].filter(Boolean))]) : [];
  const progress = JSON.stringify([plan, completed, stage]);
  if (steps.dataset.progress !== progress) {
    steps.dataset.progress = progress;
    steps.replaceChildren(...plan.map((name, index) => {
      const step = document.createElement('li');
      const done = completed.includes(name);
      step.dataset.stage = name;
      step.toggleAttribute('data-completed', done);
      const label = Object.hasOwn(stagedStepLabels, name) ? stagedStepLabels[name] : '处理步骤 ' + (index + 1);
      step.textContent = label + (done ? '（已完成）' : '');
      if (name === stage) step.setAttribute('aria-current', 'step');
      return step;
    }));
  }
  const reason = stagedMessage || stagedReason(task, restriction);
  const reasonNode = byId('staged-reason');
  reasonNode.hidden = !reason;
  reasonNode.textContent = reason;
  const issue = byId('staged-issue');
  issue.hidden = !task?.issue || generationRestrictions.has(task.reason) || Boolean(task.resumable && restriction);
  issue.textContent = issue.hidden ? '' : '问题编号 ' + task.issue;
  const resume = byId('staged-resume');
  resume.hidden = !['queued', 'interrupted'].includes(task?.state) || Boolean(stagedPendingId);
  resume.disabled = !canAct || !configured || quotaBlocked;
  renderRevisionQuestion(task, canAct);
  const proposal = pendingRevision?.staged.taskId === task?.id ? pendingRevision?.revisionPlan : null;
  const confirmation = byId('revision-confirmation');
  confirmation.hidden = !proposal;
  if (proposal && confirmation.dataset.plan !== proposal.confirmation) {
    confirmation.dataset.plan = proposal.confirmation;
    byId('revision-field-label').hidden = !proposal.fieldType;
    if (proposal.fieldType) setRevisionOptions(byId('revision-field-type'),
      Object.entries(revisionFieldTypes), proposal.fieldType.type);
    byId('revision-plan-tool').textContent = '「' + proposal.title + '」v' + proposal.baseVersion;
    byId('revision-plan-changes').replaceChildren(...proposal.changes.map(change => {
      const item = document.createElement('li');
      item.textContent = change;
      return item;
    }));
  }
  byId('revision-field-type').disabled = !canAct;
  for (const id of ['revision-confirm', 'revision-adjust', 'revision-cancel']) byId(id).disabled = !canAct;
  const reopen = byId('staged-preview');
  reopen.hidden = task?.state !== 'complete' || Boolean(stagedPendingId) || Boolean(proposal);
  reopen.disabled = !canAct;
  reopen.textContent = task?.baseToolId ? '查看修改方案' : stagedMessage ? '重新预览' : '检查预览';
  const sync = byId('staged-sync');
  sync.hidden = !stagedPendingId;
  sync.disabled = !canAct;
  renderStagedHistory(blocked);
}

function scheduleStagedRefresh(delay = 850) {
  clearTimeout(stagedTimer);
  stagedTimer = setTimeout(refreshStagedTasks, delay);
}

async function refreshStagedTasks() {
  if (!stagedEnabled || !session) return;
  clearTimeout(stagedTimer);
  const request = ++stagedRead;
  const wasRunning = stagedTasks.some(task => ['queued', 'running'].includes(task.state));
  try {
    const result = await workspaceApi('/api/staged', undefined, publicationTimeout);
    if (request !== stagedRead) return;
    const selected = selectedStagedTask();
    const followed = stagedAutoId || (isActiveStagedTask(selected) ? selected.id : null);
    let tasks = result.tasks;
    // A task can leave the list on save or confirmed failure. Read its exact state.
    if (followed && !tasks.some(task => task.id === followed)) {
      const { task } = await workspaceApi('/api/staged/' + followed, undefined, publicationTimeout);
      if (request !== stagedRead) return;
      tasks = [task, ...tasks];
      if (!isActiveStagedTask(task) && task.state !== 'published' && stagedAutoId === followed) {
        stagedAutoId = null;
        if (byId('staged-history').open) refreshStagedHistory();
      }
    } else if (selected && !followed) {
      tasks = [selected, ...tasks];
    }
    stagedTasks = tasks;
    stagedLoadError = '';
  } catch (error) {
    if (request !== stagedRead) return;
    if (error.code === 'transport') {
      stagedLoadError = '暂时无法连接工作台，请重新读取任务。';
      render();
      return;
    }
    stagedLoadError = '暂时无法读取生成任务，请重试。';
  }
  const task = selectedStagedTask();
  const running = stagedTasks.some(item => ['queued', 'running'].includes(item.state));
  render();
  if (wasRunning && !running) refreshTrial();
  if (['complete', 'published'].includes(task?.state) && task.id === stagedAutoId && !stagedPendingId && !pendingPublication) {
    if (phase === 'idle') return openStagedCandidate(task);
    scheduleStagedRefresh();
  } else if (running || (stagedAutoId && stagedLoadError)) {
    scheduleStagedRefresh(stagedLoadError ? 3000 : undefined);
  }
}

async function restoreStagedRequirement(task, source) {
  if (!task || phase !== 'idle' || stagedPendingId || pendingPublication) return false;
  if (task.baseToolId && active?.id !== task.baseToolId) {
    await selectTool(task.baseToolId, source);
    if (active?.id !== task.baseToolId) {
      stagedHistoryStatus = '暂时无法打开原工具，请稍后重试。';
      render();
      return false;
    }
  }
  beginComposition(Boolean(task.baseToolId), task.requirement, task.category);
  return true;
}

byId('staged-task-list').addEventListener('click', async event => {
  const id = event.target.closest('button')?.dataset.taskId;
  const task = stagedTasks.find(item => item.id === id);
  if (!await restoreStagedRequirement(task, event.target)) return;
  rememberStagedTask(task);
  selectedStagedId = id;
  if (['queued', 'running'].includes(task.state)) stagedAutoId = id;
  render();
});

async function refreshStagedHistory() {
  if (!stagedEnabled || !session || !byId('staged-history').open) return;
  const request = ++stagedHistoryRead;
  stagedHistoryStatus = '正在读取生成历史…';
  stagedHistoryFailed = false;
  render();
  try {
    const result = await workspaceApi('/api/staged/history', undefined, publicationTimeout);
    if (request !== stagedHistoryRead) return;
    stagedHistory = result.tasks;
    stagedHistoryStatus = stagedHistory.length ? '' : '暂无失败或过期任务。';
  } catch {
    if (request !== stagedHistoryRead) return;
    stagedHistoryStatus = '暂时无法读取生成历史。';
    stagedHistoryFailed = true;
  }
  render();
}

byId('staged-history').addEventListener('toggle', refreshStagedHistory);
byId('staged-history-refresh').addEventListener('click', refreshStagedHistory);
byId('staged-history-list').addEventListener('click', async event => {
  const id = event.target.closest('button')?.dataset.historyTaskId;
  const task = stagedHistory.find(item => item.id === id);
  if (!await restoreStagedRequirement(task, event.target)) return;
  stagedSubmission = null;
  rememberStagedTask(task);
  selectedStagedId = task.id;
  byId('staged-history').open = false;
  render();
});

byId('staged-refresh').addEventListener('click', () => {
  refreshStagedTasks();
  refreshTrial();
});

byId('staged-resume').addEventListener('click', async () => {
  const task = selectedStagedTask();
  if (!['queued', 'interrupted'].includes(task?.state) || byId('staged-resume').disabled) return;
  stagedAutoId = task.id;
  stagedMessage = '';
  phase = 'model';
  stagedActivity = '正在继续生成';
  render();
  try {
    const result = await workspaceApi('/api/staged/' + task.id + '/resume', {}, publicationTimeout);
    rememberStagedTask(result.task);
  } catch (error) {
    stagedMessage = generationError(error);
  } finally {
    phase = 'idle';
    stagedActivity = '';
    render();
    refreshStagedTasks();
    refreshTrial();
  }
});

byId('staged-preview').addEventListener('click', () => {
  const task = selectedStagedTask();
  if (task?.state === 'complete') openStagedCandidate(task);
});

byId('staged-sync').addEventListener('click', async () => {
  if (!stagedPendingId || byId('staged-sync').disabled) return;
  phase = 'sync';
  stagedActivity = '正在核对保存';
  render();
  try {
    await synchronizeStagedPublication();
  } finally {
    phase = 'idle';
    stagedActivity = '';
    render();
    refreshStagedTasks();
    refreshTrial();
  }
});

async function refreshTrial() {
  const request = ++trialRequest;
  try {
    const value = await workspaceApi('/api/trial');
    if (request !== trialRequest) return;
    trial = value;
    requirementAssistant.updateTrial(trial);
    render();
  } catch {
    // A balance read failure does not block local tools or saving.
  }
}

function labelTool(button, version) {
  button.dataset.version = 'v' + version;
  button.setAttribute('aria-label', button.dataset.title + ' · 版本 ' + version);
}

let latestToolId;

async function loadLibrary(timeout) {
  const { items, latest } = await workspaceApi('/api/tools', undefined, timeout);
  const buttons = items.map(tool => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.id = tool.id;
    const title = document.createElement('span');
    title.textContent = button.dataset.title = tool.title;
    button.append(title);
    labelTool(button, tool.version);
    button.addEventListener('click', () => selectTool(tool.id, button));
    li.append(button);
    return li;
  });
  byId('tool-list').replaceChildren(...buttons);
  byId('library-empty').hidden = items.length > 0;
  render();
  latestToolId = latest;
  return latest;
}

function compose(enabled, mode = 'compose') {
  workspace.dataset.mode = enabled ? mode : 'browse';
  composer.hidden = !enabled;
  if (workspace.dataset.mode !== 'start') dismissCoachmark();
}

function markNavigation(current) {
  for (const id of primaryNavigation) {
    if (id === current) byId(id).setAttribute('aria-current', 'page');
    else byId(id).removeAttribute('aria-current');
  }
}

function dismissCoachmark() {
  coachmark.hidden = true;
  form.removeAttribute('data-guided');
  const descriptions = (requirement.getAttribute('aria-describedby') || '').split(/\s+/)
    .filter(id => id && id !== coachmark.id);
  if (descriptions.length) requirement.setAttribute('aria-describedby', descriptions.join(' '));
  else requirement.removeAttribute('aria-describedby');
}

function showCoachmark() {
  coachmark.hidden = false;
  form.setAttribute('data-guided', '');
  const descriptions = new Set((requirement.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
  descriptions.add(coachmark.id);
  requirement.setAttribute('aria-describedby', [...descriptions].join(' '));
}

requirement.addEventListener('focus', dismissCoachmark);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !coachmark.hidden) dismissCoachmark();
});

function hideSample() {
  if (!sampleTemplate) return;
  sampleTemplate = null;
  closeSample();
  preview.hidden = false;
}

function enterQuickExperience() {
  clearStagedSelection();
  if (entryMode === 'table') tableImport.close();
  byId('generation-category').value = 'auto';
  editing = false;
  entryMode = 'fast';
  failure = '';
  compose(true);
  render();
  byId('quick-panel').querySelector('[aria-pressed="true"]')?.focus();
}

const tableImport = setupTableImport({
  request: workspaceApi,
  generate: generateTable,
  onExit: () => beginComposition(false, requirement.value),
});
const requirementAssistant = setupRequirementAssistant({
  request: workspaceApi,
  offerStorageKey: () => 'root:assistant-offer:' + session.marker,
  onViewChange: view => {
    if (view === 'drafts') assistantDestination = 'drafts-entry';
    else if (view === 'idea') assistantDestination = 'idea-entry';
    if (!byId('assistant-panel').hidden) markNavigation(assistantDestination);
  },
  onBuild: text => beginComposition(false, text),
  onExit: () => {
    requirementAssistant.close();
    render();
    byId(assistantDestination).focus();
  },
});

byId('quick-entry').addEventListener('click', enterQuickExperience);
byId('direct-entry').addEventListener('click', () => beginComposition(false, editing ? '' : requirement.value));
byId('table-entry').addEventListener('click', () => {
  clearStagedSelection();
  hideSample();
  editing = false;
  failure = '';
  entryMode = 'table';
  compose(true);
  render();
  tableImport.open();
});
function openAssistant(destination) {
  dismissCoachmark();
  hideSample();
  requirementAssistant.open(destination);
  assistantDestination = destination === 'drafts' ? 'drafts-entry' : 'idea-entry';
  render();
}

byId('idea-entry').addEventListener('click', () => openAssistant());
byId('assistant-entry').addEventListener('click', () => openAssistant());
byId('drafts-entry').addEventListener('click', () => openAssistant('drafts'));
byId('guide-start').addEventListener('click', () => {
  if (phase !== 'idle' || pendingPublication || stagedPendingId || editing) return;
  byId('account').open = false;
  beginComposition(false, requirement.value, byId('generation-category').value, false);
  byId('new-tool').focus();
  showCoachmark();
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
      const row = versionRecord({
        version: 'v' + item.version, note: item.note,
        current: item.version === current ? '当前' : '',
        date: date && date.toISOString(),
        label: date ? date.toLocaleString('zh-CN') + (item.approximate ? '（文件时间）' : '') : '时间未记录',
      });
      if (item.version !== current && active.storageLimit === tableStorageBytes) {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.restoreVersion = item.version;
        button.textContent = '恢复 v' + item.version;
        button.addEventListener('click', () => {
          if (phase !== 'idle' || pendingPublication || stagedPendingId) return;
          restoreDialog.returnValue = '';
          restoreDialog.dataset.version = item.version;
          byId('restore-version-title').textContent = '恢复 v' + item.version;
          restoreDialog.showModal();
        });
        row.append(button);
      }
      return row;
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
  if (updates) dismissCoachmark();
  byId('updates').hidden = byId('updates-back').hidden = !updates;
  byId('account').open = false;
  byId('auth').hidden = updates || !identityResolved || Boolean(session);
  byId('account').hidden = workspace.hidden = updates || !session;
  document.title = updates ? '工作台更新记录 · ROOT' : 'ROOT 工具工作台';
  if (updates) loadReleases();
  if (session) render();
  if (focus) byId(updates ? 'updates-title' : session ? 'account-name' : 'auth-title').focus();
}

for (const id of ['updates-link', 'updates-back']) {
  byId(id).addEventListener('click', event => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (id === 'updates-link') history.pushState({ fromWorkbench: true }, '', '/updates');
    else if (history.state?.fromWorkbench) {
      history.back();
      return;
    } else history.replaceState(null, '', event.currentTarget.getAttribute('href'));
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

function showRecordSave(frame, state) {
  if (frame !== active) return;
  const notice = byId('record-save-status');
  if (notice.dataset.state === state) return;
  const messages = {
    pending: ['正在保存…', '请等保存完成再刷新。'],
    saved: ['已保存', '刷新后可从「我的工具」找回记录。'],
    failed: ['保存失败', '这次更改尚未保存，刷新前请先记下。'],
    unknown: ['无法确认是否已保存', '请先记下本次更改，再从「我的工具」重开核对。'],
  };
  const [title, detail] = messages[state] ?? ['', ''];
  notice.dataset.state = state;
  byId('record-save-title').textContent = title;
  byId('record-save-detail').textContent = detail;
  notice.hidden = !title;
}

function serializeEntries(entries, limit) {
  if (!Array.isArray(entries) || !entries.every(
    entry => Array.isArray(entry) && entry.length === 2 && entry.every(value => typeof value === 'string'),
  )) throw new TypeError('无效的工具存储数据');
  const json = JSON.stringify(entries);
  if (new TextEncoder().encode(json).length > limit) throw new RangeError('工具存储已满');
  return json;
}

// Runs inside the opaque frame before any generated code. It has only this tool's snapshot.
function initializeFrame(entries, token, parentOrigin, limit, backup, storageDisabled, recordCode = false) {
  if (recordCode) document.currentScript?.remove();
  const send = parent.postMessage.bind(parent);
  const values = new Map(entries);
  const existing = new Set(values.keys());
  const reads = new Set();
  let paused = false;
  let restoringBackup = null;
  let backupSequence = 0;
  let recordRequest = null;
  let recordSequence = 0;
  const report = (type, extra = {}) => send({ token, type, ...extra }, parentOrigin);
  function receive(data) {
    if (data.type === 'pause-storage') {
      paused = true;
      if (recordCode) sendRecord({type: 'storage-paused', reads: Array.from(reads)});
      else report('storage-paused', {reads: Array.from(reads)});
    } else if (data.type === 'resume-storage') {
      paused = false;
    } else if (data.type === 'record-result' && recordRequest?.id === data.request) {
      const operation = recordRequest;
      recordRequest = null;
      if (data.error) operation.reject(new Error(data.error));
      else operation.resolve(data.records);
    } else if (data.type === 'backup-result' && restoringBackup?.request === data.request) {
      const operation = restoringBackup;
      restoringBackup = null;
      if (data.error) operation.reject(new Error(data.error));
      else {
        values.set(operation.key, operation.value);
        operation.resolve();
      }
    }
  }
  let sendRecord;
  if (!recordCode) window.addEventListener('message', event => {
    if (event.source === parent && event.origin === parentOrigin && event.data?.token === token) receive(event.data);
  });
  if (recordCode) {
    const channel = new MessageChannel();
    sendRecord = channel.port1.postMessage.bind(channel.port1);
    channel.port1.onmessage = event => receive(event.data);
    send({token, type: 'record-connect'}, parentOrigin, [channel.port2]);
    const RecordPromise = Promise;
    const reject = Promise.reject.bind(Promise);
    let writeUntil = 0;
    const clock = performance.now.bind(performance);
    const permitWrite = event => {
      if (event.isTrusted) writeUntil = clock() + 1000;
    };
    window.addEventListener('click', permitWrite, true);
    window.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') permitWrite(event);
    }, true);
    window.addEventListener('change', permitWrite, true);
    const call = operation => {
      if (paused || recordRequest) return reject(new Error('请等当前操作完成。'));
      if (operation.action !== 'list') {
        if (!writeUntil || clock() > writeUntil) return reject(new Error('请通过页面操作修改记录。'));
        writeUntil = 0;
      }
      return new RecordPromise((resolve, reject) => {
        recordRequest = {id: ++recordSequence, resolve, reject};
        if (operation.action === 'list' && existing.has('root-structured-records-v1')) {
          reads.add('root-structured-records-v1');
        }
        sendRecord({type: 'record-operation', request: recordRequest.id, operation});
      });
    };
    Object.defineProperty(window, 'rootRecords', {value: Object.freeze({
      list: () => call({action: 'list'}),
      create: values => call({action: 'create', values}),
      update: (id, values) => call({action: 'update', id, values}),
    })});
  }
  if (backup) {
    Object.defineProperty(window, 'rootSaveBackup', {
      value(key, value) {
        if (key !== 'root-structured-records-v1' || typeof value !== 'string') {
          return Promise.reject(new Error('备份存储格式无效。'));
        }
        if (paused || restoringBackup) {
          return Promise.reject(new Error('工具正忙，请稍后重试。'));
        }
        return new Promise((resolve, reject) => {
          const request = ++backupSequence;
          restoringBackup = { request, key, value, resolve, reject };
          report('backup-restore', { request, key, value });
        });
      },
    });
  }
  function commit(next) {
    if (paused || restoringBackup) throw new DOMException('正在保存或切换工具，请稍后重试。', 'InvalidStateError');
    const snapshot = Array.from(next);
    if (new TextEncoder().encode(JSON.stringify(snapshot)).length > limit) {
      report('quota');
      throw new DOMException('工具存储已满', 'QuotaExceededError');
    }
    values.clear();
    for (const [key, value] of next) values.set(key, value);
    report('storage', { entries: snapshot });
  }
  if (!storageDisabled && !recordCode) Object.defineProperty(window, 'localStorage', {
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
  if (frame.saveFailed) return;
  const sequence = ++frame.recordSaveSequence;
  showRecordSave(frame, 'pending');
  frame.saveTail = frame.saveTail.then(async () => {
    if (frame.saveFailed) return;
    try {
      const saved = await workspaceApi('/api/tools/' + frame.id + '/data', {
        revision: frame.revision, version: frame.version, entries: JSON.parse(snapshot),
      }, publicationTimeout);
      frame.revision = saved.revision;
      if (sequence === frame.recordSaveSequence) showRecordSave(frame, 'saved');
    } catch (error) {
      frame.saveFailed = true;
      showRecordSave(frame, error.code === 'transport' ? 'unknown' : 'failed');
    }
  });
}

function enqueueBackupRestore(frame, { request, key, value }) {
  if (!Number.isSafeInteger(request) || request < 1 || !frame.backup) return;
  const reply = error => {
    frame.iframe.contentWindow?.postMessage({ type: 'backup-result', token: frame.token, request, error }, '*');
  };
  if (frame !== active || key !== 'root-structured-records-v1' || typeof value !== 'string' ||
      pendingPublication?.runId || frame.backupPending) {
    reply('当前工具无法恢复备份。');
    return;
  }
  let snapshot;
  try {
    const next = new Map(JSON.parse(frame.snapshot)).set(key, value);
    snapshot = serializeEntries([...next], frame.storageLimit);
  } catch {
    reply('备份数据超出工具存储限制。');
    return;
  }
  frame.backupPending = true;
  frame.saveTail = frame.saveTail.then(async () => {
    if (frame.saveFailed) throw new Error('此工具有未确认的数据，请重新打开后再恢复。');
    const path = '/api/tools/' + frame.id;
    const revision = frame.revision;
    const payload = { revision, version: frame.version, entries: JSON.parse(snapshot) };
    let saved;
    try {
      saved = await workspaceApi(path + '/data', payload, publicationTimeout);
    } catch (error) {
      if (error.code !== 'transport') throw error;
      try {
        const current = await workspaceApi(path, undefined, publicationTimeout);
        if (current.version === frame.version && current.revision > revision &&
            serializeEntries(current.entries, frame.storageLimit) === snapshot) saved = current;
      } catch {
        // No acknowledgement for the specific snapshot.
      }
      if (!saved) {
        frame.saveFailed = true;
        if (frame === active) {
          frame.iframe.inert = true;
          warnStorage('无法确认备份是否已保存，请从「我的工具」重新打开核对。');
        }
        throw new Error('无法确认备份是否已保存，请重新打开工具核对。');
      }
    }
    frame.revision = saved.revision;
    frame.snapshot = snapshot;
  }).then(() => reply(), error => reply(error.message)).finally(() => {
    frame.backupPending = false;
  });
}

async function handleRecordOperation(frame, {request, operation}) {
  if (!Number.isSafeInteger(request)) return;
  const reply = (records, error) => frame.recordPort?.postMessage(
    {type: 'record-result', request, records, error});
  if (frame.recordPending) {
    reply(null, '请等当前操作完成。');
    return;
  }
  if (frame === candidate) {
    if (operation?.action !== 'list') {
      frame.reject(new Error('候选预览试图在初始化时写入记录。'));
      reply(null, '候选预览不能写入记录。');
      return;
    }
    const records = new Map(JSON.parse(frame.snapshot)).get('root-structured-records-v1');
    reply(records ? JSON.parse(records) : []);
    return;
  }
  if (frame !== active || frame.iframe.inert || pendingPublication) {
    reply(null, '工具正在切换，请重开后再试。');
    return;
  }
  frame.recordPending = true;
  const writing = operation?.action !== 'list';
  if (writing) showRecordSave(frame, 'pending');
  try {
    const saved = await workspaceApi('/api/tools/' + frame.id + '/records', {
      version: frame.version, revision: frame.revision, operation,
    }, publicationTimeout);
    if (writing) {
      frame.snapshot = serializeEntries(saved.entries, frame.storageLimit);
      frame.revision = saved.revision;
      showRecordSave(frame, 'saved');
    }
    if (!writing && frame.revision !== saved.revision) {
      reply(null, '记录已在另一处更新，请重新打开工具。');
      return;
    }
    reply(saved.records);
  } catch (error) {
    if (writing) showRecordSave(frame, error.code === 'transport' ? 'unknown' : 'failed');
    reply(null, error.message);
  } finally {
    frame.recordPending = false;
  }
}

window.addEventListener('message', event => {
  if (event.origin !== 'null' || !event.data || typeof event.data !== 'object') return;
  const frame = [candidate, active].find(
    item => item && event.source === item.iframe.contentWindow && event.data.token === item.token,
  );
  if (!frame) return;
  const { type, entries } = event.data;
  if (type === 'record-connect' && frame === candidate && frame.recordCode && !frame.recordPort && event.ports.length === 1) {
    frame.recordPort = event.ports[0];
    frame.recordPort.onmessage = ({data}) => {
      if (frame !== candidate && frame !== active) return;
      if (data?.type === 'record-operation') handleRecordOperation(frame, data);
      else if (data?.type === 'storage-paused') frame.acknowledgePause?.(data.reads);
    };
  } else if (type === 'storage') {
    if (frame.storageDisabled || frame.recordCode) return;
    if (frame === active && pendingPublication?.runId) return;
    try {
      const snapshot = serializeEntries(entries, frame.storageLimit);
      frame.snapshot = snapshot;
      if (frame === active) enqueueSave(frame, snapshot);
    } catch {
      if (frame === candidate) frame.reject(new Error('无法保存新工具的数据，旧工具已保留。'));
      else {
        frame.recordSaveSequence += 1;
        showRecordSave(frame, 'failed');
      }
    }
  } else if (type === 'backup-restore') {
    if (!frame.storageDisabled) enqueueBackupRestore(frame, event.data);
  } else if (type === 'quota') {
    if (frame === candidate) frame.reject(new Error('新工具数据超出浏览器存储限制。'));
    else {
      frame.recordSaveSequence += 1;
      showRecordSave(frame, 'failed');
    }
  } else if (frame === candidate && type === 'ready') {
    frame.resolve();
  } else if (frame === candidate && type === 'init-error') {
    frame.reject(new Error('新工具预览初始化失败，需求已保留。'));
  } else if (type === 'storage-paused') {
    frame.acknowledgePause?.(event.data.reads);
  }
});

function signalStorage(frame, type) {
  if (frame.recordCode) frame.recordPort?.postMessage({type});
  else frame.iframe.contentWindow.postMessage({ type, token: frame.token }, '*');
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

async function commitInitial(frame) {
  const operation = { id: frame.id, marker: session.marker };
  sessionStorage.setItem(publicationKey, JSON.stringify(operation));
  try {
    const saved = await workspaceApi('/api/tools/' + frame.id + '/commit', {
      entries: JSON.parse(frame.snapshot),
    }, publicationTimeout);
    clearPublication();
    return saved;
  } catch (error) {
    if (error.code === 'transport') pendingPublication = operation;
    else clearPublication();
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
    return operation?.marker === session.marker && validToolId(operation.id) &&
      (operation.runId === undefined || validToolId(operation.runId))
      ? operation : null;
  } catch {
    return null;
  }
}

async function synchronizePublication() {
  const { id, runId } = pendingPublication;
  try {
    const action = runId ? 'resolve-publish' : 'resolve-commit';
    const current = await workspaceApi('/api/tools/' + id + '/' + action, { runId }, publicationTimeout);
    if (!runId && !current.applied) {
      clearPublication();
      failure = '新工具尚未保存，本次候选已撤销。可以重试。';
      return false;
    }
    if (active?.id === id && active.version === current.version && active.revision === current.revision) {
      resumeStorage(active);
    } else {
      await openTool(current, true);
    }
    clearPublication();
    if (current.applied) {
      finishComposition(true);
      await loadLibrary().catch(error => warnStorage(error.message));
    }
    else if (editing) failure = '本次修改未发布，已同步当前工具。你可以重试。';
    else warnStorage('本次修改未发布，已恢复当前工具。');
    return current.applied;
  } catch {
    // Keep the preview and its writes paused until a later synchronization succeeds.
  }
}

async function readStagedPublication(id) {
  const { task } = await workspaceApi('/api/staged/' + id, undefined, publicationTimeout);
  if (task.state === 'published' && validToolId(task.toolId)) {
    return workspaceApi('/api/tools/' + task.toolId, undefined, publicationTimeout);
  }
  rememberStagedTask(task);
  return null;
}

async function resolveStagedPublication(id) {
  const result = await workspaceApi('/api/staged/' + id + '/resolve-publish',
    { nonce: stagedPendingNonce }, publicationTimeout);
  if (result.applied === true) return result;
  if (result.applied === false) return null;
  throw new Error('无法确认保存结果。');
}

async function commitStaged(frame, tool) {
  const { taskId, commit, sha256, nonce } = tool.staged;
  const payload = {
    toolId: frame.id, commit, sha256, nonce, entries: JSON.parse(frame.snapshot),
    runId: tool.runId, revision: tool.runId ? frame.revision : undefined,
    confirmation: tool.revisionPlan?.confirmation,
  };
  const path = '/api/staged/' + taskId;
  stagedActivity = '正在验证运行';
  render();
  const verification = await workspaceApi(path + '/verify-preview', payload, runtimeVerificationTimeout);
  if (verification.verified !== true) throw new Error('运行检查未通过，请重新检查预览。');
  rememberStagedPublication(taskId, nonce);
  stagedActivity = '正在保存';
  render();
  try {
    const saved = await workspaceApi(path + '/publish', payload, publicationTimeout);
    rememberStagedPublication(null);
    return saved;
  } catch (error) {
    if (error.code !== 'transport') {
      if (!['session_changed', 'login_required'].includes(error.code)) rememberStagedPublication(null);
      throw error;
    }
  }
  let saved;
  try {
    saved = await readStagedPublication(taskId);
  } catch {
    // The original request may still publish.
  }
  if (!saved) throw new Error('暂时无法确认保存结果，请点击「核对保存」。');
  rememberStagedPublication(null);
  return saved;
}

async function finishStagedComposition(id) {
  stagedTasks = stagedTasks.filter(task => task.id !== id);
  clearStagedSelection();
  finishComposition(true);
  await loadLibrary(publicationTimeout).catch(error => warnStorage(error.message));
}

async function synchronizeStagedPublication({ readOnly = false } = {}) {
  const id = stagedPendingId;
  if (!id) return;
  try {
    const saved = await (readOnly ? readStagedPublication(id) : resolveStagedPublication(id));
    if (saved) {
      if (active?.id !== saved.id || active.revision !== saved.revision || active.version !== saved.version) {
        await openTool(saved, true);
      }
      rememberStagedPublication(null);
      await finishStagedComposition(id);
    } else if (!readOnly) {
      rememberStagedPublication(null);
      if (active?.iframe.inert) resumeStorage(active);
      const snapshot = await workspaceApi('/api/staged/' + id, undefined, publicationTimeout).catch(() => null);
      const task = snapshot?.task || stagedTasks.find(item => item.id === id);
      if (task) {
        rememberStagedTask(task);
        requirement.value = task.requirement;
      }
      selectedStagedId = id;
      compose(true);
      stagedMessage = '未查到已保存的工具。';
    }
  } catch {
    stagedMessage = '暂时无法确认保存结果，请稍后再次核对。';
  }
}

async function openStagedCandidate(task, approvedTool = null) {
  if (phase !== 'idle' || pendingPublication || stagedPendingId || !networkIsolated) return;
  stagedAutoId = null;
  selectedStagedId = task.id;
  stagedMessage = '';
  stagedActivity = '检查预览';
  hideSample();
  phase = 'preview';
  render();
  try {
    let tool;
    try {
      if (task.state === 'published') {
        await openTool(await workspaceApi('/api/tools/' + task.toolId, undefined, publicationTimeout), true);
      } else {
        tool = approvedTool || await workspaceApi('/api/staged/' + task.id + '/candidate', {}, publicationTimeout);
      }
    } catch (error) {
      if (error.code !== 'staged_not_ready') throw error;
      const saved = await readStagedPublication(task.id);
      if (!saved) throw error;
      await openTool(saved, true);
    }
    if (tool) {
      if (tool.staged?.taskId !== task.id) throw new Error('候选与当前任务不匹配，无法打开预览。');
      if (tool.revisionPlan && !approvedTool) {
        pendingRevision = tool;
        return;
      }
      if (approvedTool) {
        const { confirmed } = await workspaceApi('/api/staged/' + task.id + '/confirm-revision',
          { nonce:tool.staged.nonce, confirmation:tool.revisionPlan.confirmation }, publicationTimeout);
        if (!confirmed) throw new Error('未能确认当前修改方案。');
      }
      await openTool(tool);
    }
    await finishStagedComposition(task.id);
  } catch (error) {
    stagedMessage = generationError(error);
  } finally {
    phase = 'idle';
    stagedActivity = '';
    render();
    if (pendingRevision) byId('revision-confirm').focus();
    refreshStagedTasks();
    refreshTrial();
  }
}

let pendingAnswerRequest = null;
async function submitRevisionAnswer(questionId, answer, selection) {
  const task = selectedStagedTask();
  if (!task || !answer || phase !== 'idle') return;
  const signature = JSON.stringify([task.id, questionId, answer, selection]);
  if (pendingAnswerRequest?.signature !== signature) {
    pendingAnswerRequest = {signature, requestId:crypto.randomUUID().replaceAll('-', '')};
  }
  phase = 'sync';
  stagedMessage = '';
  render();
  try {
    const result = await workspaceApi('/api/staged/' + task.id + '/answer-revision', {
      questionId, answer, selection, requestId:pendingAnswerRequest.requestId,
    }, publicationTimeout);
    pendingAnswerRequest = null;
    pendingRevision = null;
    rememberStagedTask(result.task);
    selectedStagedId = stagedAutoId = result.task.id;
  } catch (error) {
    stagedMessage = generationError(error);
    if (pendingRevision?.revisionPlan.fieldType) byId('revision-field-type').value = pendingRevision.revisionPlan.fieldType.type;
  } finally {
    phase = 'idle';
    render();
    refreshStagedTasks();
  }
}

byId('revision-answer-form').addEventListener('submit', event => {
  event.preventDefault();
  const question = selectedStagedTask()?.clarification;
  if (!question?.id) return;
  const choice = byId('revision-choice').value;
  const answer = choice || (!byId('revision-answer-label').hidden ? byId('revision-answer').value.trim() : '');
  submitRevisionAnswer(question.id, answer, choice ? (question.choices.length ? 'operation' : 'field') : null);
});

byId('revision-field-type').addEventListener('change', event => {
  const field = pendingRevision?.revisionPlan.fieldType;
  if (field && event.target.value && event.target.value !== field.type) {
    submitRevisionAnswer(field.id, event.target.value, 'field');
  }
});

async function closeRevisionQuestion(rewrite) {
  const task = selectedStagedTask();
  if (task?.state !== 'clarifying' || phase !== 'idle') return;
  phase = 'sync';
  render();
  try {
    const result = await workspaceApi('/api/staged/' + task.id + '/cancel-revision',
      {nonce:task.clarification.id}, publicationTimeout);
    rememberStagedTask(result.task);
    clearStagedSelection();
    if (rewrite) requirement.focus();
    else finishComposition();
  } catch (error) {
    stagedMessage = generationError(error);
  } finally {
    phase = 'idle';
    render();
  }
}

byId('revision-rewrite').addEventListener('click', () => closeRevisionQuestion(true));
byId('revision-question-cancel').addEventListener('click', () => closeRevisionQuestion(false));

byId('revision-confirm').addEventListener('click', () => {
  const task = selectedStagedTask();
  const tool = pendingRevision;
  if (!tool || task?.id !== tool.staged.taskId || phase !== 'idle') return;
  if (tool.revisionPlan.fieldType && byId('revision-field-type').value !== tool.revisionPlan.fieldType.type) return;
  pendingRevision = null;
  openStagedCandidate(task, tool);
});

async function discardRevision(adjust) {
  const tool = pendingRevision;
  if (!tool || phase !== 'idle') return;
  phase = 'sync';
  render();
  try {
    const { task } = await workspaceApi('/api/staged/' + tool.staged.taskId + '/cancel-revision',
      { nonce: tool.staged.nonce }, publicationTimeout);
    rememberStagedTask(task);
    clearStagedSelection();
    if (!adjust) finishComposition();
  } catch (error) {
    stagedMessage = generationError(error);
  } finally {
    phase = 'idle';
    render();
    if (adjust && !pendingRevision) requirement.focus();
  }
}

byId('revision-adjust').addEventListener('click', () => discardRevision(true));
byId('revision-cancel').addEventListener('click', () => discardRevision(false));

async function openTool(tool, restore = false) {
  if (!validToolId(tool.id) || typeof tool.html !== 'string' || !/^<!doctype html>/i.test(tool.html)) {
    throw new Error('工具文件不完整，无法打开预览。');
  }
  const entries = tool.entries;
  const storageLimit = tool.storageLimit === tableStorageBytes ? tableStorageBytes : maxStorageBytes;
  const original = serializeEntries(entries, storageLimit);

  const iframe = document.createElement('iframe');
  iframe.title = '生成的工具';
  const storageDisabled = tool.html.startsWith('<!doctype html><!--root:storage=none-->');
  const recordCode = tool.html.startsWith('<!doctype html><!--root:implementation=record-code-v1-->');
  const backup = !storageDisabled && !recordCode && tool.stagedBackup === true;
  iframe.setAttribute('sandbox', 'allow-scripts allow-forms' + (backup ? ' allow-downloads' : ''));
  iframe.referrerPolicy = 'no-referrer';
  iframe.tabIndex = -1;
  iframe.inert = true;
  const frame = {
    id: tool.id, token: crypto.randomUUID(), iframe, snapshot: original,
    storageLimit, backup, storageDisabled, recordCode,
    revision: tool.revision, version: tool.version, saveTail: Promise.resolve(), saveFailed: false, recordSaveSequence: 0,
  };
  const old = active;
  candidate = frame;
  const ready = new Promise((resolve, reject) => Object.assign(frame, { resolve, reject }));
  const deadline = setTimeout(() => frame.reject(new Error('准备预览超时，需求已保留。')), 5000);
  const args = JSON.stringify([storageDisabled ? [] : entries, frame.token, location.origin, storageLimit, backup, storageDisabled, recordCode]).replaceAll('<', '\\u003c');
  const bootstrap = '(' + initializeFrame.toString() + ')(...' + args + ')';
  iframe.srcdoc = '<!doctype html><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width, initial-scale=1">'
    + '<meta http-equiv="Content-Security-Policy" content="' + previewPolicy + '">'
    + '<script>' + bootstrap + '</script>' + tool.html.slice('<!doctype html>'.length);
  try {
    preview.append(iframe);
    await ready;
    let savedSnapshot = original;
    if (tool.staged && !restore) {
      if (tool.runId && old?.id === frame.id) {
        await pauseStorage(old);
        await old.saveTail;
        if (old.recordPending) throw new Error('旧版还有记录正在保存，请稍后重试。');
        if (old.saveFailed) throw new Error('旧版还有未保存的修改，请先解决保存问题再试。');
      }
      savedSnapshot = frame.snapshot;
      const saved = await commitStaged(frame, tool);
      if (saved.id !== frame.id) throw new Error('保存结果与当前候选不匹配。');
      if (saved.version !== frame.version || serializeEntries(saved.entries, storageLimit) !== savedSnapshot ||
          (saved.stagedBackup === true) !== backup) {
        frame.recordPort?.close();
        iframe.remove();
        return await openTool(saved, true);
      }
      frame.revision = saved.revision;
      await old?.saveTail;
    } else if (tool.runId) {
      const saved = await publishRevision(frame, old, tool);
      savedSnapshot = frame.snapshot;
      frame.revision = saved.revision;
    } else if (!restore) {
      savedSnapshot = frame.snapshot;
      const saved = await commitInitial(frame);
      frame.revision = saved.revision;
    } else {
      await workspaceApi('/api/tools/' + frame.id + '/open', { version: frame.version },
        pendingPublication || stagedPendingId ? publicationTimeout : undefined);
    }
    old?.recordPort?.close();
    old?.iframe.remove();
    active = frame;
    iframe.dataset.tool = frame.id;
    iframe.removeAttribute('tabindex');
    resumeStorage(frame);
    warnStorage('');
    showRecordSave(frame, '');
    restored = restore;
    if (frame.snapshot !== savedSnapshot) enqueueSave(frame, frame.snapshot);
    if (old?.id !== frame.id) showToolHistory(false);
    else if (!toolHistory.hidden) loadToolHistory();
  } finally {
    clearTimeout(deadline);
    if (active !== frame) {
      frame.recordPort?.close();
      iframe.remove();
      if (tool.runId && !pendingPublication && !stagedPendingId && old?.iframe.inert && old.iframe.isConnected) {
        resumeStorage(old);
      }
    }
    candidate = null;
  }
}

restoreDialog.addEventListener('close', async () => {
  if (restoreDialog.returnValue !== 'restore' || !active || phase !== 'idle'
      || pendingPublication || stagedPendingId) return;
  const source = active;
  const status = byId('tool-version-status');
  phase = 'restore';
  status.textContent = '正在准备恢复…';
  render();
  try {
    await source.saveTail;
    if (source.saveFailed) throw new Error('当前还有未保存的记录，请重新打开后再恢复。');
    const tool = await workspaceApi('/api/tools/' + source.id + '/restore', {
      version: source.version, revision: source.revision, restoreVersion: Number(restoreDialog.dataset.version),
    }, publicationTimeout);
    await openTool(tool);
    finishComposition();
    await loadLibrary().catch(error => warnStorage(error.message));
  } catch (error) {
    if (pendingPublication) await synchronizePublication();
    else {
      status.textContent = error.message;
      warnStorage(error.message);
    }
  } finally {
    phase = 'idle';
    render();
    if (!pendingPublication && toolHistory.hidden) byId('tool-history').focus();
  }
});

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

requirement.addEventListener('input', () => {
  if (selectedStagedTask()?.requirement !== requirement.value.trim()) clearStagedSelection();
  render();
});
example.addEventListener('click', () => {
  requirement.value = '做一个读书清单，可以输入并添加书名。刷新后保留已添加的书。';
  requirement.focus();
  render();
});
function finishComposition(saved = false) {
  if (saved && !latestToolId) byId('first-tool-saved').hidden = false;
  editing = false;
  entryMode = 'direct';
  requirement.value = '';
  compose(false);
}

async function generateTable(table) {
  if (!networkIsolated || phase !== 'idle' || pendingPublication || stagedPendingId) {
    throw new Error('当前无法开启隔离预览，请检查工作台状态后重试。');
  }
  failure = '';
  phase = 'table';
  render();
  try {
    const tool = await workspaceApi('/api/table/generate', { table }, publicationTimeout);
    await openTool(tool);
    finishComposition(true);
    await loadLibrary(publicationTimeout).catch(error => warnStorage(error.message));
  } catch (error) {
    if (pendingPublication) {
      if (await synchronizePublication()) return;
      throw new Error(failure || '暂时无法确认表格工具是否已保存，请先同步。');
    }
    failure = error.message;
    throw error;
  } finally {
    phase = 'idle';
    render();
  }
}

byId('generation-category').addEventListener('change', render);
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (generate.disabled) return;
  hideSample();
  compose(true);
  failure = '';
  phase = 'model';
  render();
  try {
    const path = editing ? '/api/tools/' + active.id + '/revise' : '/api/generate';
    const text = requirement.value.trim();
    const version = editing ? active.version : undefined;
    const category = stagedEnabled && !editing ? byId('generation-category').value : undefined;
    const submission = JSON.stringify([path, version, text, category]);
    if (stagedEnabled && stagedSubmission?.submission !== submission) {
      stagedSubmission = { submission, id: crypto.randomUUID().replaceAll('-', '') };
    }
    const tool = await workspaceApi(path, {
      requirement: text, version, category,
      requestId: stagedEnabled ? stagedSubmission.id : undefined,
    }, stagedEnabled ? publicationTimeout : undefined);
    stagedSubmission = null;
    if (stagedEnabled && tool.task) {
      rememberStagedTask(tool.task);
      selectedStagedId = stagedAutoId = tool.task.id;
      stagedMessage = '';
      if (tool.task.state === 'published') {
        rememberStagedPublication(tool.task.id);
        await synchronizeStagedPublication({ readOnly: true });
      }
      return;
    }
    phase = 'preview';
    render();
    if (editing) await openRevision(tool);
    else await openTool(tool);
    finishComposition(true);
    await loadLibrary().catch(error => warnStorage(error.message));
  } catch (error) {
    if (pendingPublication) {
      await synchronizePublication();
    }
    else {
      if (error.code !== 'transport') stagedSubmission = null;
      failure = stagedEnabled && error.code === 'transport'
        ? '暂时无法连接工作台，需求已保留。服务恢复后可重试。'
        : generationError(error);
    }
  } finally {
    phase = 'idle';
    render();
    refreshTrial();
    if (stagedEnabled) refreshStagedTasks();
  }
});

byId('sync-tool').addEventListener('click', async () => {
  if (!pendingPublication || phase !== 'idle') return;
  phase = 'sync';
  render();
  await synchronizePublication();
  phase = 'idle';
  render();
  refreshTrial();
});

async function selectTool(id, source) {
  if (!networkIsolated || pendingPublication || stagedPendingId || phase !== 'idle' || !validToolId(id)) return;
  const returnFocus = document.activeElement === source || composer.contains(document.activeElement);
  clearStagedSelection();
  hideSample();
  if (entryMode === 'table') tableImport.close();
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
    byId('first-tool-saved').hidden = true;
  } catch (error) {
    warnStorage(error.message);
  } finally {
    phase = 'idle';
    render();
    if (returnFocus && document.activeElement === document.body) source?.focus();
  }
}

function beginComposition(revise, value = '', category = 'auto', focus = true) {
  requirementAssistant.close();
  byId('first-tool-saved').hidden = true;
  clearStagedSelection();
  hideSample();
  if (entryMode === 'table') tableImport.close();
  editing = revise;
  entryMode = 'direct';
  compose(true, revise ? 'compose' : 'start');
  requirement.value = value;
  byId('generation-category').value = category;
  failure = '';
  render();
  if (focus) requirement.focus();
}
byId('new-tool').addEventListener('click', () => beginComposition(false));
byId('revise-tool').addEventListener('click', () => beginComposition(true));
function browseLibrary(focus) {
  requirementAssistant.close();
  const trigger = byId(focus);
  byId('first-tool-saved').hidden = true;
  clearStagedSelection();
  hideSample();
  if (entryMode === 'table') tableImport.close();
  failure = '';
  finishComposition();
  render();
  trigger.focus();
  if (!active && latestToolId) selectTool(latestToolId);
}
byId('return-to-library').addEventListener('click', () => browseLibrary(editing ? 'revise-tool' : 'new-tool'));
byId('library-entry').addEventListener('click', () => browseLibrary('library-entry'));

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

byId('invite').addEventListener('invalid', event => {
  event.preventDefault();
  setAuthError('invite', '请输入邀请码。');
  event.currentTarget.focus();
});
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
    const identity = await api(isRegistration ? '/api/register' : '/api/login', post({
      username: byId('username').value, password: password.value,
      invite: isRegistration ? byId('invite').value : undefined,
    }));
    password.value = '';
    if (isRegistration) {
      byId('invite').value = '';
      await start(identity);
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
    } else {
      refreshTrial();
      refreshStagedTasks();
    }
  } catch (error) {
    warnStorage(error.message);
  }
});

async function start(registration = null) {
  phase = 'startup';
  try {
    compose(true, 'start');
    renderEntryTemplate(templateKey);
    loadReleases();
    renderRoute();
    const identity = registration || await api('/api/session');
    identityResolved = true;
    session = identity.user ? identity : null;
    renderRoute();
    if (!session) return;
    byId('account-name').textContent = session.user;
    const config = await api('/api/config');
    configured = config.configured;
    stagedEnabled = config.staged === true;
    await refreshTrial();
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
      } else {
        const previousStagePublication = stagedEnabled ? restoredStagedPublication() : null;
        if (previousStagePublication) {
          if (latest) {
            phase = 'idle';
            await selectTool(latest);
          }
          rememberStagedPublication(previousStagePublication.id, previousStagePublication.nonce);
          selectedStagedId = previousStagePublication.id;
          compose(true);
          phase = 'sync';
          stagedActivity = '正在核对保存';
          render();
          await synchronizeStagedPublication({ readOnly: true });
          stagedActivity = '';
        }
      }
    }
    if (stagedEnabled) await refreshStagedTasks();
    if (registration && location.pathname !== '/updates') {
      showCoachmark();
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
