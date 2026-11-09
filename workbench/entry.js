'use strict';

// These product-owned examples never enter the generated tool storage or model paths.
const quickTemplates = {
  board: {
    title: '交付跟进板',
    requirement: '做一个交付跟进板，记录事项、负责人、截止日和状态，可以标记完成。',
    items: ['确认演示范围 · 小林 · 11 月 9 日', '准备样例数据 · 小陈 · 11 月 10 日', '复核体验入口 · 小王 · 11 月 11 日'],
    doneLabel: '已完成',
  },
  list: {
    title: '读书清单',
    requirement: '做一个读书清单，可以标记读完，重新打开时保留记录。',
    items: ['街道的美学', '海边的卡夫卡', '设计中的设计'],
    doneLabel: '已读',
  },
  budget: {
    title: 'AA 分账',
    requirement: '做一个 AA 分账计算器，根据总金额和人数计算人均费用。',
  },
};

function renderEntryTemplate(templateKey) {
  const template = quickTemplates[templateKey];
  document.getElementById('template-prompt').textContent = template.requirement;
  for (const button of document.querySelectorAll('[data-entry-template]')) {
    button.setAttribute('aria-pressed', String(button.dataset.entryTemplate === templateKey));
  }
}

function openSample(templateKey) {
  const template = quickTemplates[templateKey];
  const sample = document.getElementById('sample-preview');
  const content = document.getElementById('sample-content');
  const result = document.getElementById('sample-result');
  document.getElementById('sample-title').textContent = template.title;
  const markup = document.getElementById(templateKey === 'budget' ? 'sample-budget' : 'sample-checklist');
  content.replaceChildren(markup.content.cloneNode(true));

  if (templateKey === 'budget') {
    const form = content.querySelector('form');
    const amount = form.querySelector('#sample-amount');
    const people = form.querySelector('#sample-people');
    result.textContent = '输入总金额和人数后计算。';
    form.addEventListener('submit', event => {
      event.preventDefault();
      result.textContent = '人均 ¥' + (amount.valueAsNumber / people.valueAsNumber).toFixed(2);
    });
  } else {
    const list = content.querySelector('ul');
    for (const name of template.items) {
      const item = document.createElement('li');
      const label = document.createElement('label');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.setAttribute('data-sample-toggle', '');
      label.append(checkbox, name);
      item.append(label);
      list.append(item);
    }
    const update = () => {
      const done = list.querySelectorAll('input:checked').length;
      result.textContent = template.doneLabel + ' ' + done + '/' + template.items.length;
    };
    list.addEventListener('change', update);
    update();
  }
  sample.hidden = false;
}

function closeSample() {
  document.getElementById('sample-preview').hidden = true;
  document.getElementById('sample-content').replaceChildren();
  document.getElementById('sample-result').textContent = '';
}
