// ============================================================
// Popup JS - 控制面板逻辑
// 负责: 配置加载/保存、状态轮询、Boss页面筛选字段提取、投递日志展示
// ============================================================

document.addEventListener('DOMContentLoaded', function() {
  loadConfig();
  bindEvents();
  pollStatus();
  // 监听 content script 发来的筛选字段数据
  chrome.runtime.onMessage.addListener(function(msg) {
    if (msg.action === 'filterDataUpdate') {
      handleFilterData(msg.data);
    }
  });
});

var STATUS = {
  INACTIVE: 'status-inactive',
  RUNNING: 'status-running',
  PAUSED: 'status-paused',
  ERROR: 'status-error'
};

// ---- DOM refs ----
var el = {
  statusBanner: document.getElementById('status-banner'),
  statusText: document.getElementById('status-text'),
  statDelivered: document.getElementById('stat-delivered'),
  statMatched: document.getElementById('stat-matched'),
  statSkipped: document.getElementById('stat-skipped'),
  statLimit: document.getElementById('stat-limit'),
  progressFill: document.getElementById('progress-fill'),
  toggleBtn: document.getElementById('toggle-btn'),
  keywordsContainer: document.getElementById('keywords-container'),
  newKeywordInput: document.getElementById('new-keyword'),
  addKeywordBtn: document.getElementById('add-keyword-btn'),
  salaryMin: document.getElementById('salary-min'),
  salaryMax: document.getElementById('salary-max'),
  experienceFilters: document.getElementById('experience-filters'),
  educationFilters: document.getElementById('education-filters'),
  intervalMin: document.getElementById('interval-min'),
  intervalMax: document.getElementById('interval-max'),
  batchSize: document.getElementById('batch-size'),
  batchPause: document.getElementById('batch-pause'),
  safeMode: document.getElementById('safe-mode'),
  greetingMsg: document.getElementById('greeting-msg'),
  bossFilterSource: document.getElementById('boss-filter-source'),
  jobLogContainer: document.getElementById('job-log-container'),
  jobLogEmpty: document.getElementById('job-log-empty')
};

var config = null;
var liveLog = []; // 实时投递日志，最多保留50条
var currentAreas = []; // 从Boss页面提取的区域列表
var currentSalaryRanges = []; // 当前页薪资段

// ===== 加载配置 =====
function loadConfig() {
  chrome.storage.local.get([
    'ba_keywords', 'ba_filters', 'ba_delivery',
    'ba_progress', 'ba_active'
  ], function(items) {
    config = {
      keywords: items.ba_keywords || [],
      filters: items.ba_filters || {
        area: [], salaryMin: '', salaryMax: '',
        experience: [], education: []
      },
      delivery: items.ba_delivery || {
        maxDailyCount: 50, intervalMinSec: 10, intervalMaxSec: 30,
        pauseBetweenBatchesMin: 2, safeModeEnabled: true,
        customGreeting: '您好,我对这个职位很感兴趣。', batchSize: 5
      },
      active: items.ba_active || false,
      progress: items.ba_progress || { delivered: 0, skipped: 0, matched: 0 }
    };

    renderKeywords();
    populateFields();
    renderCheckboxes();
    updateUI();
  });
}

// ===== 渲染关键字标签 =====
function renderKeywords() {
  el.keywordsContainer.innerHTML = '';
  config.keywords.forEach(function(kw, i) {
    var tag = document.createElement('span');
    tag.className = 'keyword-tag';
    tag.innerHTML = escHtml(kw) + ' <span class="remove" data-i="' + i + '">&times;</span>';
    el.keywordsContainer.appendChild(tag);
  });
}

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ===== 填充输入框 =====
function populateFields() {
  el.salaryMin.value = config.filters.salaryMin || '';
  el.salaryMax.value = config.filters.salaryMax || '';
  el.intervalMin.value = config.delivery.intervalMinSec;
  el.intervalMax.value = config.delivery.intervalMaxSec;
  el.batchSize.value = config.delivery.batchSize || 5;
  el.batchPause.value = config.delivery.pauseBetweenBatchesMin || 2;
  el.safeMode.checked = config.delivery.safeModeEnabled !== false;
  el.greetingMsg.value = config.delivery.customGreeting || '';
}

// ===== 渲染复选框 =====
function renderCheckboxes() {
  // 经验
  var expOptions = ['不限','应届经验','1-3年','3-5年','5-10年','10年以上'];
  renderCheckboxGrid(el.experienceFilters, 'experience', expOptions);

  // 学历
  var eduOptions = ['不限','初中及以下','中专/中技','高中','大专','本科','硕士','博士'];
  renderCheckboxGrid(el.educationFilters, 'education', eduOptions);

  // 区域（动态从Boss页面加载）
  updateAreaDisplay();
}

function renderCheckboxGrid(container, type, options) {
  container.innerHTML = '';
  var selected = config.filters[type] || [];
  options.forEach(function(opt) {
    var label = document.createElement('label');
    label.className = 'check-item' + (selected.indexOf(opt) !== -1 ? ' checked' : '');
    label.setAttribute('data-type', type);
    label.setAttribute('data-value', opt);
    label.innerHTML = '<input type="checkbox" value="' + escHtml(opt) + '"' +
      (selected.indexOf(opt) !== -1 ? ' checked' : '') + '>';
    label.appendChild(document.createTextNode(opt));
    label.addEventListener('click', function(e) {
      e.preventDefault();
      label.classList.toggle('checked');
      label.querySelector('input').checked = !label.querySelector('input').checked;
      saveNow();
    });
    container.appendChild(label);
  });
}

// ===== Boss页面筛选字段处理 =====
function handleFilterData(data) {
  if (!data) return;
  currentAreas = data.areas || [];
  currentSalaryRanges = data.salaryRanges || [];

  // 更新区域显示
  updateAreaDisplay();

  // 标记已加载
  el.bossFilterSource.className = 'filter-source-box source-loaded';
  el.bossFilterSource.innerHTML =
    '<div>✅ Boss直聘筛选条件已提取</div>' +
    '<div class="filter-chips">' +
    (currentAreas.length > 0 ? ' 区域: ' + currentAreas.map(function(a) { return '<span class="filter-chip">' + a + '</span>'; }).join(' ') : '') +
    (currentSalaryRanges.length > 0 ? ' 薪资: ' + currentSalaryRanges.map(function(s) { return '<span class="filter-chip">' + s + '</span>'; }).join(' ') : '') +
    '</div>';
}

function updateAreaDisplay() {
  // 将区域作为可点击标签显示在Boss筛选信息区
  if (currentAreas.length > 0) {
    // 如果用户已经从Boss页面看到了区域,他们可以通过salary输入框下方的手动筛选来添加
    // 这里我们提供一个快捷方式: 点击 chips 直接设置
  }
}

// ===== 绑定事件 =====
function bindEvents() {
  // 关键词
  el.addKeywordBtn.addEventListener('click', addKeyword);
  el.newKeywordInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') addKeyword();
  });

  el.keywordsContainer.addEventListener('click', function(e) {
    if (e.target.classList.contains('remove')) {
      config.keywords.splice(parseInt(e.target.dataset.i), 1);
      renderKeywords();
      saveLater();
    }
  });

  // 切换按钮
  el.toggleBtn.addEventListener('click', function() {
    var action = config.active ? 'stopDelivery' : 'startDelivery';
    chrome.runtime.sendMessage({ action: action }, function(resp) {
      config.active = !config.active;
      if (config.active) {
        el.toggleBtn.textContent = '⏸ 停止自动投递';
        el.toggleBtn.classList.add('active');
        el.statusText.textContent = '运行中';
        el.statusBanner.className = 'status-banner status-running';
      } else {
        el.toggleBtn.textContent = '▶ 启动自动投递';
        el.toggleBtn.classList.remove('active');
        el.statusText.textContent = '已停止';
        el.statusBanner.className = 'status-banner status-inactive';
      }
      chrome.storage.local.set({ 'ba_active': config.active });
      saveNow();
    });
  });

  // 输入框变更
  var fields = [el.salaryMin, el.salaryMax, el.intervalMin, el.intervalMax,
                el.batchSize, el.batchPause, el.greetingMsg];
  fields.forEach(function(f) {
    f.addEventListener('change', saveNow);
  });
  el.safeMode.addEventListener('change', saveNow);
}

// ===== 添加关键词 =====
function addKeyword() {
  var val = el.newKeywordInput.value.trim();
  if (!val || config.keywords.indexOf(val) !== -1) return;
  config.keywords.push(val);
  renderKeywords();
  saveLater();
  el.newKeywordInput.value = '';
}

// ===== 保存配置 =====
var _saveTimer = null;
function saveLater() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(saveNow, 600);
}

function saveNow() {
  // 收集复选框选中值
  ['experience', 'education'].forEach(function(type) {
    var container = type === 'experience' ? el.experienceFilters : el.educationFilters;
    var checked = container.querySelectorAll('.check-item input:checked');
    config.filters[type] = Array.from(checked).map(function(c) { return c.value; });
  });

  config.filters.salaryMin = el.salaryMin.value;
  config.filters.salaryMax = el.salaryMax.value;
  config.delivery.intervalMinSec = parseInt(el.intervalMin.value) || 10;
  config.delivery.intervalMaxSec = parseInt(el.intervalMax.value) || 30;
  config.delivery.batchSize = parseInt(el.batchSize.value) || 5;
  config.delivery.pauseBetweenBatchesMin = parseInt(el.batchPause.value) || 2;
  config.delivery.safeModeEnabled = el.safeMode.checked;
  config.delivery.customGreeting = el.greetingMsg.value;

  chrome.storage.local.set({
    'ba_keywords': config.keywords,
    'ba_filters': config.filters,
    'ba_delivery': config.delivery
  });
}

// ===== 更新UI =====
function updateUI() {
  if (!config) return;
  var p = config.progress;
  var d = config.delivery;

  el.statDelivered.textContent = p.delivered || 0;
  el.statMatched.textContent = p.matched || 0;
  el.statSkipped.textContent = p.skipped || 0;
  el.statLimit.textContent = d.maxDailyCount || 50;

  var pct = Math.min(100, ((p.delivered || 0) / (d.maxDailyCount || 50)) * 100);
  el.progressFill.style.width = pct + '%';

  var isRunning = config.active && (p.delivered > 0 || p.matched > 0);
  if (isRunning) {
    el.statusText.textContent = '运行中';
    el.statusBanner.className = 'status-banner status-running';
    el.toggleBtn.textContent = '⏸ 停止自动投递';
    el.toggleBtn.classList.add('active');
  } else {
    el.statusText.textContent = '已停止';
    el.statusBanner.className = 'status-banner status-inactive';
    el.toggleBtn.textContent = '▶ 启动自动投递';
    el.toggleBtn.classList.remove('active');
  }
}

// ===== 轮询状态 =====
function pollStatus() {
  setInterval(function() {
    chrome.storage.local.get(['ba_active', 'ba_progress', 'ba_delivery'], function(data) {
      if (data.ba_active !== undefined) config.active = data.ba_active;
      if (data.ba_progress) config.progress = data.ba_progress;
      if (data.ba_delivery) config.delivery = data.ba_delivery;
      updateUI();
    });
  }, 3000);
}

// ===== 投递结果回调 — 从 background 或 content 获取 =====
chrome.runtime.onMessage.addListener(function(msg) {
  if (msg.action === 'deliveryResult') {
    addLogEntry(msg.result);
  }
});

// ===== 添加日志条目 =====
function addLogEntry(result) {
  if (!result) return;
  liveLog.unshift(result);
  if (liveLog.length > 50) liveLog.pop();
  renderLog();
}

function renderLog() {
  // 清空容器
  el.jobLogContainer.innerHTML = '';
  if (liveLog.length === 0) {
    el.jobLogContainer.appendChild(el.jobLogEmpty);
    return;
  }

  liveLog.forEach(function(entry) {
    var div = document.createElement('div');
    div.className = 'job-entry';

    var statusClass = entry.type === 'skip' ? 'skip' :
                      entry.type === 'fail' ? 'fail' :
                      entry.type === 'info' ? 'info' : 'match';

    var html = '<div class="job-entry-header">' +
      '<div class="job-entry-status ' + statusClass + '"></div>' +
      '<span class="job-entry-title">' + escHtml(entry.title || entry.reason) + '</span>' +
      '<span class="job-entry-time">' + (entry.time || '') + '</span>' +
      '</div>';

    if (entry.meta) {
      html += '<div class="job-entry-meta">' +
        '<span>' + escHtml(entry.meta) + '</span>' +
        '</div>';
    }
    if (entry.reason) {
      html += '<div class="job-entry-reason">' + escHtml(entry.reason) + '</div>';
    }

    div.innerHTML = html;
    el.jobLogContainer.appendChild(div);
  });
}
