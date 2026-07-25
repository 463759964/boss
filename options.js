// ============================================================
// Options Page JS - 高级设置页面逻辑
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
  loadAllSettings();
  bindEvents();
});

const OPTIONS_KEYS = {
  keywords: 'boss_autoapplier_keywords',
  filters: 'boss_autoapplier_filters',
  delivery: 'boss_autoapplier_delivery'
};

// DOM refs
const el = {
  keywordInput: document.getElementById('opt-keyword-input'),
  addKeywordBtn: document.getElementById('opt-add-keyword'),
  keywordsList: document.getElementById('opt-keywords'),
  areas: document.getElementById('opt-areas'),
  excludeAreas: document.getElementById('opt-exclude-areas'),
  salaryMin: document.getElementById('opt-salary-min'),
  salaryMax: document.getElementById('opt-salary-max'),
  experienceChecks: document.getElementById('opt-experience-checks'),
  educationChecks: document.getElementById('opt-education-checks'),
  companySizeChecks: document.getElementById('opt-company-size-checks'),
  industries: document.getElementById('opt-industries'),
  maxDaily: document.getElementById('opt-max-daily'),
  intervalMin: document.getElementById('opt-interval-min'),
  intervalMax: document.getElementById('opt-interval-max'),
  batchSize: document.getElementById('opt-batch-size'),
  batchPause: document.getElementById('opt-batch-pause'),
  safeMode: document.getElementById('opt-safe-mode'),
  greeting: document.getElementById('opt-greeting'),
  saveBtn: document.getElementById('opt-save'),
  saveStatus: document.getElementById('opt-save-status'),
  resetBtn: document.getElementById('opt-reset-all'),
  clearDeliveredBtn: document.getElementById('opt-clear-delivered')
};

let allKeywords = [];
let currentFilters = {};
let currentDelivery = {};

// ===== 加载所有设置 =====
async function loadAllSettings() {
  const result = await chrome.storage.local.get([
    OPTIONS_KEYS.keywords,
    OPTIONS_KEYS.filters,
    OPTIONS_KEYS.delivery
  ]);

  allKeywords = result[OPTIONS_KEYS.keywords] || [];
  currentFilters = result[OPTIONS_KEYS.filters] || {};
  currentDelivery = result[OPTIONS_KEYS.delivery] || {};

  // 渲染关键字列表
  renderOptionKeywords();

  // 填充基础字段
  el.areas.value = (currentFilters.area || []).join(', ');
  el.salaryMin.value = currentFilters.salaryMin || '';
  el.salaryMax.value = currentFilters.salaryMax || '';
  el.industries.value = (currentFilters.industry || []).join(', ');

  // 投递设置
  el.maxDaily.value = currentDelivery.maxDailyCount ?? 50;
  el.intervalMin.value = currentDelivery.intervalMin ?? 10;
  el.intervalMax.value = currentDelivery.intervalMax ?? 30;
  el.batchSize.value = currentDelivery.batchSize ?? 5;
  el.batchPause.value = Math.round((currentDelivery.pauseBetweenBatches || 120000) / 60000);
  el.safeMode.checked = currentDelivery.safeModeEnabled !== false;
  el.greeting.value = currentDelivery.customGreeting || '';

  // 渲染复选框组
  renderOptionCheckboxes('experience', ['不限','应届经验','1-3年','3-5年','5-10年','10年以上'], currentFilters.experience || []);
  renderOptionCheckboxes('education', ['不限','初中及以下','中专/中技','高中','大专','本科','硕士','博士'], currentFilters.education || []);
  renderOptionCheckboxes('companySize', [
    '20-99人','100-499人','500-999人','1000-9999人','10000人以上',
    '不需要融资','天使轮','A轮','B轮','C轮','D轮及以上','上市公司','已上线'
  ], currentFilters.companySize || []);
}

// ===== 渲染关键字列表 =====
function renderOptionKeywords() {
  el.keywordsList.innerHTML = '';
  allKeywords.forEach((kw, i) => {
    const span = document.createElement('span');
    span.className = 'tag-item';
    span.innerHTML = `${escHtml(kw)} <span class="remove" data-index="${i}">&times;</span>`;
    el.keywordsList.appendChild(span);
  });
}

function escHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

// ===== 渲染复选框组 =====
function renderOptionCheckboxes(type, options, selected) {
  const container = document.getElementById(`opt-${type}-checks`);
  if (!container) return;
  
  options.forEach(opt => {
    const label = document.createElement('label');
    label.className = 'cb-item' + (selected.includes(opt) ? ' selected' : '');
    label.innerHTML = `<input type="checkbox" data-type="${type}" value="${opt}" ${selected.includes(opt) ? 'checked' : ''}>${escHtml(opt)}`;
    
    label.addEventListener('click', () => {
      label.classList.toggle('selected');
      label.querySelector('input').checked = !label.querySelector('input').checked;
    });
    
    container.appendChild(label);
  });
}

// ===== 绑定事件 =====
function bindEvents() {
  // 添加关键字
  el.addKeywordBtn.addEventListener('click', () => {
    const val = el.keywordInput.value.trim();
    if (!val || allKeywords.includes(val)) return;
    allKeywords.push(val);
    renderOptionKeywords();
    el.keywordInput.value = '';
  });

  el.keywordInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') el.addKeywordBtn.click();
  });

  // 删除关键字
  el.keywordsList.addEventListener('click', (e) => {
    if (e.target.classList.contains('remove')) {
      allKeywords.splice(parseInt(e.target.dataset.index), 1);
      renderOptionKeywords();
    }
  });

  // 保存按钮
  el.saveBtn.addEventListener('click', saveAllSettings);

  // 重置所有
  el.resetBtn.addEventListener('click', () => {
    if (confirm('确定要重置所有设置吗？此操作不可撤销。')) {
      chrome.storage.local.clear();
      alert('已重置所有设置，请刷新页面。');
      location.reload();
    }
  });

  // 清除投递记录
  el.clearDeliveredBtn.addEventListener('click', () => {
    if (confirm('确定清除本地已投递记录？下次运行时会重新投递相同的职位。')) {
      localStorage.removeItem('boss_delivered_jobs');
      alert('已清除！');
    }
  });
}

// ===== 保存所有设置 =====
async function saveAllSettings() {
  // 收集复选框选中值
  ['experience', 'education', 'companySize'].forEach(type => {
    const checked = document.querySelectorAll(`#opt-${type}-checks .cb-item.selected input`);
    currentFilters[type] = Array.from(checked).map(c => c.value);
  });

  // 基础筛选
  currentFilters.area = el.areas.value.split(/[,，]/).map(a => a.trim()).filter(Boolean);
  currentFilters.salaryMin = el.salaryMin.value;
  currentFilters.salaryMax = el.salaryMax.value;
  currentFilters.industry = el.industries.value.split(/[,，]/).map(i => i.trim()).filter(Boolean);

  // 投递设置
  currentDelivery.maxDailyCount = parseInt(el.maxDaily.value) || 50;
  currentDelivery.intervalMin = parseInt(el.intervalMin.value) || 10;
  currentDelivery.intervalMax = parseInt(el.intervalMax.value) || 30;
  currentDelivery.batchSize = parseInt(el.batchSize.value) || 5;
  currentDelivery.pauseBetweenBatches = (parseInt(el.batchPause.value) || 2) * 60000;
  currentDelivery.safeModeEnabled = el.safeMode.checked;
  currentDelivery.customGreeting = el.greeting.value;

  await chrome.storage.local.set({
    [OPTIONS_KEYS.keywords]: allKeywords,
    [OPTIONS_KEYS.filters]: currentFilters,
    [OPTIONS_KEYS.delivery]: currentDelivery
  });

  // 显示保存成功提示
  el.saveStatus.textContent = '✅ 设置已保存!';
  el.saveStatus.classList.add('show');
  setTimeout(() => el.saveStatus.classList.remove('show'), 2000);
}
