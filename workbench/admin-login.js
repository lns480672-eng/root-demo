'use strict';

const form = document.getElementById('admin-login-form');
const username = document.getElementById('admin-username');
const password = document.getElementById('admin-password');
const feedback = document.getElementById('admin-login-feedback');
const submit = document.getElementById('admin-login-submit');

try {
  if (sessionStorage.getItem('root:admin-expired')) {
    sessionStorage.removeItem('root:admin-expired');
    feedback.textContent = '后台登录已过期，请重新登录。';
  }
} catch {
  // Sign-in still works without browser storage.
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submit.disabled) return;
  submit.disabled = true;
  feedback.textContent = '正在登录…';
  try {
    const response = await fetch('/api/admin/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      signal: AbortSignal.timeout(10000),
      body: JSON.stringify({username: username.value, password: password.value}),
    });
    const result = await response.json();
    if (response.ok) {
      form.reset();
      location.reload();
      return;
    }
    feedback.textContent = result.message;
    password.value = '';
    password.focus();
  } catch {
    feedback.textContent = '暂时无法登录，请稍后重试。';
  } finally {
    submit.disabled = false;
  }
});

window.addEventListener('pagehide', () => form.reset());
