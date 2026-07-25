// ============================================================
// Content Script - Boss直聘页面解析与投递引擎
// 负责: 职位卡片的提取/筛选/投递, 筛选面板的解析, UI增强
// ============================================================

// ---- 全局状态 ----
let autoApply = {
  running: false,
  keywords: [],
  filters: {},
  deliverySettings: {},
  lastJobTitle: '',
  currentPage: 1,
  consecutiveNonMatch: 0 // 连续不匹配计数,超过阈值停止翻到下一页
};

const MAX_CONSECUTIVE_NON_MATCH = 5; // 最多连续跳过多少个不匹配的

// ---- 监听 background 消息 ----
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'processNextPage') {
    processPage(msg.config).then(sendResponse);
    return true;
  }
  if (msg.action === 'deliverJob' && msg.data) {
    deliverSingleJob(msg.data).then(() => sendResponse({ ok: true }));
    return true;
  }
});

// ---- 监听自定义事件(来自UI交互) ----
document.addEventListener('boss-autoapply-toggle', (e) => {
  autoApply.running = e.detail.running;
  updateStatusUI();
});

document.addEventListener('boss-autoapply-config', (e) => {
  Object.assign(autoApply, e.detail);
});

// ===== 核心: 处理当前页 =====
async function processPage(config) {
  if (!autoApply.running) return { skipped: 0 };

  autoApply.keywords = config.keywords;
  autoApply.filters = config.filters;
  autoApply.deliverySettings = config.delivery;

  // 第一步: 确保页面在搜索/列表视图
  const isInSearch = window.location.href.includes('job/list');
  if (!isInSearch) {
    console.log('[BossAutoApply] 不在列表页,尝试导航...');
    // 如果不在搜索结果页，尝试获取关键词
    const keywords = getKeywordsFromURL() || config.keywords?.[0] || '';
    if (keywords) {
      navigateToSearch(keywords);
      await sleep(3000);
    }
  }

  // 第二步: 提取并过滤职位卡片
  const jobCards = extractJobCards();
  let delivered = 0;
  let skipped = 0;
  
  for (const job of jobCards) {
    if (!autoApply.running) break;

    // 检查是否重复投递(避免循环)
    if (hasDelivered(job)) continue;

    // 应用筛选
    const match = applyFilters(job);
    
    if (!match) {
      skipped++;
      autoApply.consecutiveNonMatch++;
      
      if (autoApply.consecutiveNonMatch >= MAX_CONSECUTIVE_NON_MATCH) {
        console.log('[BossAutoApply] 连续无匹配,翻页...');
        autoApply.consecutiveNonMatch = 0;
        clickNextPage();
        await sleep(3000);
        return { delivered: 0, skipped: 1, needsStop: false };
      }
      continue;
    }

    autoApply.consecutiveNonMatch = 0;
    
    // 匹配成功,打开职位并投递
    console.log('[BossAutoApply] 匹配职位:', job.title);
    markAsDelivered(job);
    
    try {
      await openJobDetail(job);
      await sleep(getWaitTime('openDetail'));
      
      await sendMessageChat();
      await sleep(getWaitTime('chat'));
      
      await closeTab();
      await sleep(getWaitTime('closeTab'));
      
      delivered++;
    } catch (err) {
      console.error('[BossAutoApply] 投递失败:', err.message);
      await sleep(getWaitTime('errorRetry'));
    }
  }

  // 如果没有投递任何且还有下一页,自动翻页
  if (delivered === 0 && hasMorePages()) {
    console.log('[BossAutoApply] 本页无投递,翻页...');
    clickNextPage();
    await sleep(getWaitTime('pageLoad'));
  }

  return { delivered, skipped };
}

// ===== Boss直聘页面元素选择器 =====
const SELECTORS = {
  jobList: '.job-card-wrapper .job-card-left',
  jobCard: '.job-card-left',
  jobTitle: '.job-card-left .job-card-main a',
  jobSalary: '.job-card-left .information span:first-child',
  jobCompany: '.job-card-left .company-info h3',
  jobInfo: '.job-card-left .job-card-main p', // 经验/学历/区域
  jobArea: '.job-card-left .job-area',
  nextBtn: '.pager_next .pager_next_btn',
  hasNext: '.pager_next:not(.disabled)',
  salaryTag: '.job-card-left .format-margin',
  experienceTags: '.job-card-left p span',
  chatButton: '.btn-start-chat',
  sendBtn: '.talk-btn--3O8R4tY',
  confirmBtn: '.confirm-box__btn--1wF8q0j',
  closeButton: '.intimacy-tip .el-icon-close,.close--2T0pM1s'
};

// ===== 提取页面中的职位卡片 =====
function extractJobCards() {
  const cards = document.querySelectorAll(SELECTORS.jobCard);
  const jobs = [];

  cards.forEach(card => {
    try {
      const titleEl = card.querySelector(SELECTORS.jobTitle.split(' ').pop());
      const salaryEl = card.querySelector(SELECTORS.jobSalary.split(' ').pop());
      const companyEl = card.querySelector('.company-info h3');
      const infoP = card.querySelector('.job-card-main p');
      const areaEl = card.querySelector('.job-area');
      
      if (!titleEl) return;

      const title = titleEl.textContent.trim();
      const salaryText = salaryEl?.textContent.trim() || '';
      const company = companyEl?.textContent.trim() || '';
      const area = areaEl?.textContent.trim() || '';
      const infoText = infoP?.textContent.trim() || '';
      
      // 解析薪资
      const salary = parseSalary(salaryText);
      
      // 解析信息标签
      const tags = parseInfoTags(infoText);

      jobs.push({
        title,
        salary,
        salaryText,
        company,
        area,
        ...tags,
        element: card,
        titleEl
      });
    } catch (e) {
      console.warn('[BossAutoApply] 解析卡片失败:', e.message);
    }
  });

  console.log(`[BossAutoApply] 找到 ${jobs.length} 个职位卡片`);
  return jobs;
}

// ===== 解析薪资字符串 =====
function parseSalary(text) {
  const nums = text.match(/(\d+)-(\d+)/g);
  if (!nums) return { min: 0, max: 0, unit: '' };
  
  const [minStr, maxStr] = nums[0].split('-').map(Number);
  return { min: minStr, max: maxStr, unit: 'K/月' };
}

// ===== 解析职位信息标签(经验/学历等) =====
function parseInfoTags(text) {
  const experienceMap = {};
  const educationMap = {};
  
  const parts = text.split(/\s+/);
  const result = {
    experience: null,
    education: null
  };

  // Boss直聘的info通常是: "1-3年" "本科" "西湖区"
  for (const part of parts) {
    if (/^[\d+-]+年$/.test(part) || /^[\d+-]+以下$/.test(part)) {
      result.experience = part;
    } else if (/^(本科|大专|硕士|博士|不限)$/.test(part)) {
      result.education = part;
    }
  }

  return result;
}

// ===== 应用筛选条件 =====
function applyFilters(job) {
  const f = autoApply.filters;
  const kw = autoApply.keywords;
  
  // 1. 关键字匹配
  if (kw.length > 0) {
    const keywordMatched = kw.some(kw2 => 
      job.title.includes(kw2) || 
      job.description?.some(d => d.includes(kw2))
    );
    if (!keywordMatched) return false;
  }

  // 2. 薪资筛选
  if (f.salaryMin || f.salaryMax) {
    const min = parseInt(f.salaryMin) || 0;
    const max = parseInt(f.salaryMax) || 999;
    if (job.salary.max < min || job.salary.min > max) return false;
  }

  // 3. 经验筛选
  if (f.experience.length > 0 && job.experience) {
    if (!f.experience.includes(job.experience)) return false;
  }

  // 4. 学历筛选
  if (f.education.length > 0 && job.education) {
    if (!f.education.includes(job.education)) return false;
  }

  // 5. 区域筛选
  if (f.area.length > 0 && job.area) {
    const areaMatched = f.area.some(a => job.area.includes(a));
    if (!areaMatched) return false;
  }

  // 6. 公司规模筛选
  if (f.companySize.length > 0) {
    // 从公司卡片获取规模信息
    const sizeEl = job.element.querySelector('.company-info .company-tag-list li');
    if (sizeEl) {
      const sizeText = sizeEl.textContent.trim();
      if (!f.companySize.some(s => sizeText.includes(s))) return false;
    }
  }

  // 7. 职位类型筛选
  if (f.jobType.length > 0) {
    // 全职通常是默认值,兼职有标记
    const isPartTime = job.title.includes('兼职') || 
                       job.title.includes('实习') || 
                       job.title.includes('暑期');
    if (f.jobType.includes('parttime') && !isPartTime) return false;
    if (f.jobType.includes('fulltime') && isPartTime) return false;
  }

  return true;
}

// ===== 判断是否有更多页 =====
function hasMorePages() {
  return document.querySelector(SELECTORS.hasNext) !== null;
}

// ===== 点击下一页 =====
function clickNextPage() {
  const btn = document.querySelector(SELECTORS.nextBtn);
  if (btn) {
    btn.click();
    return true;
  }
  return false;
}

// ===== 打开职位详情 =====
async function openJobDetail(job) {
  job.titleEl.click();
  // Boss直聘在新tab打开,等待新窗口
  await sleep(2000);
  return true;
}

// ===== 发送聊天消息(打招呼) =====
async function sendMessageChat() {
  const greeting = autoApply.deliverySettings.customGreeting || 
    '您好,我对这个职位很感兴趣。';
  
  // Boss直聘的聊天入口按钮
  const chatBtn = document.querySelector('.btn-start-chat, .talk-icon--3eG3xZK');
  if (!chatBtn) {
    console.log('[BossAutoApply] 未找到聊天按钮');
    return false;
  }

  chatBtn.click();
  await sleep(1500);

  // 找到输入框并发送
  const input = document.querySelector('textarea[placeholder], .talk-panel__textarea--3kQ2xLb, .el-textarea textarea');
  if (input) {
    // 模拟用户输入
    input.focus();
    input.value = greeting;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    
    await sleep(500);
    
    // 找到发送按钮
    const sendBtn = document.querySelector('.talk-btn--3O8R4tY, .el-button--primary, button[type="submit"]');
    if (sendBtn) {
      sendBtn.click();
      await sleep(1000);
      console.log('[BossAutoApply] 已发送打招呼消息');
      return true;
    }
  }
  
  console.log('[BossAutoApply] 未找到输入框或发送按钮');
  return false;
}

// ===== 关闭标签页 =====
async function closeTab() {
  // 在当前页面查找关闭按钮
  const closeBtn = document.querySelector(SELECTORS.closeButton);
  if (closeBtn) {
    closeBtn.click();
    await sleep(1000);
  }
  
  // 如果是新标签页打开的,可以调用 chrome.tabs API 关闭
  // 但由于这是 content script,我们尝试用 JS 关闭
  window.close();
}

// ===== 重复投递检测 =====
function getDeliveredJobs() {
  try {
    return JSON.parse(localStorage.getItem('boss_delivered_jobs') || '[]');
  } catch {
    return [];
  }
}

function hasDelivered(job) {
  const delivered = getDeliveredJobs();
  const key = `${job.title}-${job.company}`;
  return delivered.includes(key);
}

function markAsDelivered(job) {
  const delivered = getDeliveredJobs();
  const key = `${job.title}-${job.company}`;
  if (!delivered.includes(key)) {
    delivered.push(key);
    // 只保留最近100条
    if (delivered.length > 100) delivered.splice(0, delivered.length - 100);
    localStorage.setItem('boss_delivered_jobs', JSON.stringify(delivered));
  }
}

// ===== 工具函数 =====
function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function getWaitTime(type) {
  const settings = autoApply.deliverySettings;
  switch (type) {
    case 'openDetail': return settings.waitDetailOpen || 4000;
    case 'chat': return 2000;
    case 'closeTab': return 1000;
    case 'pageLoad': return settings.waitPageLoad || 3000;
    case 'errorRetry': return 5000;
    default: return 3000;
  }
}

function getKeywordsFromURL() {
  const params = new URLSearchParams(window.location.search);
  return params.get('keyword') || '';
}

function navigateToSearch(keyword) {
  const cityParam = getCityFromURL() || 'hangzhou';
  window.location.href = `https://www.zhipin.com/job/detail/?query=${encodeURIComponent(keyword)}&city=${cityParam}`;
}

function getCityFromURL() {
  const m = window.location.pathname.match(/\/([^/]+)\/?$/);
  return m ? m[1] : 'hangzhou';
}

// ===== 注入状态指示器 =====
function injectStatusIndicator() {
  // 在页面侧边添加浮动按钮
  const div = document.createElement('div');
  div.id = 'boss-autoapply-indicator';
  div.style.cssText = `
    position: fixed; top: 80px; right: 20px; z-index: 9999;
    width: 56px; height: 56px; border-radius: 50%;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white; display: flex; align-items: center; justify-content: center;
    font-size: 24px; cursor: pointer; box-shadow: 0 4px 15px rgba(102,126,234,0.4);
    transition: all 0.3s ease; user-select: none;
  `;
  div.innerHTML = '🤖';
  div.title = 'Boss直聘智能投递助手';
  
  // Tooltip
  const tooltip = document.createElement('div');
  tooltip.className = 'autoapply-tooltip';
  tooltip.style.cssText = `
    position: absolute; right: 65px; top: 50%; transform: translateY(-50%);
    background: rgba(0,0,0,0.85); color: white; padding: 8px 12px;
    border-radius: 6px; font-size: 12px; white-space: nowrap;
    pointer-events: none; opacity: 0; transition: opacity 0.2s;
  `;
  tooltip.innerHTML = `<div style="font-weight:bold;margin-bottom:2px">Boss投递助手</div>
    <div id="autoapply-status-text">点击启动</div>`;
  div.appendChild(tooltip);
  
  div.addEventListener('mouseenter', () => tooltip.style.opacity = '1');
  div.addEventListener('mouseleave', () => tooltip.style.opacity = '0');
  
  div.addEventListener('click', () => {
    toggleAutoApply();
  });

  document.body.appendChild(div);
  updateStatusUI();
}

// ===== 更新状态UI =====
function updateStatusUI() {
  const indicator = document.getElementById('boss-autoapply-indicator');
  if (!indicator) return;
  
  const icon = indicator.firstChild;
  const statusText = indicator.querySelector('#autoapply-status-text');
  
  if (autoApply.running) {
    icon.innerHTML = '⏸';
    icon.parentElement.style.background = 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)';
    statusText.textContent = `投递中: ${state.todayDelivered || 0}/日`;
    icon.parentElement.title = '点击停止';
  } else {
    icon.innerHTML = '🤖';
    icon.parentElement.style.background = 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)';
    statusText.textContent = '点击启动';
    icon.parentElement.title = '点击启动';
  }
}

// ===== 切换自动投递 =====
function toggleAutoApply() {
  autoApply.running = !autoApply.running;
  
  // 通知background
  chrome.runtime.sendMessage({ action: 'toggleStart', starting: autoApply.running })
    .catch(() => {});
  
  // 触发事件
  document.dispatchEvent(new CustomEvent('boss-autoapply-toggle', {
    detail: { running: autoApply.running }
  }));
  
  updateStatusUI();
}

// ===== 监听状态变更 =====
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type) {
    updateStatusUI();
  }
});

// ===== 初始化 =====
(function init() {
  console.log('[BossAutoApply] Content Script 已加载');
  
  // 注入悬浮按钮
  setTimeout(injectStatusIndicator, 1000);
  
  // 定期同步配置
  syncConfig();
  setInterval(syncConfig, 5000);
})();

// ===== 同步配置 =====
function syncConfig() {
  chrome.storage.local.get([
    'boss_autoapplier_keywords',
    'boss_autoapplier_filters',
    'boss_autoapplier_delivery',
    'boss_autoapplier_active'
  ], (items) => {
    document.dispatchEvent(new CustomEvent('boss-autoapply-config', {
      detail: {
        keywords: items.boss_autoapplier_keywords || [],
        filters: items.boss_autoapplier_filters || {},
        deliverySettings: items.boss_autoapplier_delivery || {}
      }
    }));
    
    if (items.boss_autoapplier_active) {
      document.dispatchEvent(new CustomEvent('boss-autoapply-toggle', {
        detail: { running: true }
      }));
    }
  });
}
