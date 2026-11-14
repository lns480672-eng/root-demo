import assert from 'node:assert/strict';

export async function verifyAdmin({evaluate, click, pointerClick, type, until, cdp, login, logout, visible,
  appRequest, screenshot, stage}) {
  const admin = 'qa-trial-admin';
  const member = 'qa-trial-member';
  const adminPath = '/api/trial/admin';
  const text = selector => evaluate('document.querySelector(' + JSON.stringify(selector) + ').textContent');
  const style = (selector, property) => evaluate(
    `getComputedStyle(document.querySelector(${JSON.stringify(selector)}))[${JSON.stringify(property)}]`);
  const open = async () => {
    if (!await visible('#admin-entry')) await click('#account-name');
    await until(() => visible('#admin-entry'));
    await click('#admin-entry');
    await until(async () => await visible('#backend') || await visible('#admin-login-form'));
    if (await visible('#admin-login-form')) {
      assert.equal(await style('#admin-login-submit', 'backgroundColor'), 'rgb(35, 58, 118)');
      assert.equal(await style('#admin-username', 'borderColor'), 'rgb(175, 190, 212)');
      assert.equal(await style('.login-panel', 'borderTopColor'), 'rgb(35, 58, 118)');
      await screenshot('admin-login');
      await type('#admin-username', admin);
      await type('#admin-password', 'root-browser-test-only!');
      await click('#admin-login-submit');
    }
    await until(() => visible('#backend'));
  };
  const close = async () => {
    await click('#back-to-workbench');
    await until(() => visible('#account-name'));
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
  await click('#admin-logout');
  await until(() => visible('#admin-login-form'));
  await close();
  await logout();

  stage('AC-017 normal account denies administrative URL and populates real endpoint stats');
  await login(member);
  assert.equal(await visible('#admin-entry'), false);
  assert.equal(await evaluate('fetch("/admin#accounts").then(response => response.status)'), 200);
  assert.equal((await appRequest(adminPath)).status, 401);
  assert.equal((await appRequest('/api/generate', {requirement: '做一个读书清单'})).status, 200);
  assert.equal((await appRequest('/api/generate', {requirement: '回归：模型超时'})).status, 502);
  const offered = await appRequest('/api/assistant/analyze', {
    step: 'start', idea: '做一个项目跟进工具，可以记录进度并保存', requestId: crypto.randomUUID(), roundId: crypto.randomUUID(),
  });
  assert.equal(offered.status, 200);
  assert.equal((await appRequest('/api/assistant/claim-draft', {offerId: offered.body.offerId})).status, 200);
  await logout();

  stage('AC-017 DOM reflects two separately calculated real failure rates');
  await login(admin);
  await open();
  const stats = await evaluate(`fetch('/api/trial/admin', {headers: {'X-Root-Session': identity.marker}})
    .then(response => response.json())`);
  assert.equal(stats.generation.failureRate, 0.5);
  assert.equal(stats.analysis.failureRate, 0);
  assert.match(await text('#generation-failure'), /50%.*1 失败 \/ 2 已结束/);
  assert.match(await text('#analysis-failure'), /0%.*0 失败 \/ 1 已结束/);
  assert.match(await text('#analysis-total'), /1.*次/);
  assert.match(await text('#account-list'), /需求梳理 · 今日.*1 条/s);
  assert.equal(await style('.admin-nav', 'backgroundColor'), 'rgb(237, 241, 248)');
  assert.equal(await style('.admin-nav [aria-current="page"]', 'backgroundColor'), 'rgb(226, 234, 248)');
  await screenshot('admin-desktop');
  for (const width of [700, 390, 320]) {
    await cdp('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: true });
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    assert.equal(await evaluate(`
      document.querySelector('.admin-nav').getBoundingClientRect().right
      <= document.querySelector('.admin-content').getBoundingClientRect().left`), true);
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
    let dropGrant = true;
    window.fetch = async (path, options) => {
      const response = await original(path, options);
      if (dropGrant && String(path) === '/api/trial/admin' && options?.method === 'POST' &&
          JSON.parse(options.body).action === 'grant') {
        dropGrant = false;
        throw new TypeError('lost response');
      }
      return response;
    };
    document.querySelector('.top-up:not([hidden]) input').value = '';
  })()`);
  await type('.top-up:not([hidden]) input', '2');
  await click('.top-up:not([hidden]) [type=submit]');
  await until(() => visible('#retry-action'));
  const relogged = await evaluate(`fetch('/api/admin/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: ${JSON.stringify(admin)}, password: 'root-browser-test-only!'}),
  }).then(response => response.status)`);
  assert.equal(relogged, 200);
  await click('#retry-action');
  await until(async () => /会话或权限已变化/.test(await text('#action-feedback')));
  assert.equal(await visible('#retry-action'), true, '失效会话无法确认上一次操作，必须保留原请求号');
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

  stage('generation settings frontend contract');
  const liveSettings = process.env.ROOT_ADMIN_SETTINGS_HTTP === '1';
  if (!liveSettings) {
  await evaluate(`(() => {
    const original = window.fetch;
    const defaults = {sourceBytes:2457600, revisionContextBytes:4096000, outputTokens:393216, timeoutSeconds:300};
    const maximums = {sourceBytes:5000000, revisionContextBytes:7000000, outputTokens:400000, timeoutSeconds:600};
    let settings = {...defaults};
    window.generationSettingsWrites = [];
    window.restoreSettingsFetch = () => { window.fetch = original; };
    window.fetch = async (path, options) => {
      if (path !== '/api/admin/generation-settings') return original(path, options);
      if (!options.headers['X-Root-Session']) return Response.json({code:'session_changed'}, {status:403});
      if (options.method === 'POST') {
        const body = JSON.parse(options.body);
        window.generationSettingsWrites.push(body);
        settings = body.reset ? {...defaults} : body.settings;
      }
      return Response.json({settings, defaults, maximums});
    };
  })()`);
  }
  const settingsFromServer = () => evaluate(`fetch('/api/admin/generation-settings', {
    headers: {'X-Root-Session': identity.marker},
  }).then(response => response.json())`);
  await click('#generation-settings-nav');
  await until(() => evaluate('!document.querySelector("#generation-settings-fields").disabled'));
  assert.equal(await visible('#admin'), false);
  assert.equal(await evaluate('document.querySelector("#setting-timeoutSeconds").valueAsNumber'), 300);
  const limits = await settingsFromServer();
  const maximumTimeout = limits.maximums.timeoutSeconds;
  assert.equal(await evaluate('document.querySelector("#setting-timeoutSeconds").max'), String(maximumTimeout));
  await evaluate('document.querySelector("#setting-revisionContextBytes").value = 1');
  await click('#generation-settings-form [type=submit]');
  assert.match(await text('#generation-settings-feedback'), /大于或等于/);
  if (liveSettings) assert.deepEqual((await settingsFromServer()).settings, limits.settings);
  else assert.deepEqual(await evaluate('window.generationSettingsWrites'), []);
  await evaluate('document.querySelector("#setting-revisionContextBytes").value = 4096000; document.querySelector("#setting-timeoutSeconds").value = ' + (maximumTimeout + 1));
  await click('#generation-settings-form [type=submit]');
  assert.ok((await text('#generation-settings-feedback')).includes(maximumTimeout.toLocaleString('zh-CN')));
  if (liveSettings) assert.deepEqual((await settingsFromServer()).settings, limits.settings);
  else assert.deepEqual(await evaluate('window.generationSettingsWrites'), []);
  await evaluate('document.querySelector("#setting-timeoutSeconds").value = 450');
  await click('#generation-settings-form [type=submit]');
  await until(async () => (await text('#generation-settings-feedback')).includes('已保存'));
  if (liveSettings) {
    assert.equal((await settingsFromServer()).settings.timeoutSeconds, 450);
    await cdp('Page.reload', {ignoreCache:true});
    await until(() => evaluate('!document.querySelector("#generation-settings-fields").disabled'));
    assert.equal(await evaluate('document.querySelector("#setting-timeoutSeconds").valueAsNumber'), 450);
  } else {
    assert.equal(await evaluate('window.generationSettingsWrites[0].settings.timeoutSeconds'), 450);
  }
  await screenshot('admin-generation-settings');
  await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
  await screenshot('admin-generation-settings-390');
  await cdp('Emulation.setDeviceMetricsOverride', {width:1360,height:980,deviceScaleFactor:1,mobile:false});
  await click('#generation-settings-reset');
  await until(async () => (await text('#generation-settings-feedback')).includes('已恢复默认值'));
  if (liveSettings) assert.deepEqual((await settingsFromServer()).settings, limits.defaults);
  else assert.deepEqual(await evaluate('window.generationSettingsWrites[1]'), {reset:true});
  assert.equal(await evaluate('document.querySelector("#setting-timeoutSeconds").valueAsNumber'), 300);
  if (!liveSettings) await evaluate('window.restoreSettingsFetch()');

  stage('AC-017 user management is a locked preview');
  assert.match(await text('#security-nav'), /密码修改/);
  assert.equal(await evaluate('document.querySelector(".admin-nav").lastElementChild.id'), 'account-nav');
  assert.match(await text('#account-nav'), /用户管理.*暂未开放/);
  await click('#account-nav');
  await until(() => visible('#account-preview'));
  assert.match(await text('#account-preview'), /暂未开放/);
  assert.match(await text('#account-preview'), /示例账号.*reviewer-01/s);
  assert.doesNotMatch(await text('#account-preview'), /qa-trial-(admin|member)/);
  assert.equal(await evaluate('document.querySelector("#account-nav").getAttribute("aria-current")'), 'page');
  assert.equal(await visible('#admin'), false);
  assert.equal(await visible('#invite-module'), false);
  assert.deepEqual(await evaluate(`[...document.querySelectorAll('#account-preview button')]
    .map(button => [button.textContent, button.disabled])`), [['删除账号', true]]);
  await evaluate(`(() => {
    const original = window.fetch;
    window.previewWrites = [];
    window.fetch = (path, options) => {
      if (options?.method && options.method !== 'GET') window.previewWrites.push(String(path));
      return original(path, options);
    };
  })()`);
  for (const width of [1360, 390]) {
    await cdp('Emulation.setDeviceMetricsOverride', {width,height:844,deviceScaleFactor:1,mobile:width===390});
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    await screenshot('admin-user-management-' + width);
  }
  await cdp('Emulation.setDeviceMetricsOverride', {width:1360,height:980,deviceScaleFactor:1,mobile:false});
  await pointerClick('#account-preview button');
  assert.deepEqual(await evaluate('window.previewWrites'), []);
  await screenshot('account-preview-desktop');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
  await screenshot('account-preview-390');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1360, height: 980, deviceScaleFactor: 1, mobile: false });

  stage('AC-017 fetch failure with no false data');
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
  assert.equal(await visible('#account-preview'), true, '重新加载仍打开原账号预览');
  await click('#trial-nav');
  await until(() => visible('#admin'));
  assert.equal(Number(await quota()), before + 2);
  stage('AC-019 administrator issues and copies an invitation');
  await click('#invite-nav');
  await until(() => visible('#invite-module'));
  assert.match(await text('#invite-policy'), /72 小时.*一次/);
  const ttlHours = 72;
  await screenshot('invite-module-empty');
  await cdp('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
  await screenshot('invite-module-390');
  await cdp('Emulation.setDeviceMetricsOverride', {width: 1360, height: 980, deviceScaleFactor: 1, mobile: false});
  const issuedCount = () => evaluate(`fetch('/api/trial/admin', {headers: {'X-Root-Session': identity.marker}})
    .then(response => response.json()).then(state => state.audit.filter(row => row.action === 'issue_invite').length)`);
  const issuedBefore = await issuedCount();
  await evaluate(`(() => {
    const original = window.fetch;
    let drop = true;
    window.fetch = async (path, options) => {
      const response = await original(path, options);
      if (drop && String(path) === '/api/admin/invites' && options?.method === 'POST') {
        drop = false;
        throw new TypeError('lost issuance response');
      }
      return response;
    };
  })()`);
  stage('AC-019 lost issuance response');
  await click('#invite-generate');
  await until(async () => /确认上次发码/.test(await text('#invite-generate')));
  stage('AC-019 restore pending issuance after reload');
  await cdp('Page.reload', {ignoreCache: true});
  await until(async () => (await visible('#invite-module')) && /确认上次发码/.test(await text('#invite-generate')));
  stage('AC-019 recover status without reissuing');
  await click('#invite-generate');
  await until(async () => /原码无法恢复/.test(await text('#invite-feedback')));
  assert.equal(await issuedCount(), issuedBefore + 1);
  assert.equal(await visible('#invite-result'), false);

  stage('AC-019 show newly issued invite');
  const origin = await evaluate('location.origin');
  await cdp('Browser.grantPermissions', {origin, permissions:['clipboardReadWrite','clipboardSanitizedWrite']});
  const now = Date.now();
  await click('#invite-generate');
  await until(() => visible('#invite-result'));
  const expiry = await evaluate('Date.parse(document.querySelector("#invite-expires").dateTime)');
  const ttlMs = ttlHours * 60 * 60 * 1000;
  assert.ok(expiry - now >= ttlMs - 1000 && expiry - now < ttlMs + 10000);
  await evaluate('document.querySelector("#issued-code").style.visibility = "hidden"');
  await screenshot('invite-expiration');
  await evaluate('document.querySelector("#issued-code").style.removeProperty("visibility")');
  stage('AC-019 write actual Chrome clipboard');
  await cdp('Page.bringToFront');
  await pointerClick('#invite-copy');
  const copyFeedback = await until(async () => {
    const feedback = await text('#invite-feedback');
    return /已复制|手动复制/.test(feedback) && feedback;
  });
  assert.match(copyFeedback, /已复制/);
  assert.equal(await evaluate('navigator.clipboard.readText().then(code => code === document.querySelector("#issued-code").value && code.length > 0)'), true);
  await click('#invite-copy');
  assert.equal(await issuedCount(), issuedBefore + 2);
  stage('AC-019 manual clipboard fallback');
  await evaluate(`Object.defineProperty(navigator.clipboard, 'writeText', {
    configurable: true, value: () => Promise.reject(new DOMException('blocked', 'NotAllowedError')),
  })`);
  await click('#invite-copy');
  await until(async () => /手动复制/.test(await text('#invite-feedback')));
  assert.equal(await evaluate(`(() => {
    const input = document.querySelector('#issued-code');
    return document.activeElement === input && input.selectionEnd - input.selectionStart === input.value.length;
  })()`), true);
  assert.equal(await issuedCount(), issuedBefore + 2);

  // The code stays in test memory and is never emitted in screenshots or test results.
  stage('AC-019 register a fresh session with the copied code');
  const copied = await evaluate('navigator.clipboard.readText()');
  await click('#admin-logout');
  await until(() => visible('#admin-login-form'));
  await close();
  await logout();
  await click('#auth-mode');
  await type('#invite', copied);
  await type('#username', 'qa-issued-reviewer');
  await type('#password', 'root-browser-test-only!');
  await click('#auth-submit');
  await until(() => evaluate('document.querySelector("#account-name")?.textContent === "qa-issued-reviewer"'));
  assert.equal(await visible('#admin-entry'), false);
  assert.equal(await evaluate('fetch("/admin#invites").then(response => response.status)'), 200);
  assert.equal((await appRequest('/api/admin/invites', {requestId: crypto.randomUUID()})).status, 401);
  await logout();
  await click('#auth-mode');
  await type('#invite', copied);
  stage('AC-019 reject reuse of the copied code');
  await type('#username', 'qa-issued-repeat');
  await type('#password', 'root-browser-test-only!');
  await click('#auth-submit');
  await until(() => visible('#invite-error'));
  assert.match(await text('#invite-error'), /已使用|已用完/);
  await evaluate(`(() => {
    for (const id of ['invite','username','password']) document.getElementById(id).value = '';
  })()`);
  await evaluate("navigator.clipboard.writeText('')");
  await click('#auth-mode');

  return [{
    id: 'TEST-017', result: 'passed', normalAccountDenied: true, noSampleState: true,
    generationFailures: [1, 2], analysisFailures: [0, 1], pauseResume: true,
    lostGrantResponseChargedOnce: true, idempotencySurvivesSessionChange: true, loadErrorDoesNotShowFakeData: true,
    generationSettingsContract: true, generationSettingsHttp: liveSettings, accountActionsLocked: true, accountPreviewReloaded: true, accountPreviewWrites: 0, responsiveWidths: [1360, 700, 390, 320],
  }, {
    id: 'TEST-019', result: 'passed', actualClipboard: true, manualCopyFallback: true,
    survivesResponseLoss: true, singleUse: true, ordinaryAccountDenied: true, ttlHours,
  }];
}
