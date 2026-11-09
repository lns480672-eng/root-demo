// AC-012 / AC-013: browser acceptance with the deterministic analysis API.
import assert from 'node:assert/strict';

export async function verifyAssistant({
  evaluate, click, type, until, cdp, activeTool, appRequest, library, login, logout,
  accountA, accountB, accountBRegistered = false, screenshot, stage,
}) {
  const idea = '#assistant-idea textarea';
  const draft = '#assistant-draft-text textarea';
  const draftsPath = '/api/requirement-drafts';
  const visible = selector => evaluate(
    `Boolean(document.querySelector(${JSON.stringify(selector)})?.checkVisibility())`,
  );
  const text = selector => evaluate(
    `document.querySelector(${JSON.stringify(selector)})?.textContent || ""`,
  );
  const input = selector => evaluate(
    `document.querySelector(${JSON.stringify(selector)})?.value`,
  );
  const clear = selector => evaluate(
    `document.querySelector(${JSON.stringify(selector)}).value = ""`,
  );
  const setInput = async (selector, value) => {
    await clear(selector);
    await type(selector, value);
  };
  const waitStatus = message => until(async () => (await text('#assistant-status')).includes(message));
  const initial = await activeTool();
  const initialLibrary = await library();
  const trial = async () => (await appRequest('/api/trial')).body;
  const initialTrial = await trial();
  const checkBalance = async (personal, dispatches) => {
    const current = await trial();
    assert.equal(current.analysis.used - initialTrial.analysis.used, personal);
    assert.equal(current.analysis.totalUsed - initialTrial.analysis.totalUsed, dispatches);
    assert.deepEqual(current.generation, initialTrial.generation);
    await until(async () => (await text('#analysis-balance')).includes(
      '今日可梳理需求 ' + current.analysis.remaining + ' / ' + current.analysis.limit + ' 条'));
  };

  stage('assistant retains input after an error and ignores a late analysis response');
  await click('#new-tool');
  await click('#idea-entry');
  await until(() => visible(idea));
  assert.match(await text('#assistant-panel'), /本地模拟分析/);
  assert.match(await text('#assistant-panel'), /一条需求首次生成可编辑草稿时扣 1 次/);
  await checkBalance(0, 0);
  const ambiguous = '项目进度总要在群里逐个问，如何早点发现卡住的事项？';
  await setInput(idea, ambiguous);
  await evaluate(`(() => {
    const original = window.fetch;
    window.assistantOriginalFetch = original;
    window.fetch = (url, options) => url === "/api/assistant/analyze"
      ? Promise.resolve(new Response(JSON.stringify({message: "分析暂时不可用"}), {status: 503}))
      : original(url, options);
  })()`);
  await click('#assistant-analyze button');
  await waitStatus('分析暂时不可用');
  assert.equal(await input(idea), ambiguous);

  const abandoned = '做一个读书清单，可以添加书名并保存记录。';
  await setInput(idea, abandoned);
  await evaluate(`(() => {
    const original = window.assistantOriginalFetch;
    window.fetch = (url, options) => url === "/api/assistant/analyze"
      ? original(url, options).then(response => new Promise(resolve => {
          window.releaseLateAnalysis = () => resolve(response);
        }))
      : original(url, options);
  })()`);
  await click('#assistant-analyze button');
  await until(() => evaluate('typeof window.releaseLateAnalysis === "function"'));
  await setInput(idea, ambiguous + '希望能先看异常。');
  await evaluate(`(() => {
    window.fetch = window.assistantOriginalFetch;
    window.releaseLateAnalysis();
  })()`);
  await until(async () => !(await evaluate('document.querySelector("#assistant-analyze button").disabled')));
  assert.equal(await visible(draft), false);
  assert.equal(await input(idea), ambiguous + '希望能先看异常。');
  await checkBalance(0, 1);

  stage('assistant asks different questions for two directions');
  await click('#assistant-analyze button');
  await until(() => visible('#assistant-directions [data-direction="1"]'));
  assert.match(await text('#assistant-directions'), /交付跟进板.*周报汇总/s);
  await checkBalance(0, 2);
  await click('#assistant-directions [data-direction="0"]');
  await until(() => visible('#assistant-questions select'));
  const firstQuestion = await text('#assistant-questions label');
  await checkBalance(0, 3);
  await click('#assistant-change-direction');
  await click('#assistant-directions [data-direction="1"]');
  await until(() => visible('#assistant-questions select'));
  assert.notEqual(await text('#assistant-questions label'), firstQuestion);
  await evaluate(`(() => {
    const answer = document.querySelector("#assistant-questions select");
    answer.selectedIndex = 1;
    answer.dispatchEvent(new Event("change", {bubbles: true}));
  })()`);
  stage('assistant preserves the selected answer after invalid upstream JSON and retries once');
  const answer = '#assistant-questions select';
  const selected = await input(answer);
  await checkBalance(0, 4);
  await click('#assistant-organize');
  await waitStatus('模型返回的分析不完整');
  assert.equal(await visible(answer), true);
  assert.equal(await input(answer), selected);
  assert.equal(await input(idea), ambiguous + '希望能先看异常。');
  await checkBalance(0, 5);
  stage('assistant retries the same round and claims its first draft');
  await click('#assistant-organize');
  await until(async () => {
    if (await visible('#assistant-status.elys-error')) throw new Error(await text('#assistant-status'));
    return visible(draft);
  });
  assert.ok((await input(draft)).includes(selected));
  assert.match(await input(draft), /周报汇总/);
  await checkBalance(1, 6);

  stage('clear idea opens a draft, saving survives a lost response and a conflict preserves the edit');
  await click('#assistant-new-round');
  const clearIdea = '做一个读书清单，可以添加书名，保存阅读状态。';
  await setInput(idea, clearIdea);
  await evaluate(`(() => {
    const original = window.assistantOriginalFetch;
    window.fetch = async (url, options) => {
      const response = await original(url, options);
      if (url === '/api/assistant/claim-draft' && response.ok) {
        window.fetch = original;
        throw new TypeError('fixture lost draft acknowledgement');
      }
      return response;
    };
  })()`);
  await click('#assistant-analyze button');
  await waitStatus('暂时无法确认草稿');
  assert.equal(await visible(draft), false);
  await checkBalance(2, 7);
  stage('assistant recovers a lost draft response after reloading the workbench');
  const navigation = await evaluate('performance.timeOrigin');
  await cdp('Page.reload', {ignoreCache: true});
  await until(() => evaluate(`performance.timeOrigin !== ${navigation} &&
    document.documentElement.dataset.ready === 'true'`));
  const reopenedTool = await activeTool();
  assert.equal(reopenedTool.id, initial.id);
  if (!(await visible('#idea-entry'))) await click('#new-tool');
  await evaluate('window.assistantOriginalFetch = window.fetch');
  await click('#idea-entry');
  await until(() => visible('#assistant-recover-draft'));
  await click('#assistant-recover-draft');
  await until(() => visible(draft));
  await checkBalance(2, 7);
  assert.equal(await input(draft), clearIdea);
  assert.equal(await visible('#assistant-directions'), false);
  const saved = '做一个工作室借具清单，可以录入借具名，保存归还状态。';
  await setInput(draft, saved);
  await evaluate(`(() => {
    const original = window.assistantOriginalFetch;
    window.assistantGenerated = 0;
    window.assistantLostSave = false;
    window.fetch = async (url, options) => {
      if (url === "/api/generate") window.assistantGenerated += 1;
      const response = await original(url, options);
      if (url.startsWith("/api/requirement-drafts/") && options?.method === "POST" &&
          response.ok && !window.assistantLostSave) {
        window.assistantLostSave = true;
        throw new Error("模拟网络丢失保存回执");
      }
      return response;
    };
  })()`);
  await click('#assistant-save');
  await waitStatus('已保存');
  assert.equal(await evaluate('window.assistantLostSave'), true);
  assert.equal(await visible('#assistant-dirty'), false);
  const firstList = await appRequest(draftsPath);
  assert.equal(firstList.status, 200);
  assert.equal(firstList.body.items.length, 1);
  const {id, revision} = firstList.body.items[0];
  const remote = '另一个会话更新后的草稿，可以查看最新记录。';
  const competing = await appRequest(draftsPath + '/' + id, {text: remote, revision});
  assert.equal(competing.status, 200);
  const edited = '保留当前编辑，做一个可搜索的工作室借具清单。';
  await setInput(draft, edited);
  assert.equal(await visible('#assistant-dirty'), true);
  await click('#assistant-save');
  await waitStatus('草稿已在别处更新');
  assert.equal(await input(draft), edited);
  await screenshot('assistant-conflict-retains-edit');

  stage('opening the same conflicting draft shows the saved version without replacing the edit');
  await click('#assistant-drafts');
  await until(() => visible(`#assistant-draft-list [data-draft-id="${id}"]`));
  await click(`#assistant-draft-list [data-draft-id="${id}"]`);
  await until(() => visible(draft));
  assert.equal(await input(draft), edited);
  assert.equal(await input('#assistant-saved-version'), remote);
  assert.equal((await appRequest(draftsPath + '/' + id)).body.text, remote);

  stage('assistant handoff copies the current edit without generating');
  await click('#assistant-build');
  await until(() => visible('#requirement'));
  assert.equal(await input('#requirement'), edited);
  assert.equal(await evaluate('window.assistantGenerated'), 0);
  await checkBalance(2, 7);
  assert.deepEqual(await library(), initialLibrary);
  assert.equal((await activeTool()).loader, reopenedTool.loader);

  stage('explicitly saving the compared edit updates only the reviewed revision');
  await click('#idea-entry');
  await until(() => visible('#assistant-replace-saved'));
  await click('#assistant-replace-saved');
  await waitStatus('已保存');
  assert.equal((await appRequest(draftsPath + '/' + id)).body.text, edited);

  stage('saved drafts survive login and stay private to their account');
  await logout();
  await login(accountA);
  await click('#new-tool');
  await click('#idea-entry');
  await click('#assistant-drafts');
  await until(() => visible(`#assistant-draft-list [data-draft-id="${id}"]`));
  await click(`#assistant-draft-list [data-draft-id="${id}"]`);
  await until(() => visible(draft));
  assert.equal(await input(draft), edited);
  const revised = '重新登录后编辑，可以按借具名筛选并保存。';
  await setInput(draft, revised);
  await click('#assistant-save');
  await waitStatus('已保存');
  assert.equal((await appRequest(draftsPath + '/' + id)).body.text, revised);
  await screenshot('assistant-draft-restored-and-edited');
  await logout();
  await login(accountB, !accountBRegistered);
  const other = await appRequest(draftsPath);
  assert.deepEqual(other.body.items, []);
  assert.equal((await appRequest(draftsPath + '/' + id)).status, 404);
  assert.equal((await appRequest(draftsPath + '/' + id, {text: '禁止跨账号写入', revision: 3})).status, 404);
  await logout();
  await login(accountA);
  assert.equal((await appRequest(draftsPath + '/' + id)).body.text, revised);
  assert.equal((await activeTool()).id, initial.id);
  return {id: 'TEST-012/013', result: 'passed',
    lateResponseIgnored: true, clearAndAmbiguousPaths: true, twoDirections: true,
    invalidAnswersRecovered: true, globalDispatches: 7, personalDrafts: 2,
    abandonedDraftUncharged: true, lostDraftReceiptRecoveredWithoutSecondCharge: true,
    lostReceiptRecovered: true, conflictRetainsEdit: true, comparisonRetainsEdit: true, generationDuringHandoff: 0,
    draftSurvivesLogin: true, crossAccountReadAndWrite: 404};
}
