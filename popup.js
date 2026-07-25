// ============================================================
// Popup JS - 控制面板逻辑
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  bindEvents();
  startStatusPolling();
});

// ---- 状态常量 ----
const STATUS = {
  INACTIVE: 'status-inactive',
  RUNNING: 'status-running',
  PAUSED: 'status-paused',
  ERROR: 'status-error'
};

// ---- DOM 引用 ----
const el = {
  statusBanner: document.getElementById('status-banner'),
  statusText: document.getElementById('status-text'),
  statusSub: document.getElementById('status-sub'),
  progressFill: document.getElementById('progress-fill'),
  progressCount: document.getElementById('progress-count'),
  progressLimit: document.getElementById('progress-limit'),
  statMatched: document.getElementById('stat-matched'),
  statSkipped: document.getElementById('stat-skipped'),
  statDelivered: document.getElementById('stat-delivered'),
  toggleBtn: document.getElementById('toggle-btn'),
  openBossBtn: document.getElementById('open-boss-btn'),
  keywordsContainer: document.getElementById('keywords-container'),
  newKeywordInput: document.getElementById('new-keyword'),
  addKeywordBtn: document.getElementById('add-keyword-btn'),
  salaryMin: document.getElementById('salary-min'),
  salaryMax: document.getElementById('salary-max'),
  areaInput: document.getElementById('area-input'),
  maxDaily: document.getElementById('max-daily'),
  intervalMin: document.getElementById('interval-min'),
  intervalMax: document.getElementById('interval-max'),
  batchSize: document.getElementById('batch-size'),
  batchPause: document.getElementById('batch-pause'),
  safeMode: document.getElementById('safe-mode'),
  greetingMsg: document.getElementById('greeting-msg')
};

let savedConfig = null;

// ===== 加载配置 =====
async function loadConfig() {
  const result = await chrome.storage.local.get([
    'boss_autoapplier_keywords',
    'boss_autoapplier_filters',
    'boss_autoapplier_delivery',
    'boss_autoapplier_progress',
    'boss_autoapplier_active'
  ]);

  savedConfig = {
    keywords: result.boss_autoapplier_keywords || [],
    filters: result.boss_autoapplier_filters || {
      area: [], salaryMin: '', salaryMax: '',
      experience: [], education: [], companySize: [], jobType: ['fulltime']
    },
    delivery: result.boss_autoapplier_delivery || {
      maxDailyCount: 50, intervalMin: 10, intervalMax: 30,
      customGreeting: '您好,我对这个职位很感兴趣。', safeModeEnabled: true,
      pauseBetweenBatches: 120000, batchSize: 5
    },
    active: result.boss_autoapplier_active || false,
    progress: result.boss_autoapplier_progress || { delivered: 0, skipped: 0, matched: 0 }
  };

  // 渲染关键字标签
  renderKeywords();

  // 填充筛选值
  el.salaryMin.value = savedConfig.filters.salaryMin || '';
  el.salaryMax.value = savedConfig.filters.salaryMax || '';
  el.areaInput.value = (savedConfig.filters.area || []).join(',');

  // 渲染复选框组
  renderCheckboxes('experience', ['不限','应届经验','1-3年','3-5年','5-10年','10年以上']);
  renderCheckboxes('education', ['不限','初中及以下','中专/中技','高中','大专','本科','硕士','博士']);
  renderCheckboxes('companySize', ['不需要融资','天使轮','A轮','B轮','C轮','D轮及以上','上市公司','已上线','20-99人','100-499人','500-999人','1000-9999人','10000人以上']);

  // 填充投递设置
  el.maxDaily.value = savedConfig.delivery.maxDailyCount;
  el.intervalMin.value = savedConfig.delivery.intervalMin;
  el.intervalMax.value = savedConfig.delivery.intervalMax;
  el.batchSize.value = savedConfig.delivery.batchSize;
  el.batchPause.value = Math.round((savedConfig.delivery.pauseBetweenBatches || 120000) / 60000);
  el.safeMode.checked = savedConfig.delivery.safeModeEnabled !== false;
  el.greetingMsg.value = savedConfig.delivery.customGreeting || '';
  el.progressLimit.textContent = savedConfig.delivery.maxDailyCount;

  updateStatus(savedConfig.active, savedConfig.progress);
}

// ===== 渲染关键字标签 =====
function renderKeywords() {
  el.keywordsContainer.innerHTML = '';
  savedConfig.keywords.forEach((kw, i) => {
    const tag = document.createElement('span');
    tag.className = 'keyword-tag';
    tag.innerHTML = `${escHtml(kw)} <span class="remove" data-index="${i}">&times;</span>`;
    el.keywordsContainer.appendChild(tag);
  });
}

function escHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ===== 渲染复选框组 =====
function renderCheckboxes(type, options) {
  const containerId = type + '-filters';
  const container = document.getElementById(containerId);
  if (!container) return;

  const selected = savedConfig.filters[type] || [];

  options.forEach(opt => {
    const label = document.createElement('label');
    label.className = 'check-item' + (selected.includes(opt) ? ' checked' : '');
    label.innerHTML = `
      <input type="checkbox" data-type="${type}" value="${opt}" ${selected.includes(opt) ? 'checked' : ''}>
      <span>${opt}</span>
    `;
    label.addEventListener('click', (e) => {
      e.preventDefault();
      const cb = label.querySelector('input');
      cb.checked = !cb.checked;
      label.classList.toggle('checked', cb.checked);
      saveCurrentConfig();
    });
    container.appendChild(label);
  });
}

// ===== 绑定事件 =====
function bindEvents() {
  // 关键词输入
  el.addKeywordBtn.addEventListener('click', addKeyword);
  el.newKeywordInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') addKeyword();
  });

  // 删除关键词
  el.keywordsContainer.addEventListener('click', (e) => {
    if (e.target.classList.contains('remove')) {
      const idx = parseInt(e.target.dataset.index);
      savedConfig.keywords.splice(idx, 1);
      renderKeywords();
      saveCurrentConfig();
    }
  });

  // 切换按钮
  el.toggleBtn.addEventListener('click', async () => {
    if (savedConfig.active) {
      savedConfig.active = false;
      el.toggleBtn.textContent = '▶ 启动自动投递';
      el.toggleBtn.classList.remove('active');
      await chrome.runtime.sendMessage({ action: 'toggleStart', starting: false });
      chrome.storage.local.set({ 'boss_autoapplier_active': false });
    } else {
      savedConfig.active = true;
      el.toggleBtn.textContent = '⏸ 停止自动投递';
      el.toggleBtn.classList.add('active');
      await chrome.runtime.sendMessage({ action: 'toggleStart', starting: true });
      chrome.storage.local.set({ 'boss_autoapplier_active': true });
    }
    saveCurrentConfig();
    updateStatus(savedConfig.active, savedConfig.progress);
  });

  // 打开 Boss 页面
  el.openBossBtn.addEventListener('click', () => {
    chrome.tabs.create({ url: 'https://www.zhipin.com/' });
  });

  // 实时保存投递设置变更
  const saveFields = [el.maxDaily, el.intervalMin, el.intervalMax, el.batchSize, el.batchPause, el.safeMode, el.greetingMsg];
  saveFields.forEach(input => {
    input.addEventListener('change', saveCurrentConfig);
    input.addEventListener('input', debouncedSave);
  });

  // 筛选器变更
  [el.salaryMin, el.salaryMax, el.areaInput].forEach(input => {
    input.addEventListener('change', saveCurrentConfig);
  });
}

// ===== 添加关键词 =====
function addKeyword() {
  const val = el.newKeywordInput.value.trim();
  if (!val) return;
  if (savedConfig.keywords.includes(val)) {
    alert('该关键词已存在');
    return;
  }
  savedConfig.keywords.push(val);
  renderKeywords();
  saveCurrentConfig();
  el.newKeywordInput.value = '';
}

// ===== 保存当前配置 =====
let saveTimeout = null;
const debouncedSave = () => {
  clearTimeout(saveTimeout);
  saveTimeout = setTimeout(saveCurrentConfig, 800);
};

async function saveCurrentConfig() {
  // 收集复选框选中值
  ['experience', 'education', 'companySize'].forEach(type => {
    const checked = document.querySelectorAll(`#${type}-filters input:checked`);
    savedConfig.filters[type] = Array.from(checked).map(c => c.value);
  });

  // 收集文字输入值
  savedConfig.filters.salaryMin = el.salaryMin.value;
  savedConfig.filters.salaryMax = el.salaryMax.value;
  savedConfig.filters.area = el.areaInput.value.split(/[,，]/).map(a => a.trim()).filter(Boolean);

  // 收集投递设置
  const batchPauseMin = parseInt(el.batchPause.value) || 2;
  savedConfig.delivery.maxDailyCount = parseInt(el.maxDaily.value) || 50;
  savedConfig.delivery.intervalMin = parseInt(el.intervalMin.value) || 10;
  savedConfig.delivery.intervalMax = parseInt(el.intervalMax.value) || 30;
  savedConfig.delivery.batchSize = parseInt(el.batchSize.value) || 5;
  savedConfig.delivery.pauseBetweenBatches = batchPauseMin * 60000;
  savedConfig.delivery.safeModeEnabled = el.safeMode.checked;
  savedConfig.delivery.customGreeting = el.greetingMsg.value;
  savedConfig.delivery.maxDailyCount = savedConfig.delivery.maxDailyCount;

  // 更新 UI
  el.progressLimit.textContent = savedConfig.delivery.maxDailyCount;

  // 持久化
  await chrome.storage.local.set({
    'boss_autoapplier_keywords': savedConfig.keywords,
    'boss_autoapplier_filters': savedConfig.filters,
    'boss_autoapplier_delivery': savedConfig.delivery
  });
}

// ===== 更新状态显示 =====
function updateStatus(active, progress) {
  const banner = el.statusBanner;
  banner.className = 'status-banner ' + (active ? STATUS.RUNNING : STATUS.INACTIVE);
  
  el.statusText.textContent = active ? '运行中' : '已停止';
  el.statusSub.textContent = active ? `今日已投递 ${progress.delivered || 0}` : '点击启动开始自动投递';
  
  el.statMatched.textContent = progress.matched || 0;
  el.statSkipped.textContent = progress.skipped || 0;
  el.statDelivered.textContent = progress.delivered || 0;

  // 进度条
  const limit = savedConfig?.delivery?.maxDailyCount || 50;
  const pct = Math.min(100, ((progress.delivered || 0) / limit) * 100);
  el.progressFill.style.width = pct + '%';

  // 切换按钮
  if (active && !el.toggleBtn.classList.contains('active')) {
    el.toggleBtn.textContent = '⏸ 停止自动投递';
    el.toggleBtn.classList.add('active');
  } else if (!active && el.toggleBtn.classList.contains('active')) {
    el.toggleBtn.textContent = '▶ 启动自动投递';
    el.toggleBtn.classList.remove('active');
  }
}

// ===== 状态轮询 =====
function startStatusPolling() {
  setInterval(async () => {
    const data = await chrome.storage.local.get([
      'boss_autoapplier_active',
      'boss_autoapplier_progress',
      'boss_autoapplier_delivery'
    ]);
    savedConfig.active = data.boss_autoapplier_active || false;
    savedConfig.progress = data.boss_autoapplier_progress || {};
    if (data.boss_autoapplier_delivery) {
      savedConfig.delivery = data.boss_autoapplier_delivery;
      el.progressLimit.textContent = savedConfig.delivery.maxDailyCount || 50;
    }
    updateStatus(savedConfig.active, savedConfig.progress);
  }, 3000);
}
