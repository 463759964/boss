// ============================================================
// Popup JS - 鎺у埗闈㈡澘閫昏緫
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  bindEvents();
  startStatusPolling();
});

// ---- 鐘舵€佸父閲?----
const STATUS = {
  INACTIVE: 'status-inactive',
  RUNNING: 'status-running',
  PAUSED: 'status-paused',
  ERROR: 'status-error'
};

// ---- DOM 寮曠敤 ----
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

// ===== 鍔犺浇閰嶇疆 =====
async function loadConfig() {
  const result = await chrome.storage.local.get([
    'ba_keywords',
    'ba_filters',
    'ba_delivery',
    'ba_progress',
    'ba_active'
  ]);

  savedConfig = {
    keywords: result.ba_keywords || [],
    filters: result.ba_filters || {
      area: [], salaryMin: '', salaryMax: '',
      experience: [], education: [], companySize: [], jobType: ['fulltime']
    },
    delivery: result.ba_delivery || {
      maxDailyCount: 50, intervalMin: 10, intervalMax: 30,
      customGreeting: '鎮ㄥソ,鎴戝杩欎釜鑱屼綅寰堟劅鍏磋叮銆?, safeModeEnabled: true,
      pauseBetweenBatches: 120000, batchSize: 5
    },
    active: result.ba_active || false,
    progress: result.ba_progress || { delivered: 0, skipped: 0, matched: 0 }
  };

  // 娓叉煋鍏抽敭瀛楁爣绛?  renderKeywords();

  // 濉厖绛涢€夊€?  el.salaryMin.value = savedConfig.filters.salaryMin || '';
  el.salaryMax.value = savedConfig.filters.salaryMax || '';
  el.areaInput.value = (savedConfig.filters.area || []).join(',');

  // 娓叉煋澶嶉€夋缁?  renderCheckboxes('experience', ['涓嶉檺','搴斿眾缁忛獙','1-3骞?,'3-5骞?,'5-10骞?,'10骞翠互涓?]);
  renderCheckboxes('education', ['涓嶉檺','鍒濅腑鍙婁互涓?,'涓笓/涓妧','楂樹腑','澶т笓','鏈','纭曞＋','鍗氬＋']);
  renderCheckboxes('companySize', ['涓嶉渶瑕佽瀺璧?,'澶╀娇杞?,'A杞?,'B杞?,'C杞?,'D杞強浠ヤ笂','涓婂競鍏徃','宸蹭笂绾?,'20-99浜?,'100-499浜?,'500-999浜?,'1000-9999浜?,'10000浜轰互涓?]);

  // 濉厖鎶曢€掕缃?  el.maxDaily.value = savedConfig.delivery.maxDailyCount;
  el.intervalMin.value = savedConfig.delivery.intervalMin;
  el.intervalMax.value = savedConfig.delivery.intervalMax;
  el.batchSize.value = savedConfig.delivery.batchSize;
  el.batchPause.value = Math.round((savedConfig.delivery.pauseBetweenBatches || 120000) / 60000);
  el.safeMode.checked = savedConfig.delivery.safeModeEnabled !== false;
  el.greetingMsg.value = savedConfig.delivery.customGreeting || '';
  el.progressLimit.textContent = savedConfig.delivery.maxDailyCount;

  updateStatus(savedConfig.active, savedConfig.progress);
}

// ===== 娓叉煋鍏抽敭瀛楁爣绛?=====
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

// ===== 娓叉煋澶嶉€夋缁?=====
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

// ===== 缁戝畾浜嬩欢 =====
function bindEvents() {
  // 鍏抽敭璇嶈緭鍏?  el.addKeywordBtn.addEventListener('click', addKeyword);
  el.newKeywordInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') addKeyword();
  });

  // 鍒犻櫎鍏抽敭璇?  el.keywordsContainer.addEventListener('click', (e) => {
    if (e.target.classList.contains('remove')) {
      const idx = parseInt(e.target.dataset.index);
      savedConfig.keywords.splice(idx, 1);
      renderKeywords();
      saveCurrentConfig();
    }
  });

  // 鍒囨崲鎸夐挳
  el.toggleBtn.addEventListener('click', async () => {
    if (savedConfig.active) {
      savedConfig.active = false;
      el.toggleBtn.textContent = '鈻?鍚姩鑷姩鎶曢€?;
      el.toggleBtn.classList.remove('active');
      await chrome.runtime.sendMessage({ action: 'toggleStart', starting: false });
      chrome.storage.local.set({ 'ba_active': false });
    } else {
      savedConfig.active = true;
      el.toggleBtn.textContent = '鈴?鍋滄鑷姩鎶曢€?;
      el.toggleBtn.classList.add('active');
      await chrome.runtime.sendMessage({ action: 'toggleStart', starting: true });
      chrome.storage.local.set({ 'ba_active': true });
    }
    saveCurrentConfig();
    updateStatus(savedConfig.active, savedConfig.progress);
  });

  // 鎵撳紑 Boss 椤甸潰
  el.openBossBtn.addEventListener('click', () => {
    chrome.tabs.create({ url: 'https://www.zhipin.com/' });
  });

  // 瀹炴椂淇濆瓨鎶曢€掕缃彉鏇?  const saveFields = [el.maxDaily, el.intervalMin, el.intervalMax, el.batchSize, el.batchPause, el.safeMode, el.greetingMsg];
  saveFields.forEach(input => {
    input.addEventListener('change', saveCurrentConfig);
    input.addEventListener('input', debouncedSave);
  });

  // 绛涢€夊櫒鍙樻洿
  [el.salaryMin, el.salaryMax, el.areaInput].forEach(input => {
    input.addEventListener('change', saveCurrentConfig);
  });
}

// ===== 娣诲姞鍏抽敭璇?=====
function addKeyword() {
  const val = el.newKeywordInput.value.trim();
  if (!val) return;
  if (savedConfig.keywords.includes(val)) {
    alert('璇ュ叧閿瘝宸插瓨鍦?);
    return;
  }
  savedConfig.keywords.push(val);
  renderKeywords();
  saveCurrentConfig();
  el.newKeywordInput.value = '';
}

// ===== 淇濆瓨褰撳墠閰嶇疆 =====
let saveTimeout = null;
const debouncedSave = () => {
  clearTimeout(saveTimeout);
  saveTimeout = setTimeout(saveCurrentConfig, 800);
};

async function saveCurrentConfig() {
  // 鏀堕泦澶嶉€夋閫変腑鍊?  ['experience', 'education', 'companySize'].forEach(type => {
    const checked = document.querySelectorAll(`#${type}-filters input:checked`);
    savedConfig.filters[type] = Array.from(checked).map(c => c.value);
  });

  // 鏀堕泦鏂囧瓧杈撳叆鍊?  savedConfig.filters.salaryMin = el.salaryMin.value;
  savedConfig.filters.salaryMax = el.salaryMax.value;
  savedConfig.filters.area = el.areaInput.value.split(/[,锛宂/).map(a => a.trim()).filter(Boolean);

  // 鏀堕泦鎶曢€掕缃?  const batchPauseMin = parseInt(el.batchPause.value) || 2;
  savedConfig.delivery.maxDailyCount = parseInt(el.maxDaily.value) || 50;
  savedConfig.delivery.intervalMin = parseInt(el.intervalMin.value) || 10;
  savedConfig.delivery.intervalMax = parseInt(el.intervalMax.value) || 30;
  savedConfig.delivery.batchSize = parseInt(el.batchSize.value) || 5;
  savedConfig.delivery.pauseBetweenBatches = batchPauseMin * 60000;
  savedConfig.delivery.safeModeEnabled = el.safeMode.checked;
  savedConfig.delivery.customGreeting = el.greetingMsg.value;
  savedConfig.delivery.maxDailyCount = savedConfig.delivery.maxDailyCount;

  // 鏇存柊 UI
  el.progressLimit.textContent = savedConfig.delivery.maxDailyCount;

  // 鎸佷箙鍖?  await chrome.storage.local.set({
    'ba_keywords': savedConfig.keywords,
    'ba_filters': savedConfig.filters,
    'ba_delivery': savedConfig.delivery
  });
}

// ===== 鏇存柊鐘舵€佹樉绀?=====
function updateStatus(active, progress) {
  const banner = el.statusBanner;
  banner.className = 'status-banner ' + (active ? STATUS.RUNNING : STATUS.INACTIVE);
  
  el.statusText.textContent = active ? '杩愯涓? : '宸插仠姝?;
  el.statusSub.textContent = active ? `浠婃棩宸叉姇閫?${progress.delivered || 0}` : '鐐瑰嚮鍚姩寮€濮嬭嚜鍔ㄦ姇閫?;
  
  el.statMatched.textContent = progress.matched || 0;
  el.statSkipped.textContent = progress.skipped || 0;
  el.statDelivered.textContent = progress.delivered || 0;

  // 杩涘害鏉?  const limit = savedConfig?.delivery?.maxDailyCount || 50;
  const pct = Math.min(100, ((progress.delivered || 0) / limit) * 100);
  el.progressFill.style.width = pct + '%';

  // 鍒囨崲鎸夐挳
  if (active && !el.toggleBtn.classList.contains('active')) {
    el.toggleBtn.textContent = '鈴?鍋滄鑷姩鎶曢€?;
    el.toggleBtn.classList.add('active');
  } else if (!active && el.toggleBtn.classList.contains('active')) {
    el.toggleBtn.textContent = '鈻?鍚姩鑷姩鎶曢€?;
    el.toggleBtn.classList.remove('active');
  }
}

// ===== 鐘舵€佽疆璇?=====
function startStatusPolling() {
  setInterval(async () => {
    const data = await chrome.storage.local.get([
      'ba_active',
      'ba_progress',
      'ba_delivery'
    ]);
    savedConfig.active = data.ba_active || false;
    savedConfig.progress = data.ba_progress || {};
    if (data.ba_delivery) {
      savedConfig.delivery = data.ba_delivery;
      el.progressLimit.textContent = savedConfig.delivery.maxDailyCount || 50;
    }
    updateStatus(savedConfig.active, savedConfig.progress);
  }, 3000);
}

