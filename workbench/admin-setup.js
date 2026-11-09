'use strict';

const form = document.getElementById('setup-form');
const field = id => document.getElementById(id);
const feedback = field('setup-feedback');
const submit = field('setup-submit');

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submit.disabled) return;
  const password = field('setup-password').value;
  if (password !== field('setup-password-again').value) {
    feedback.textContent = '两次输入的密码不一致。';
    field('setup-password-again').focus();
    return;
  }
  submit.disabled = true;
  feedback.textContent = '正在设置管理员…';
  try {
    const response = await fetch('/api/admin/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(10000),
      body: JSON.stringify({
        code: field('setup-code').value,
        username: field('setup-username').value,
        password,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      feedback.textContent = result.message;
      return;
    }
    form.reset();
    history.replaceState(null, '', '/admin#invites');
    location.reload();
  } catch {
    feedback.textContent = '暂时无法确认设置结果。若初始化码已失效，请重新打开后台，用刚设置的账号登录。';
  } finally {
    submit.disabled = false;
  }
});

window.addEventListener('pagehide', () => form.reset());
