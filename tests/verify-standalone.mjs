import assert from 'node:assert/strict';

export async function verifyStandalone(ctx) {
  const { base, cdp, evaluate, until, login, logout, generate, activeTool,
    click, pointerClick, type, visible, appRequest, accountA, accountB, anotherTab, screenshot, evidence, probeUrl, probeRequests, probeControls, live, readingRequirement, recordRun } = ctx;
  await cdp('Page.navigate', { url: base });
  await login(accountA, true);
  if (await visible('#guide-skip')) await click('#guide-skip');
  assert.equal(await visible('#open-standalone'), false);
  await generate(live ? readingRequirement : '做一个读书清单，能添加书名并保存。');
  const original = await activeTool();
  if (live) await recordRun(original);
  if (await visible('#open-generated-tool')) await click('#open-generated-tool');
  const apiPath = '/api/tools/' + original.id;
  const frameText = context => evaluate('document.querySelector("#list").textContent', context);
  async function add(book, context) {
    await type('#book', book, context);
    await click('form button', context);
    await until(async () => JSON.stringify((await appRequest(apiPath)).body.entries).includes(book));
  }

  await add('工作台书目', original.context);
  const url = await evaluate('document.querySelector("#open-standalone")?.href');
  assert.equal(url, base + '/#tool/' + original.id, '详情的独立入口须指向当前保存工具');
  for (const width of [1440, 390]) {
    const metrics = { width, height: 850, deviceScaleFactor: 1, mobile: width === 390 };
    await cdp('Emulation.setDeviceMetricsOverride', metrics);
    assert.equal(await visible('#open-standalone'), true);
    await anotherTab(() => pointerClick('#open-standalone'), async () => {
      await cdp('Emulation.setDeviceMetricsOverride', metrics);
      const tool = await activeTool();
      assert.equal(tool.id, original.id);
      assert.equal(await evaluate('location.href'), url);
      assert.equal(await evaluate('window.opener === null'), true);
      assert.equal(await evaluate('document.querySelector(".workspace-sidebar").checkVisibility()'), false);
      assert.equal(await evaluate('document.querySelector("#standalone-back").href'), base + '/');
      assert.equal(await evaluate('document.querySelector("#preview iframe").sandbox.contains("allow-same-origin")'), false);
      assert.equal(await evaluate('(() => { try { return Boolean(parent.document); } catch { return false; } })()', tool.context), false);
      assert.match(await frameText(tool.context), /工作台书目/);
      if (probeUrl) {
        assert.equal(await evaluate('fetch(' + JSON.stringify(probeUrl + '/standalone') +
          ', {mode:"no-cors"}).then(() => true, () => false)', tool.context), false);
        assert.deepEqual(probeRequests, probeControls);
      }
      if (width === 390) await add('独立页书目', tool.context);
      await screenshot('standalone-' + width);
    });
    assert.equal(await evaluate('location.href'), base + '/');
    assert.equal((await activeTool()).id, original.id);
  }
  await evaluate('window.fixtureOriginalFrame = document.querySelector("#preview iframe[data-tool]")');
  await click('#tool-list button[data-id="' + original.id + '"]');
  await until(() => evaluate('!window.fixtureOriginalFrame.isConnected && phase === "idle"'));
  assert.match(await frameText((await activeTool()).context), /独立页书目/);
  if (!live) {
    const v1 = (await appRequest(apiPath)).body;
    const {body: pending} = await appRequest(apiPath + '/revise', {requirement:'增加已读筛选', version:1});
    assert.equal(pending.version, 2);
    assert.equal((await appRequest('/api/tools/' + pending.runId)).status, 404);
    await anotherTab(() => pointerClick('#open-standalone'), async () => {
      const previous = await activeTool();
      assert.equal((await appRequest(apiPath)).body.version, 1);
      assert.equal(await evaluate('Boolean(document.querySelector("#read-filter"))', previous.context), false);
    });
    assert.equal((await appRequest(apiPath + '/publish', {
      runId:pending.runId, revision:pending.revision, entries:v1.entries,
    })).status, 200);
    await anotherTab(() => pointerClick('#open-standalone'), async () => {
      const current = await activeTool();
      assert.equal(current.id, original.id);
      assert.equal((await appRequest(apiPath)).body.version, 2);
      assert.equal(await evaluate('Boolean(document.querySelector("#read-filter"))', current.context), true);
    });
  }
  const expected = (await appRequest(apiPath)).body;
  const ownerMarker = await evaluate('session.marker');
  await logout();
  await cdp('Page.navigate', { url });
  await until(() => visible('#auth'));
  assert.equal(await visible('#preview iframe'), false);
  assert.equal((await appRequest(apiPath, undefined, ownerMarker)).status, 401);
  await login(accountB, true);
  await until(() => visible('#standalone-error'));
  assert.equal(await visible('#preview iframe'), false);
  const denied = await appRequest(apiPath);
  assert.equal(denied.status, 404);
  assert.equal(Object.hasOwn(denied.body, 'html'), false);
  assert.equal((await appRequest(apiPath + '/data', {
    entries: [], revision: expected.revision, version: expected.version,
  })).status, 404);
  await logout();
  await login(accountA);
  assert.equal((await activeTool()).id, original.id);
  assert.deepEqual((await appRequest(apiPath)).body.entries, expected.entries);
  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  assert.match(await frameText((await activeTool()).context), /独立页书目/);
  await click('#standalone-back');
  await until(() => evaluate('location.hash === "" && document.documentElement.dataset.ready === "true"'));
  await cdp('Page.navigate', {url:base + '/workbench#tool/' + original.id});
  assert.equal((await activeTool()).id, original.id);
  assert.equal(await evaluate('document.querySelector("#standalone-back").href'), base + '/workbench');
  await click('#standalone-back');
  await until(() => evaluate('location.pathname === "/workbench" && location.hash === "" && document.documentElement.dataset.ready === "true"'));
  evidence.tests.push({id:'STANDALONE-01/02/03/04', tool:original.id, sharedRecords:true, pendingHidden:!live, nextAppliedVersion:!live, twoAccounts:true});
}

export async function verifyCatalog(ctx) {
  const { base, cdp, evaluate, until, click, pointerClick, type, visible, activeTool,
    anotherTab, screenshot, evidence, appRequest, seed, username, password } = ctx;
  await cdp('Page.navigate', {url:base});
  await until(() => visible('#auth'));
  await type('#username', username);
  await type('#password', password);
  await click('#auth-submit');
  await until(() => evaluate('document.documentElement.dataset.ready === "true" && Boolean(session)'));
  await click('#library-entry');

  const checks = {
    '清单': async frame => {
      assert.match(await evaluate('document.querySelector("#list").textContent', frame), /反馈08 验收书目/);
      assert.equal(await evaluate('document.querySelector("#add").textContent', frame), '添加');
      assert.equal((await appRequest(seed.pending.task)).body.task.state, 'complete');
    },
    '台账': async frame => assert.match(await evaluate('document.querySelector("#records").textContent', frame), /反馈08 验收雨伞/),
    '看板': async frame => assert.match(await evaluate('document.querySelector("#record-board").textContent', frame), /反馈08 验收公司/),
    '计算器': async (frame, tool) => {
      await click('#add', frame);
      assert.equal(await evaluate('document.querySelector("#total").textContent', frame), '5');
      assert.deepEqual((await appRequest('/api/tools/' + tool.id)).body.entries, []);
    },
    '导表': async frame => assert.match(await evaluate('document.querySelector("#table-records").textContent', frame), /反馈08 验收事项/),
    '单页网站': async frame => {
      await click('#site-faq summary', frame);
      assert.equal(await evaluate('document.querySelector("#site-faq details").open', frame), true);
    },
    '多页网站': async frame => {
      await click('[data-route="catalog"]', frame);
      assert.equal(await evaluate('document.querySelector("#site-catalog").checkVisibility()', frame), true);
      await click('[data-route="faq"]', frame);
      assert.equal(await evaluate('document.querySelector("#site-catalog").checkVisibility()', frame), false);
      assert.equal(await evaluate('document.querySelector("#site-faq").checkVisibility()', frame), true);
    },
  };
  for (const [label, tool] of Object.entries(seed.catalog)) {
    const selector = '#tool-list button[data-id="' + tool.id + '"]';
    await until(() => evaluate('Boolean(document.querySelector(' + JSON.stringify(selector + ':enabled') + '))'));
    await click(selector);
    await until(async () => await evaluate('document.querySelector("#preview iframe[data-tool]")?.dataset.tool') === tool.id);
    await until(() => visible('#open-standalone'));
    await anotherTab(() => pointerClick('#open-standalone'), async () => {
      const opened = await activeTool();
      assert.equal(opened.id, tool.id);
      const snapshot = (await appRequest('/api/tools/' + tool.id)).body;
      assert.equal(snapshot.version, tool.version);
      assert.deepEqual(snapshot.entries, tool.entries);
      await checks[label](opened.context, tool);
      await screenshot('独立页-' + label);
    });
    evidence.tests.push({id:'STANDALONE-01/03', label, tool:tool.id, passed:true});
  }
}
