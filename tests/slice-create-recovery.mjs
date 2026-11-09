import assert from 'node:assert/strict';

export async function verifyCreateRecovery({
  evaluate, click, type, until, cdp, activeTool, appRequest, library, screenshot, stage,
}) {
  async function generationUsage() {
    const response = await appRequest('/api/trial');
    assert.equal(response.status, 200);
    const { used, totalUsed } = response.body.generation;
    assert.ok(Number.isInteger(used) && Number.isInteger(totalUsed));
    return { used, totalUsed };
  }

  const trace = () => evaluate('window.createRecoveryTrace');
  const restoredScenarios = [];
  let previous = await activeTool();
  for (const scenario of ['automatic', 'manual']) {
    const manual = scenario === 'manual';
    const beforeUsage = await generationUsage();
    const beforeTools = await library();
    stage('ROOT-GEN-IA-02 首次保存成功但确认丢失 · ' + scenario);
    await evaluate(`(() => {
      const realFetch = window.fetch;
      const trace = window.createRecoveryTrace = {
        generations: 0, commitAttempts: [], committed: [], readbackAttempts: [],
      };
      window.blockCreateReadback = ${manual};
      window.fetch = async (input, init) => {
        const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
        const method = init?.method || input.method || 'GET';
        if (path === '/api/generate' && method === 'POST') trace.generations++;
        const commit = path.match(/^\\/api\\/tools\\/(run-[a-z0-9_]{8})\\/commit$/);
        const resolve = path.match(/^\\/api\\/tools\\/(run-[a-z0-9_]{8})\\/resolve-commit$/);
        if (resolve) {
          trace.readbackAttempts.push(resolve[1]);
          if (window.blockCreateReadback) throw new TypeError('fixture: 首次保存核对暂不可用');
        }
        if (commit && method === 'POST') trace.commitAttempts.push(commit[1]);
        const response = await realFetch(input, init);
        if (commit && method === 'POST' && response.status === 200) {
          trace.committed.push(commit[1]);
          throw new TypeError('fixture: 已收到首次保存 HTTP 200，但丢弃确认');
        }
        return response;
      };
      window.restoreCreateRecoveryFetch = () => { window.fetch = realFetch; };
    })()`);

    let candidateId;
    try {
      await click('#new-tool');
      await type('#requirement', '做一个读书清单，可以输入书名并保存。测试标记：' + scenario);
      await click('#generate');
      candidateId = await until(async () => (await trace()).committed[0]);

      if (manual) {
        stage('ROOT-GEN-IA-02 核对不可用时禁用重复生成，并等待手动同步');
        await until(async () => (await trace()).readbackAttempts.length > 0 && await evaluate(
          'document.querySelector("#sync-tool")?.checkVisibility() && !document.querySelector("#sync-tool").disabled'));
        assert.equal(await evaluate('document.querySelector("#generate").disabled'), true);
        assert.equal((await library()).latest, candidateId, '服务端已提交，页面尚未完成核对');
        assert.deepEqual(await generationUsage(), {
          used: beforeUsage.used + 1, totalUsed: beforeUsage.totalUsed + 1,
        });
        await click('#generate');
        assert.equal((await trace()).generations, 1, '状态未核对前不能再次调用生成');
        await screenshot('create-commit-unknown');
        await evaluate('window.blockCreateReadback = false');
        await click('#sync-tool');
      }

      stage('ROOT-GEN-IA-02 核对后打开原候选且不重复扣次 · ' + scenario);
      const tool = await activeTool(previous.id);
      assert.equal(tool.id, candidateId, '从实际提交恢复原候选 ID');
      assert.equal(await evaluate('document.querySelector("h1").textContent', tool.context), '读书清单');
      await until(() => evaluate(`Boolean(document.querySelector(${JSON.stringify(
        '#tool-list button[data-id="' + candidateId + '"]',
      )}))`));
      const currentTools = await library();
      assert.equal(currentTools.latest, candidateId);
      assert.deepEqual(currentTools.items.map(item => item.id).sort(),
        [...beforeTools.items.map(item => item.id), candidateId].sort());
      assert.deepEqual(await generationUsage(), {
        used: beforeUsage.used + 1, totalUsed: beforeUsage.totalUsed + 1,
      });
      const captured = await trace();
      assert.equal(captured.generations, 1);
      assert.deepEqual([...new Set(captured.commitAttempts)], [candidateId]);
      assert.deepEqual([...new Set(captured.readbackAttempts)], [candidateId]);
      restoredScenarios.push({ scenario, id: candidateId, generationRequests: captured.generations,
        lostAcknowledgements: captured.committed.length, personalDebit: 1, globalDebit: 1 });
      previous = tool;
      await screenshot('create-commit-recovered-' + scenario);
    } finally {
      await evaluate('window.restoreCreateRecoveryFetch()');
    }
  }

  stage('ROOT-GEN-IA-02 恢复后的工具仍可保存，并能刷新找回');
  const marker = '同步后保存的书';
  await type('#book', marker, previous.context);
  await click('form button', previous.context);
  await until(async () => {
    const response = await appRequest('/api/tools/' + previous.id);
    return response.status === 200 && JSON.stringify(response.body.entries).includes(marker);
  });
  const previousNavigation = await evaluate('performance.timeOrigin');
  await cdp('Page.reload', { ignoreCache: true });
  await until(() => evaluate(`performance.timeOrigin !== ${previousNavigation} && document.documentElement.dataset.ready === 'true'`));
  const reloaded = await activeTool();
  assert.equal(reloaded.id, previous.id);
  assert.match(await evaluate('document.querySelector("#list").innerText', reloaded.context), /同步后保存的书/);
  return { id: 'ROOT-GEN-IA-02', result: 'passed', scenarios: restoredScenarios, saveAfterRecovery: true };
}
