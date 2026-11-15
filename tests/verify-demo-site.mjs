import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

const landingRequirement = '做一个「山野咖啡」的单页官网原型，有品牌介绍、三款咖啡和常见问题。' +
  '菜单能按冷饮和热饮切换，常见问题点击后展开。所有内容在当前页完成。';
const brandRequirement = '做一个「山野咖啡」品牌官网，有首页、咖啡菜单、常见问题三个页面，' +
  '导航可以在三个页面之间切换。首页展示品牌介绍，菜单展示三款咖啡，可按冷饮和热饮筛选，' +
  '常见问题点击后展开。';

export async function verifyDemoSite(ctx) {
  const { cdp, evaluate, until, base, click, visible, appRequest, login, generate, activeTool,
    accountA, root, probeUrl, probeRequests, probeControls, library, evidence, screenshot } = ctx;
  await cdp('Emulation.setDeviceMetricsOverride', {width:1440, height:1000, deviceScaleFactor:1, mobile:false});
  await cdp('Page.navigate', {url:base});
  await login(accountA, true);
  if (await visible('#workspace-guide')) await click('#guide-skip');
  await click('#new-tool');

  assert.equal(await visible('#generation-category'), true, '提交前必须能查看并选择生成类别');
  assert.equal(await visible('#generation-scope-copy'), true, '提交前必须能看到生成范围');
  assert.deepEqual(await evaluate('Array.from(document.querySelector("#generation-category").options, option => option.value)'),
    ['auto', 'ledger', 'program', 'landing', 'brand']);
  const scope = await evaluate('document.querySelector("#generation-scope-copy").textContent');
  assert.match(scope, /单页落地页/);
  assert.match(scope, /多页品牌官网/);
  assert.match(scope, /后续计划.*数据库.*公开发布/);
  const initial = (await appRequest('/api/trial')).body.generation;
  await screenshot('demo-generation-scope');

  // Capture the real form POST response before the task disappears into the saved tool list.
  await evaluate(`(() => {
    window.demoSiteSubmissions = [];
    const original = window.fetch;
    window.fetch = async (path, options) => {
      const response = await original(path, options);
      if (path === '/api/generate' && options?.method === 'POST') {
        window.demoSiteSubmissions.push({ input: JSON.parse(options.body), status: response.status,
          body: await response.clone().json() });
      }
      return response;
    };
  })()`);

  async function submit(requirement, category, previous = '') {
    await generate(requirement);
    const submission = await until(() => evaluate(`window.demoSiteSubmissions.find(
      entry => entry.input.requirement === ${JSON.stringify(requirement)})`));
    assert.equal(submission.input.category, category);
    assert.equal(submission.status, 202, JSON.stringify(submission.body));
    const id = submission.body.task.id;
    const result = await until(async () => {
      const { body:{ task } } = await appRequest('/api/staged/' + id);
      if (['failed','exhausted','unsupported','expired'].includes(task.state)) {
        throw new Error('网站生成未完成 ' + task.stage + ' / ' + task.reason + ' / ' + (task.issue || id));
      }
      return task.state === 'published' && task;
    }, 110000);
    assert.deepEqual(result.completed, ['spec','implementation','assemble']);
    const tool = await activeTool(previous);
    assert.equal(tool.id, result.toolId);
    return { ...tool, task: id };
  }

  const page = (tool, expression) => evaluate(expression, tool.context);
  async function expectLayout(tool, layout) {
    assert.equal(await page(tool, 'document.querySelector("#root-website")?.dataset.layout'), layout);
    assert.match(await page(tool, 'document.title'), /山野咖啡/);
  }
  async function route(tool, target) {
    const selector = '[data-route="' + target + '"]';
    assert.equal(await page(tool, `document.querySelector(${JSON.stringify(selector)})?.checkVisibility()`), true);
    await click(selector, tool.context);
    assert.deepEqual(await page(tool, `Array.from(document.querySelectorAll('[data-page]'))
      .filter(page => page.checkVisibility()).map(page => page.dataset.page)`), [target]);
    assert.ok(await page(tool, `document.querySelector(${JSON.stringify(selector)}).getAttribute('aria-current')`));
  }
  async function checkMenu(tool) {
    const filters = await page(tool, `Array.from(document.querySelectorAll('#site-filters button[data-filter]'),
      button => ({id:button.dataset.filter, label:button.textContent.trim()}))`);
    const cold = filters.find(filter => filter.label.includes('冷'));
    const hot = filters.find(filter => filter.label.includes('热'));
    assert.ok(cold && hot && cold.id !== hot.id, JSON.stringify(filters));
    const cards = await page(tool, `Array.from(document.querySelectorAll('#site-catalog [data-item-filter]'),
      item => ({group:item.dataset.itemFilter, text:item.textContent.trim()}))`);
    assert.equal(cards.length, 3);
    assert.equal(new Set(cards.map(card => card.text)).size, 3);
    assert.ok(cards.every(card => card.text && [cold.id, hot.id].includes(card.group)));
    for (const filter of [cold, hot]) {
      const selector = '#site-filters button[data-filter=' + JSON.stringify(filter.id) + ']';
      await click(selector, tool.context);
      const states = await page(tool, `Array.from(document.querySelectorAll('#site-catalog [data-item-filter]'),
        item => ({group:item.dataset.itemFilter, hidden:item.hidden,
          display:getComputedStyle(item).display, visible:item.getClientRects().length > 0}))`);
      for (const item of states) {
        const matches = item.group === filter.id;
        assert.equal(item.hidden, !matches);
        assert.equal(item.display === 'none', !matches, 'hidden 卡片不可被模型布局样式重新显示');
        assert.equal(item.visible, matches);
      }
    }
    return cards;
  }
  async function checkFAQ(tool) {
    const faq = '#site-faq details';
    const first = await page(tool, `(() => {
      const detail = document.querySelector(${JSON.stringify(faq)});
      return detail && {open:detail.open, question:detail.querySelector('summary').textContent.trim(),
        answer:detail.querySelector('p')?.textContent.trim()};
    })()`);
    assert.ok(first?.question && first?.answer);
    assert.equal(first.open, false);
    await click(faq + ' > summary', tool.context);
    assert.equal(await page(tool, `document.querySelector(${JSON.stringify(faq)}).open`), true);
    await click(faq + ' > summary', tool.context);
    assert.equal(await page(tool, `document.querySelector(${JSON.stringify(faq)}).open`), false);
  }
  async function checkIsolation(tool) {
    const sandbox = await evaluate('document.querySelector("#preview iframe[data-tool]").getAttribute("sandbox")');
    assert.equal(sandbox, 'allow-scripts allow-forms');
    assert.equal(await page(tool, '(() => { try { return Boolean(parent.document); } catch { return false; } })()'), false);
    assert.equal(await page(tool, 'fetch(' + JSON.stringify(probeUrl + '/demo-site-preview') +
      ', {mode:"no-cors"}).then(() => true, () => false)'), false);
    assert.deepEqual(probeRequests, probeControls);
  }

  const landing = await submit(landingRequirement, 'auto');
  await expectLayout(landing, 'landing');
  const landingMenu = await checkMenu(landing);
  await checkFAQ(landing);
  await checkIsolation(landing);
  await screenshot('demo-site-landing');

  await click('#new-tool');
  await evaluate(`(() => {
    const select = document.querySelector('#generation-category');
    select.value = 'brand';
    select.dispatchEvent(new Event('change', {bubbles:true}));
  })()`);
  const brand = await submit(brandRequirement, 'brand', landing.id);
  await expectLayout(brand, 'brand');
  const hostLocation = await evaluate('location.href');
  const initialDocument = await page(brand, 'performance.timeOrigin');
  await route(brand, 'catalog');
  const brandMenu = await checkMenu(brand);
  await route(brand, 'faq');
  await checkFAQ(brand);
  await route(brand, 'home');
  assert.equal(await page(brand, 'performance.timeOrigin'), initialDocument);
  assert.equal(await evaluate('location.href'), hostLocation);
  await checkIsolation(brand);
  await screenshot('demo-site-brand-desktop');

  await cdp('Emulation.setDeviceMetricsOverride', {width:390, height:844, deviceScaleFactor:1, mobile:true});
  await route(brand, 'catalog');
  assert.equal(await page(brand, 'innerWidth <= 390 && document.documentElement.scrollWidth <= document.documentElement.clientWidth'),
    true, '窄屏菜单和导航应留在预览宽度内');
  await checkMenu(brand);
  await route(brand, 'faq');
  await checkFAQ(brand);
  await screenshot('demo-site-brand-narrow');

  await cdp('Page.reload');
  await until(() => evaluate('document.documentElement.dataset.ready === "true"'));
  await click('#library-entry');
  assert.equal((await activeTool()).id, brand.id);
  await click('#tool-list button[data-id=' + JSON.stringify(landing.id) + ']');
  const restoredLanding = await activeTool(brand.id);
  assert.equal(restoredLanding.id, landing.id);
  await expectLayout(restoredLanding, 'landing');
  await click('#tool-list button[data-id=' + JSON.stringify(brand.id) + ']');
  const restoredBrand = await activeTool(landing.id);
  assert.equal(restoredBrand.id, brand.id);
  await expectLayout(restoredBrand, 'brand');
  await route(restoredBrand, 'catalog');
  assert.deepEqual(await checkMenu(restoredBrand), brandMenu);
  await route(restoredBrand, 'faq');
  await checkFAQ(restoredBrand);
  assert.deepEqual(new Set((await library()).items.map(item => item.id)), new Set([landing.id, brand.id]));
  const after = (await appRequest('/api/trial')).body.generation;
  assert.equal(after.used - initial.used, 2);
  assert.equal(after.totalUsed - initial.totalUsed, 4);

  const receipts = (await readFile(join(root, 'runs', 'staged-usage.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  evidence.runs = receipts.filter(item => [landing.task, brand.task].includes(item.task));
  for (const tool of [landing, brand]) {
    const phases = evidence.runs.filter(run => run.task === tool.task);
    assert.deepEqual(phases.map(run => [run.stage, run.status]), [['spec','ok'], ['implementation','ok']]);
    if (process.env.ROOT_STAGED_LIVE === '1') assert.ok(phases.every(run => run.request_id && run.usage?.total_tokens > 0));
  }
  evidence.tests.push({id:'CHG-046', landing:{category:'auto', task:landing.task, tool:landing.id, menu:landingMenu},
    brand:{category:'brand', task:brand.task, tool:brand.id, menu:brandMenu, reopened:true, narrowWidth:390},
    personal:2, dispatched:4});
  await screenshot('demo-site-brand-reopened');

  const lateNavigation = probeUrl + '/demo-site-late-navigation';
  await page(restoredBrand, 'setTimeout(() => { location.href = ' + JSON.stringify(lateNavigation) + '; }, 0)');
  await new Promise(resolve => setTimeout(resolve, 1000));
  assert.deepEqual(probeRequests, probeControls, '预览尝试导航时不能访问外部资源');
  assert.equal(await evaluate('location.href'), hostLocation, '预览尝试导航时工作台仍留在原页');
  evidence.tests.push({id:'CHG-046-late-navigation', outboundBlocked:true, workbenchRetained:true});

  if (process.env.ROOT_STAGED_LIVE !== '1') {
    const escapedRequirement = '转义标签回归：生成一个有首页与产品页的茶壶品牌官网。';
    await click('#new-tool');
    await generate(escapedRequirement);
    const escapedTask = await until(async () => {
      const {body:{tasks}} = await appRequest('/api/staged/history');
      return tasks.find(task => task.requirement === escapedRequirement && task.state === 'failed');
    });
    assert.equal(escapedTask.reason, 'staged_runtime_failed');
    assert.equal(escapedTask.resumable, false);
    await until(() => evaluate('!document.querySelector("#generate").disabled'));
    assert.equal(await evaluate('document.querySelector("#requirement").value'), escapedRequirement);
    assert.equal((await library()).items.length, 2);
    assert.equal((await appRequest('/api/trial')).body.generation.used, after.used);
    evidence.tests.push({id:'website-markup-text', failed:escapedTask.id, requirementRetained:true, toolCount:2});
    const recoveryRequirement = '做一个单页官网原型，有上午和下午可切换的日程。';
    await click('#new-tool');
    await generate(recoveryRequirement);
    const failed = await until(async () => {
      const {body:{tasks}} = await appRequest('/api/staged/history');
      return tasks.find(task => task.requirement === recoveryRequirement && task.state === 'failed');
    });
    assert.equal(failed.reason, 'staged_runtime_failed');
    assert.equal(failed.resumable, false);
    await until(async () => !await visible('#staged-preview')
      && await evaluate('!document.querySelector("#generate").disabled'));
    assert.equal(await evaluate('document.querySelector("#requirement").value'), recoveryRequirement);
    assert.equal((await appRequest('/api/trial')).body.generation.used, after.used);
    assert.equal((await library()).items.length, 2);
    await screenshot('demo-site-invalid-tabs-retained-requirement');

    await click('#generate');
    const saved = await activeTool(restoredBrand.id);
    await click('#afternoon-tab', saved.context);
    assert.equal(await page(saved, 'document.getElementById("afternoon").checkVisibility()'), true);
    assert.equal((await appRequest('/api/trial')).body.generation.used, after.used + 1);
    evidence.tests.push({id:'CHG-046-runtime-recovery', failed:failed.id, recovered:saved.id, personal:1});
  }
}
