'use strict';

function setupRequirementAssistant({request, offerStorageKey, onGuideUpdate, onBuild, onExit}) {
  const panel = document.getElementById('assistant-panel');
  const draftsPath = '/api/requirement-drafts';
  const maxIdea = 1200;
  const maxDraft = 2000;
  const draftId = /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i;
  const isText = (value, max) => typeof value === 'string' && value.trim() !== '' && [...value].length <= max;
  const isRevision = value => Number.isSafeInteger(value) && value >= 1;
  const invalid = () => new Error('收到的结果不完整。输入已保留，请重试。');
  let view = 'idea';
  let previousView = 'idea';
  let epoch = 0;
  let pending = false;
  let saving = false;
  let directions = [];
  let selectedDirection = null;
  let questions = [];
  let draft = null;
  let conflict = false;
  let comparison = null;
  let round = null;
  let pendingOffer = null;
  const inFlightAnalysis = new Set();

  function node(tag, attributes = {}, ...children) {
    const element = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    element.append(...children);
    return element;
  }

  function button(id, text, handler, primary = false) {
    const element = node('button', {type: 'button', ...(id ? {id} : {})}, text);
    if (primary) element.classList.add('elys-primary');
    element.addEventListener('click', handler);
    return element;
  }

  function field(label, id, limit, rows) {
    const input = node('textarea', {id, maxlength: limit, rows});
    return [node('label', {for: id}, label), input];
  }

  const title = node('h1', {id: 'assistant-title', tabindex: '-1'}, '需求助手');
  panel.setAttribute('aria-labelledby', title.id);
  const status = node('p', {id: 'assistant-status', role: 'status', 'aria-live': 'polite'});
  const quotaHelp = '一条需求首次生成可编辑草稿时扣 1 次。换方向、追问、失败重试、编辑和保存不另扣。';
  const analysisInfo = node('p', {class: 'auth-muted'}, quotaHelp);
  const balance = node('p', {id: 'analysis-balance', class: 'auth-muted', role: 'status', hidden: ''});
  const dirty = node('p', {id: 'assistant-dirty', class: 'auth-muted'}, '有未保存的修改。');
  const [ideaLabel, idea] = field('描述你想解决的问题', 'assistant-idea-text', maxIdea, 4);
  const [draftLabel, editor] = field('需求草稿', 'assistant-draft-editor', maxDraft, 8);
  const start = button(null, '分析我的想法', () => analyze({step: 'start', idea: idea.value.trim()}), true);
  const organize = button('assistant-organize', '整理成需求草稿', organizeAnswers, true);
  const save = button('assistant-save', '保存需求', () => saveDraft(), true);
  const reviewSaved = button('assistant-review-saved', '核对保存版', () => openDraft(draft.id));
  const [savedLabel, savedEditor] = field('当前保存版（只读）', 'assistant-saved-version', maxDraft, 5);
  savedEditor.readOnly = true;
  const replaceSaved = button('assistant-replace-saved', '用本次编辑更新保存版', () => saveDraft(comparison.revision));
  const comparisonPanel = node('div', {class: 'assistant-draft-comparison'}, savedLabel, savedEditor,
    node('p', {class: 'auth-muted'}, '你本次的编辑仍在上方。核对后可修改文字，再明确更新保存版。'),
    node('div', {class: 'assistant-actions'}, replaceSaved));
  const build = button('assistant-build', '带到直接开发', () => {
    if (isText(editor.value, maxDraft)) onBuild(editor.value);
  });
  const myDrafts = button('assistant-drafts', '查看我的需求', openDrafts);
  const recoverDraft = button('assistant-recover-draft', '找回草稿', () => {
    run('正在找回草稿…', () => claimDraft(pendingOffer), receiveDraft);
  });
  const newRound = button('assistant-new-round', '梳理新需求', () => {
    round = null;
    idea.value = '';
    directions = [];
    selectedDirection = null;
    questions = [];
    show('idea', idea);
  });
  const editIdea = button('assistant-edit-idea', '修改原始问题', () => show('idea', idea));
  const changeDirection = button('assistant-change-direction', '换个方向', () => show('directions', directionList.firstElementChild));
  const backToDraft = button('assistant-drafts-back', '返回上一步', () => show(previousView));
  const retryList = button('assistant-drafts-retry', '重新读取列表', openDrafts);
  const understanding = node('p', {class: 'assistant-understanding'});
  const directionList = node('div', {class: 'assistant-options'});
  const questionList = node('div', {class: 'assistant-questions-list'});
  const list = node('ul', {id: 'assistant-draft-list', class: 'assistant-draft-list'});
  const screen = (id, heading, ...children) => node('section', {id, 'aria-labelledby': id + '-heading'},
    node('h2', {id: id + '-heading', tabindex: '-1'}, heading), ...children);

  const views = {
    idea: screen('assistant-idea', '你想解决什么问题？', ideaLabel, idea,
      node('div', {id: 'assistant-analyze', class: 'assistant-actions'}, start)),
    directions: screen('assistant-directions', '选一个方向', understanding, directionList),
    questions: screen('assistant-questions', '还有几个关键问题', questionList,
      node('div', {class: 'assistant-actions'}, organize)),
    draft: screen('assistant-draft', '检查需求草稿',
      node('div', {id: 'assistant-draft-text'}, draftLabel, editor), dirty,
      node('div', {class: 'assistant-actions'}, save, reviewSaved, build), comparisonPanel),
    drafts: screen('assistant-drafts-view', '我的需求', list,
      node('div', {class: 'assistant-actions'}, backToDraft, retryList)),
  };

  panel.replaceChildren(
    node('header', {class: 'assistant-header'},
      node('div', {}, node('p', {class: 'auth-eyebrow'}, '整理需求'), title),
      button('assistant-exit', '返回工作台', onExit)),
    analysisInfo, balance,
    node('div', {class: 'assistant-actions assistant-nav'}, myDrafts, editIdea, changeDirection, newRound, recoverDraft),
    ...Object.values(views), status,
  );

  function feedback(message = '', error = false) {
    status.textContent = message;
    status.classList.toggle('elys-error', error);
  }

  function refresh() {
    for (const [name, section] of Object.entries(views)) section.hidden = name !== view;
    myDrafts.hidden = view === 'drafts';
    editIdea.hidden = view === 'idea' || view === 'drafts' || !idea.value.trim();
    changeDirection.hidden = !directions.length || !['questions', 'draft'].includes(view);
    myDrafts.disabled = view === 'draft' && saving;
    recoverDraft.hidden = !pendingOffer;
    recoverDraft.disabled = pending;
    newRound.hidden = view !== 'draft' || Boolean(pendingOffer);
    newRound.disabled = pending || saving;
    start.disabled = pending || Boolean(pendingOffer) || !isText(idea.value, maxIdea);
    organize.disabled = pending || Boolean(pendingOffer) || !questions.length || !readAnswers().every(Boolean);
    const canSave = !pending && !saving && isText(editor.value, maxDraft);
    save.disabled = !canSave || conflict || editor.value === draft?.savedText;
    reviewSaved.hidden = !conflict || Boolean(comparison);
    reviewSaved.disabled = pending || saving;
    comparisonPanel.hidden = !comparison;
    replaceSaved.disabled = !canSave || !comparison;
    build.disabled = !isText(editor.value, maxDraft);
    retryList.disabled = pending;
    dirty.hidden = !editor.value || editor.value === draft?.savedText;
    onGuideUpdate();
  }

  function invalidate() {
    epoch += 1;
    pending = false;
    feedback();
  }

  function show(next, focus = views[next].querySelector('h2')) {
    invalidate();
    view = next;
    refresh();
    focus?.focus();
  }

  async function run(message, action, apply) {
    const requestEpoch = ++epoch;
    pending = true;
    feedback(message);
    refresh();
    try {
      const result = await action(() => requestEpoch === epoch);
      if (requestEpoch !== epoch) return;
      feedback();
      apply(result);
    } catch (error) {
      if (requestEpoch === epoch) feedback(error.message || '暂时无法完成，请重试。', true);
    } finally {
      if (requestEpoch === epoch) {
        pending = false;
        refresh();
      }
    }
  }

  function readAnswers() {
    return Array.from(questionList.querySelectorAll('select'), input => input.value);
  }

  function organizeAnswers() {
    if (organize.disabled) return;
    analyze({
      step: 'answers', idea: idea.value.trim(), direction: selectedDirection,
      questions, answers: readAnswers(),
    });
  }

  function rememberOffer(id) {
    pendingOffer = id;
    try {
      if (id) sessionStorage.setItem(offerStorageKey(), id);
      else sessionStorage.removeItem(offerStorageKey());
    } catch {
      // The current tab can still retry without session storage.
    }
  }

  async function claimDraft(id) {
    try {
      return await request('/api/assistant/claim-draft', {offerId: id});
    } catch (error) {
      if (['analysis_draft_unavailable', 'analysis_account_limit'].includes(error.code)) rememberOffer(null);
      if (error.code === 'transport') throw new Error('暂时无法确认草稿，请点「找回草稿」。');
      throw error;
    }
  }

  function receiveDraft(result) {
    if (result?.kind !== 'draft' || !isText(result.text, maxDraft)) throw invalid();
    rememberOffer(null);
    openEditor(result.text, null);
  }

  function analyze(data) {
    if (pendingOffer || !isText(data.idea, maxIdea)) return;
    if (round?.idea !== data.idea) round = {idea: data.idea, id: crypto.randomUUID()};
    const roundId = round.id;
    const signature = JSON.stringify([roundId, data]);
    if (inFlightAnalysis.has(signature)) return;
    inFlightAnalysis.add(signature);
    run('正在整理你的想法…', async current => {
      try {
        const requestId = crypto.randomUUID();
        const result = await request('/api/assistant/analyze', {...data, roundId, requestId});
        if (result?.kind !== 'draft_ready' || !current()) return result;
        if (result.offerId !== requestId) throw invalid();
        rememberOffer(result.offerId);
        return await claimDraft(result.offerId);
      } finally {
        inFlightAnalysis.delete(signature);
      }
    }, result => {
      if (result?.kind === 'draft' && pendingOffer) {
        if (data.step === 'start') directions = [];
        receiveDraft(result);
        return;
      }
      if (result?.kind === 'directions' && data.step === 'start' && isText(result.understanding, maxIdea + 80) &&
          Array.isArray(result.directions) && result.directions.length >= 2 && result.directions.length <= 3 &&
          result.directions.every(item => isText(item?.title, 120) && isText(item?.description, 500))) {
        directions = result.directions.map(({title, description}) => ({title, description}));
        understanding.textContent = result.understanding;
        directionList.replaceChildren(...directions.map((direction, index) => {
          const item = button(null, '', () => {
            selectedDirection = direction;
            analyze({step: 'direction', idea: idea.value.trim(), direction});
          });
          item.dataset.direction = index;
          item.append(node('strong', {}, direction.title), node('span', {}, direction.description));
          return item;
        }));
        show('directions');
        return;
      }
      if (result?.kind === 'questions' && data.step === 'direction' && Array.isArray(result.questions) &&
          result.questions.length >= 1 && result.questions.length <= 2 &&
          result.questions.every(item => isText(item?.prompt, 300) && Array.isArray(item?.options) &&
            item.options.length >= 2 && item.options.length <= 5 && item.options.every(option => isText(option, 300)))) {
        questions = result.questions.map(({prompt, options}) => ({prompt, options}));
        questionList.replaceChildren(...questions.map(({prompt, options}, index) => {
          const id = 'assistant-answer-' + index;
          const input = node('select', {id, 'data-question': index},
            node('option', {value: ''}, '请选择'),
            ...options.map(option => node('option', {value: option}, option)));
          input.addEventListener('change', () => {
            invalidate();
            refresh();
          });
          return node('div', {}, node('label', {for: id}, prompt), input);
        }));
        show('questions');
        return;
      }
      throw invalid();
    });
  }

  function openEditor(text, record) {
    draft = record;
    conflict = false;
    comparison = null;
    editor.value = text;
    show('draft', editor);
  }

  function readDraft(record, id) {
    if (record?.id !== id || !isText(record.text, maxDraft) || !isRevision(record.revision)) throw invalid();
    return record;
  }

  async function saveDraft(reviewedRevision) {
    if (reviewedRevision === undefined ? save.disabled : replaceSaved.disabled) return;
    draft ??= {id: crypto.randomUUID(), revision: 0, savedText: null};
    const {id, revision: originalRevision} = draft;
    const revision = reviewedRevision ?? originalRevision;
    const text = editor.value;
    const url = draftsPath + '/' + id;
    const saveEpoch = epoch;
    saving = true;
    feedback('正在保存需求…');
    refresh();
    try {
      let result;
      try {
        result = readDraft(await request(url, {text, revision}), id);
      } catch (error) {
        if (error.code !== 'transport') throw error;
        const restored = await request(url).catch(() => null);
        if (restored?.id !== id || restored.text !== text || restored.revision !== revision + 1) throw error;
        result = readDraft(restored, id);
      }
      if (result.text !== text || result.revision !== revision + 1) throw invalid();
      if (draft?.id === id && draft.revision === originalRevision) {
        draft = {id, revision: result.revision, savedText: text};
        conflict = false;
        comparison = null;
        if (saveEpoch === epoch) feedback('已保存到「我的需求」。');
      }
    } catch (error) {
      if (error.code === 'draft_conflict' && draft?.id === id) {
        conflict = true;
        comparison = null;
      }
      if (saveEpoch === epoch) feedback(error.message || '暂时无法保存，请重试。', true);
    } finally {
      saving = false;
      refresh();
    }
  }

  function openDrafts() {
    if (view !== 'drafts') previousView = view;
    show('drafts');
    list.replaceChildren();
    run('正在读取我的需求…', () => request(draftsPath), result => {
      if (!Array.isArray(result?.items) || !result.items.every(item =>
        draftId.test(item?.id) && isText(item.title, maxDraft) && isRevision(item.revision))) throw invalid();
      if (!result.items.length) {
        list.replaceChildren(node('li', {class: 'auth-muted'}, '暂无已保存的需求。'));
        return;
      }
      list.replaceChildren(...result.items.map(item => {
        const open = button(null, item.title, () => openDraft(item.id));
        open.dataset.draftId = item.id;
        const date = new Date(item.updatedAt);
        if (Number.isFinite(date.valueOf())) {
          open.append(node('span', {class: 'auth-muted'}, date.toLocaleString('zh-CN')));
        }
        return node('li', {}, open);
      }));
    });
  }

  function openDraft(id) {
    run('正在打开草稿…', async () => readDraft(await request(draftsPath + '/' + id), id), result => {
      if (draft?.id === id && editor.value !== draft.savedText) {
        conflict = result.revision !== draft.revision;
        comparison = conflict ? result : null;
        savedEditor.value = result.text;
        show('draft', conflict ? savedEditor : editor);
        return;
      }
      directions = [];
      selectedDirection = null;
      questions = [];
      openEditor(result.text, {id, revision: result.revision, savedText: result.text});
    });
  }

  idea.addEventListener('input', () => {
    invalidate();
    refresh();
  });
  editor.addEventListener('input', refresh);

  refresh();
  return {
    open() {
      panel.hidden = false;
      try {
        pendingOffer ??= sessionStorage.getItem(offerStorageKey());
      } catch {
        // Session storage is optional.
      }
      refresh();
      views[view].querySelector('h2').focus();
    },
    close() {
      invalidate();
      panel.hidden = true;
    },
    updateTrial(trial) {
      analysisInfo.textContent = trial.analysisMode === 'fixture'
        ? '当前使用本地模拟分析。' + quotaHelp
        : quotaHelp;
      const usage = trial.analysis;
      balance.textContent = '今日可梳理需求 ' + usage.remaining + ' / ' + usage.limit + ' 条'
        + (trial.paused ? ' · 模型调用暂时暂停' : !usage.totalRemaining ? ' · 试用的总分析额度已用完' : '');
      balance.hidden = false;
    },
  };
}
