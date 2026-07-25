// ============================================================
// Options Page JS - 楂樼骇璁剧疆椤甸潰閫昏緫
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
  loadAllSettings();
  bindEvents();
});

const STORAGE_KEYS = {
  keywords: 'ba_keywords',
  filters: 'ba_filters',
  delivery: 'ba_delivery'
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

// ===== 鍔犺浇鎵€鏈夎缃?=====
async function loadAllSettings() {
  const result = await chrome.storage.local.get([
    STORAGE_KEYS.keywords,
    STORAGE_KEYS.filters,
    STORAGE_KEYS.delivery
  ]);

  allKeywords = result[STORAGE_KEYS.keywords] || [];
  currentFilters = result[STORAGE_KEYS.filters] || {};
  currentDelivery = result[STORAGE_KEYS.delivery] || {};

  // 娓叉煋鍏抽敭瀛楀垪琛?  renderOptionKeywords();

  // 濉厖鍩虹瀛楁
  el.areas.value = (currentFilters.area || []).join(', ');
  el.salaryMin.value = currentFilters.salaryMin || '';
  el.salaryMax.value = currentFilters.salaryMax || '';
  el.industries.value = (currentFilters.industry || []).join(', ');

  // 鎶曢€掕缃?  el.maxDaily.value = currentDelivery.maxDailyCount ?? 50;
  el.intervalMin.value = currentDelivery.intervalMin ?? 10;
  el.intervalMax.value = currentDelivery.intervalMax ?? 30;
  el.batchSize.value = currentDelivery.batchSize ?? 5;
  el.batchPause.value = Math.round((currentDelivery.pauseBetweenBatches || 120000) / 60000);
  el.safeMode.checked = currentDelivery.safeModeEnabled !== false;
  el.greeting.value = currentDelivery.customGreeting || '';

  // 娓叉煋澶嶉€夋缁?  renderOptionCheckboxes('experience', ['涓嶉檺','搴斿眾缁忛獙','1-3骞?,'3-5骞?,'5-10骞?,'10骞翠互涓?], currentFilters.experience || []);
  renderOptionCheckboxes('education', ['涓嶉檺','鍒濅腑鍙婁互涓?,'涓笓/涓妧','楂樹腑','澶т笓','鏈','纭曞＋','鍗氬＋'], currentFilters.education || []);
  renderOptionCheckboxes('companySize', [
    '20-99浜?,'100-499浜?,'500-999浜?,'1000-9999浜?,'10000浜轰互涓?,
    '涓嶉渶瑕佽瀺璧?,'澶╀娇杞?,'A杞?,'B杞?,'C杞?,'D杞強浠ヤ笂','涓婂競鍏徃','宸蹭笂绾?
  ], currentFilters.companySize || []);
}

// ===== 娓叉煋鍏抽敭瀛楀垪琛?=====
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

// ===== 娓叉煋澶嶉€夋缁?=====
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

// ===== 缁戝畾浜嬩欢 =====
function bindEvents() {
  // 娣诲姞鍏抽敭瀛?  el.addKeywordBtn.addEventListener('click', () => {
    const val = el.keywordInput.value.trim();
    if (!val || allKeywords.includes(val)) return;
    allKeywords.push(val);
    renderOptionKeywords();
    el.keywordInput.value = '';
  });

  el.keywordInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') el.addKeywordBtn.click();
  });

  // 鍒犻櫎鍏抽敭瀛?  el.keywordsList.addEventListener('click', (e) => {
    if (e.target.classList.contains('remove')) {
      allKeywords.splice(parseInt(e.target.dataset.index), 1);
      renderOptionKeywords();
    }
  });

  // 淇濆瓨鎸夐挳
  el.saveBtn.addEventListener('click', saveAllSettings);

  // 閲嶇疆鎵€鏈?  el.resetBtn.addEventListener('click', () => {
    if (confirm('纭畾瑕侀噸缃墍鏈夎缃悧锛熸鎿嶄綔涓嶅彲鎾ら攢銆?)) {
      chrome.storage.local.clear();
      alert('宸查噸缃墍鏈夎缃紝璇峰埛鏂伴〉闈€?);
      location.reload();
    }
  });

  // 娓呴櫎鎶曢€掕褰?  el.clearDeliveredBtn.addEventListener('click', () => {
    if (confirm('纭畾娓呴櫎鏈湴宸叉姇閫掕褰曪紵涓嬫杩愯鏃朵細閲嶆柊鎶曢€掔浉鍚岀殑鑱屼綅銆?)) {
      localStorage.removeItem('boss_delivered_jobs');
      alert('宸叉竻闄わ紒');
    }
  });
}

// ===== 淇濆瓨鎵€鏈夎缃?=====
async function saveAllSettings() {
  // 鏀堕泦澶嶉€夋閫変腑鍊?  ['experience', 'education', 'companySize'].forEach(type => {
    const checked = document.querySelectorAll(`#opt-${type}-checks .cb-item.selected input`);
    currentFilters[type] = Array.from(checked).map(c => c.value);
  });

  // 鍩虹绛涢€?  currentFilters.area = el.areas.value.split(/[,锛宂/).map(a => a.trim()).filter(Boolean);
  currentFilters.salaryMin = el.salaryMin.value;
  currentFilters.salaryMax = el.salaryMax.value;
  currentFilters.industry = el.industries.value.split(/[,锛宂/).map(i => i.trim()).filter(Boolean);

  // 鎶曢€掕缃?  currentDelivery.maxDailyCount = parseInt(el.maxDaily.value) || 50;
  currentDelivery.intervalMin = parseInt(el.intervalMin.value) || 10;
  currentDelivery.intervalMax = parseInt(el.intervalMax.value) || 30;
  currentDelivery.batchSize = parseInt(el.batchSize.value) || 5;
  currentDelivery.pauseBetweenBatches = (parseInt(el.batchPause.value) || 2) * 60000;
  currentDelivery.safeModeEnabled = el.safeMode.checked;
  currentDelivery.customGreeting = el.greeting.value;

  await chrome.storage.local.set({
    [STORAGE_KEYS.keywords]: allKeywords,
    [STORAGE_KEYS.filters]: currentFilters,
    [STORAGE_KEYS.delivery]: currentDelivery
  });

  // 鏄剧ず淇濆瓨鎴愬姛鎻愮ず
  el.saveStatus.textContent = '鉁?璁剧疆宸蹭繚瀛?';
  el.saveStatus.classList.add('show');
  setTimeout(() => el.saveStatus.classList.remove('show'), 2000);
}

