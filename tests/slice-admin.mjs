import assert from 'node:assert/strict';

export async function verifyAdmin({evaluate, click, type, until, cdp, login, logout, visible,
  appRequest, screenshot, stage}) {
  const admin = 'qa-trial-admin';
  const member = 'qa-trial-member';
  const adminPath = '/api/trial/admin';
  const text = selector => evaluate('document.querySelector(' + JSON.stringify(selector) + ').textContent');
  const open = async () => {
    await until(() => visible('#admin-entry'));
    await click('#admin-entry');
    await until(() => visible('#backend'));
  };
  const close = async () => {
    await click('#back-to-workbench');
    await until(() => visible('#logout'));
  };
  const selectMember = `Array.from(document.querySelectorAll(".account")).find(row => row.querySelector("h3").textContent === ${JSON.stringify(member)})`;
  const quota = () => evaluate(selectMember + '.querySelector(".gen-left").textContent');
  const confirmToggle = async () => {
    await click('#toggle');
    await until(() => visible('#confirm'));
    await click('#confirm-action');
  };

  stage('AC-017 empty sample and administrator route');
  await login(admin);
  await open();
  assert.match(await text('#generation-failure'), /暂无已结束调用/);
  assert.match(await text('#analysis-failure'), /暂无已结束调用/);
  assert.match(await text('#current-operator'), /qa-trial-admin/);
  await close();
  await logout();

  stage('AC-017 normal account denies administrative URL and populates real endpoint stats');
  await login(member);
  assert.equal(await visible('#admin-entry'), false);
  assert.equal(await evaluate('fetch("/admin").then(response => response.status)'), 403);
  assert.equal((await appRequest(adminPath)).status, 403);
  assert.equal((await appRequest('/api/generate', {requirement: '做一个读书清单'})).status, 200);
  assert.equal((await appRequest('/api/generate', {requirement: '回归：模型超时'})).status, 502);
  assert.equal((await appRequest('/api/assistant/analyze', {
    step: 'start', idea: '做一个项目跟进工具，可以记录进度并保存', requestId: crypto.randomUUID(),
  })).status, 200);
  await logout();

  stage('AC-017 DOM reflects two separately calculated real failure rates');
  await login(admin);
  const stats = (await appRequest(adminPath)).body;
  assert.equal(stats.generation.failureRate, 0.5);
  assert.equal(stats.analysis.failureRate, 0);
  await open();
  assert.match(await text('#generation-failure'), /50%.*1 失败 \/ 2 已结束/);
  assert.match(await text('#analysis-failure'), /0%.*0 失败 \/ 1 已结束/);
  await screenshot('admin-desktop');
  for (const width of [700, 390, 320]) {
    await cdp('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: true });
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    await screenshot('admin-' + width);
  }
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });

  stage('AC-017 confirmed pause and idempotent grant after a lost response');
  await confirmToggle();
  await until(async () => /已暂停/.test(await text('#model-status')));
  assert.match(await text('#audit-list'), /qa-trial-admin.*暂停模型调用/);
  await evaluate(selectMember + '.querySelector(".open-top-up").click()');
  const before = Number(await quota());
  await type('.top-up:not([hidden]) input', '0');
  await click('.top-up:not([hidden]) [type=submit]');
  assert.match(await text('.top-up:not([hidden]) .error'), /正整数/);
  await evaluate(`(() => {
    const original = window.fetch;
    window.fetch = async (path, options) => {
      const response = await original(path, options);
      if (String(path) === '/api/trial/admin' && options?.method === 'POST' &&
          JSON.parse(options.body).action === 'grant') throw new TypeError('lost response');
      return response;
    };
    document.querySelector('.top-up:not([hidden]) input').value = '';
  })()`);
  await type('.top-up:not([hidden]) input', '2');
  await click('.top-up:not([hidden]) [type=submit]');
  await until(() => visible('#retry-action'));
  await cdp('Page.reload', { ignoreCache: true });
  await until(() => visible('#retry-action'));
  assert.equal(Number(await quota()), before + 2);
  await click('#retry-action');
  await until(async () => !(await visible('#retry-action')) && /补发 2 次/.test(await text('#action-feedback')));
  assert.equal(Number(await quota()), before + 2);
  assert.equal(await evaluate('document.querySelector("#audit-list").textContent.split("补发 2 次").length - 1'), 1);
  await confirmToggle();
  await until(async () => /正常/.test(await text('#model-status')));
  assert.match(await text('#audit-list'), /恢复模型调用/);

  stage('AC-017 locked account actions and fetch failure with no false data');
  await click('#account-nav');
  await until(() => visible('#account-preview'));
  assert.equal(await evaluate('[...document.querySelectorAll("#account-preview button")].every(button => button.disabled)'), true);
  const { identifier } = await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `
    (() => {
      const original = window.fetch;
      let fail = true;
      window.fetch = (path, options) => {
        if (String(path) === '/api/trial/admin' && fail) {
          fail = false;
          return Promise.resolve(new Response(JSON.stringify({message: 'fixture unavailable'}), {status: 503}));
        }
        return original(path, options);
      };
    })();
  ` });
  await cdp('Page.reload', { ignoreCache: true });
  await until(() => visible('#retry-load'));
  assert.equal(await visible('#backend'), false);
  assert.match(await text('#load-status'), /加载失败/);
  await click('#retry-load');
  await until(() => visible('#backend'));
  await cdp('Page.removeScriptToEvaluateOnNewDocument', { identifier });
  await click('#trial-nav');
  await until(() => visible('#admin'));
  assert.equal(Number(await quota()), before + 2);
  await close();
  await logout();

  return [{
    id: 'TEST-017', result: 'passed', normalAccountDenied: true, noSampleState: true,
    generationFailures: [1, 2], analysisFailures: [0, 1], pauseResume: true,
    lostGrantResponseChargedOnce: true, loadErrorDoesNotShowFakeData: true,
    accountActionsLocked: true, responsiveWidths: [1360, 700, 390, 320],
  }];
}
