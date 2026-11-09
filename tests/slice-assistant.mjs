// AC-012 / AC-013: browser acceptance with the deterministic analysis API.
import assert from 'node:assert/strict';

export async function verifyAssistant({
  evaluate, click, type, until, activeTool, appRequest, library, login, logout,
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

  stage('assistant retains input after an error and ignores a late analysis response');
  await click('#idea-entry');
  await until(() => visible(idea));
  assert.match(await text('#assistant-panel'), /本地模拟分析/);
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

  await evaluate(`(() => {
    const original = window.assistantOriginalFetch;
    window.fetch = (url, options) => url === "/api/assistant/analyze"
      ? new Promise(resolve => {
          window.releaseLateAnalysis = () => {
            resolve(new Response(JSON.stringify({kind: "draft", text: "过期分析不得显示"})));
          };
        })
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

  stage('assistant asks different questions for two directions');
  await click('#assistant-analyze button');
  await until(() => visible('#assistant-directions [data-direction="1"]'));
  assert.match(await text('#assistant-directions'), /交付跟进板.*周报汇总/s);
  await click('#assistant-directions [data-direction="0"]');
  await until(() => visible('#assistant-questions select'));
  const firstQuestion = await text('#assistant-questions label');
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
  const trialBefore = (await appRequest('/api/trial')).body;
  await click('#assistant-organize');
  await waitStatus('模型返回的分析不完整');
  assert.equal(await visible(answer), true);
  assert.equal(await input(answer), selected);
  assert.equal(await input(idea), ambiguous + '希望能先看异常。');
  const afterFailure = (await appRequest('/api/trial')).body;
  assert.equal(afterFailure.analysis.used - trialBefore.analysis.used, 1);
  assert.equal(afterFailure.analysis.totalUsed - trialBefore.analysis.totalUsed, 1);
  assert.deepEqual(afterFailure.generation, trialBefore.generation);
  await click('#assistant-organize');
  await until(() => visible(draft));
  assert.ok((await input(draft)).includes(selected));
  assert.match(await input(draft), /周报汇总/);
  const afterRetry = (await appRequest('/api/trial')).body;
  assert.equal(afterRetry.analysis.used - afterFailure.analysis.used, 1);
  assert.equal(afterRetry.analysis.totalUsed - afterFailure.analysis.totalUsed, 1);
  assert.deepEqual(afterRetry.generation, trialBefore.generation);

  stage('clear idea opens a draft, saving survives a lost response and a conflict preserves the edit');
  await click('#assistant-edit-idea');
  const clearIdea = '做一个读书清单，可以添加书名，保存阅读状态。';
  await setInput(idea, clearIdea);
  await click('#assistant-analyze button');
  await until(() => visible(draft));
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
  assert.deepEqual(await library(), initialLibrary);
  assert.equal((await activeTool()).loader, initial.loader);

  stage('explicitly saving the compared edit updates only the reviewed revision');
  await click('#idea-entry');
  await until(() => visible('#assistant-replace-saved'));
  await click('#assistant-replace-saved');
  await waitStatus('已保存');
  assert.equal((await appRequest(draftsPath + '/' + id)).body.text, edited);

  stage('saved drafts survive login and stay private to their account');
  await logout();
  await login(accountA);
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
    invalidAnswersRecovered: true, analysisChargesForFailureAndRetry: 2,
    lostReceiptRecovered: true, conflictRetainsEdit: true, comparisonRetainsEdit: true, generationDuringHandoff: 0,
    draftSurvivesLogin: true, crossAccountReadAndWrite: 404};
}
