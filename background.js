// ============================================================
// Background Service Worker - 投递调度引擎
// 负责: 投递队列管理、频率控制、状态广播、防封号机制
// ============================================================

const CONFIG_KEYS = {
  JOB_KEYWORDS: 'boss_autoapplier_keywords',
  FILTER_SETTINGS: 'boss_autoapplier_filters',
  DELIVERY_SETTINGS: 'boss_autoapplier_delivery',
  ACTIVE_STATUS: 'boss_autoapplier_active',
  PROGRESS: 'boss_autoapplier_progress'
};

// ---- 默认投递设置 ----
const DEFAULT_DELIVERY_SETTINGS = {
  maxDailyCount: 50,            // 每日最大投递数
  intervalMin: 10,              // 最小间隔(秒)
  intervalMax: 30,              // 最大间隔(秒),随机化
  autoApproveChat: true,        // 自动发送打招呼
  customGreeting: '您好,我对这个职位很感兴趣,我的经验与岗位要求匹配度高,希望能有机会进一步沟通。',
  safeModeEnabled: true,        // 安全模式(更保守)
  pauseBetweenBatches: 120000,  // 批次间休息(ms)
  batchSize: 5                  // 每批投递数
};

// ---- Boss直聘筛选字段映射(从页面提取) ----
const DEFAULT_FILTERS = {
  area: [],             // 区域: 如['徐汇','黄浦','静安']
  salaryMin: '',        // 最低薪资(千/月)
  salaryMax: '',        // 最高薪资(千/月)
  experience: [],       // 经验: 如['1-3年','3-5年','不限']
  education: [],        // 学历: 如['大专','本科','硕士']
  companySize: [],      // 公司规模: 如['20-99人','100-499人']
  jobType: ['fulltime'],// 职位类型: ['fulltime'=全职,'parttime'=兼职]
  industry: []          // 行业: 如['互联网','金融']
};

// ---- 运行时状态 ----
let state = {
  isRunning: false,
  todayDelivered: 0,
  todayDate: null,
  currentBatch: 0,
  totalSkipped: 0
};

// ===== 初始化 =====
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(CONFIG_KEYS, (items) => {
    if (!items[CONFIG_KEYS.DELIVERY_SETTINGS]) {
      chrome.storage.local.set({ [CONFIG_KEYS.DELIVERY_SETTINGS]: DEFAULT_DELIVERY_SETTINGS });
    }
    if (!items[CONFIG_KEYS.FILTER_SETTINGS]) {
      chrome.storage.local.set({ [CONFIG_KEYS.FILTER_SETTINGS]: DEFAULT_FILTERS });
    }
  });
});

// ===== 日期同步 & 每日重置计数 =====
function resetDailyCountIfNeeded() {
  const today = new Date().toISOString().slice(0, 10);
  chrome.storage.local.get([CONFIG_KEYS.PROGRESS], (items) => {
    const progress = items[CONFIG_KEYS.PROGRESS] || {};
    if (progress.date !== today) {
      chrome.storage.local.set({
        [CONFIG_KEYS.PROGRESS]: { date: today, delivered: 0, skipped: 0, matched: 0 }
      });
      state.todayDelivered = 0;
      state.todayDate = today;
    } else {
      state.todayDelivered = progress.delivered || 0;
      state.todayDate = today;
      state.totalSkipped = progress.skipped || 0;
    }
  });
}

// ===== 获取完整配置 =====
function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get([
      CONFIG_KEYS.JOB_KEYWORDS,
      CONFIG_KEYS.FILTER_SETTINGS,
      CONFIG_KEYS.DELIVERY_SETTINGS,
      CONFIG_KEYS.ACTIVE_STATUS
    ], (items) => {
      resolve({
        keywords: items[CONFIG_KEYS.JOB_KEYWORDS] || [],
        filters: { ...DEFAULT_FILTERS, ...(items[CONFIG_KEYS.FILTER_SETTINGS] || {}) },
        delivery: { ...DEFAULT_DELIVERY_SETTINGS, ...(items[CONFIG_KEYS.DELIVERY_SETTINGS] || {}) },
        active: items[CONFIG_KEYS.ACTIVE_STATUS] || false
      });
    });
  });
}

// ===== 保存进度 =====
function saveProgress(added = 0, skipped = 0) {
  const today = new Date().toISOString().slice(0, 10);
  state.todayDelivered += added;
  state.totalSkipped += skipped;
  chrome.storage.local.set({
    [CONFIG_KEYS.PROGRESS]: {
      date: today,
      delivered: state.todayDelivered,
      skipped: state.totalSkipped,
      matched: state.todayDelivered + state.totalSkipped
    }
  });
}

// ===== 检查投递限制 =====
async function checkDeliveryLimit(config) {
  const d = config.delivery;
  if (state.todayDelivered >= d.maxDailyCount) {
    console.log('[BossAutoApply] 今日已达上限:', state.todayDelivered);
    await stopService();
    broadcastStatus('limit_reached');
    return true;
  }
  if (state.currentBatch >= d.batchSize) {
    console.log(`[BossAutoApply] 批次完成,休息 ${(d.pauseBetweenBatches / 1000)}s`);
    broadcastStatus('batch_paused', { remaining: d.pauseBetweenBatches });
    await new Promise(r => setTimeout(r, d.pauseBetweenBatches));
    state.currentBatch = 0;
  }
  return false;
}

// ===== 随机延迟 =====
function randInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function delay(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ===== 广播状态 =====
function broadcastStatus(type, extra = {}) {
  const payload = { type, ...extra, data: {
    isRunning: state.isRunning,
    todayDelivered: state.todayDelivered,
    totalSkipped: state.totalSkipped,
    currentBatch: state.currentBatch
  }};
  chrome.tabs.query({}, (tabs) => {
    tabs.forEach(tab => {
      if (tab.id) {
        chrome.tabs.sendMessage(tab.id, payload).catch(() => {});
      }
    });
  });
  chrome.storage.local.set({ [CONFIG_KEYS.ACTIVE_STATUS]: state.isRunning });
}

// ===== 停止服务 =====
async function stopService() {
  state.isRunning = false;
  state.currentBatch = 0;
  broadcastStatus('stopped');
  chrome.alarms.clearAll();
}

// ===== 消息监听 =====
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'toggleStart') {
    if (message.starting) {
      startCycle(sender.tab?.id);
    } else {
      stopService();
    }
    sendResponse({ ok: true });
  }
  return true; // keep channel open for async sendResponse
});

// ===== 主投递循环 =====
async function startCycle(tabId = null) {
  const config = await getConfig();
  if (!config.active) {
    config.active = true;
    chrome.storage.local.set({ [CONFIG_KEYS.ACTIVE_STATUS]: true });
  }
  
  resetDailyCountIfNeeded();
  state.isRunning = true;

  let targetTabId = tabId;
  
  // 查找Boss页面
  if (!targetTabId) {
    chrome.tabs.query({ url: 'https://www.zhipin.com/*' }, (tabs) => {
      if (tabs.length > 0) {
        targetTabId = tabs[0].id;
        runCycle(targetTabId, config);
      } else {
        state.isRunning = false;
        broadcastStatus('no_tab');
      }
    });
  } else {
    runCycle(targetTabId, config);
  }
}

// ===== 单轮投递循环 =====
async function runCycle(tabId, config) {
  while (state.isRunning && !await checkDeliveryLimit(config)) {
    try {
      const result = await chrome.tabs.sendMessage(tabId, {
        action: 'processNextPage',
        config: config
      }).catch(() => null);
      
      if (result) {
        // 如果content脚本报告了投递结果
        if (result.delivered) {
          saveProgress(result.delivered, 0);
        }
        if (result.skipped) {
          saveProgress(0, result.skipped);
        }
        if (result.needsStop) {
          break;
        }
      }
      
      state.currentBatch++;
      
      // 计算延迟
      const d = config.delivery;
      const delayRange = d.safeModeEnabled
        ? [d.intervalMax * 1000, d.intervalMax * 2.5 * 1000]
        : [d.intervalMin * 1000, d.intervalMax * 1000];
      await delay(randInt(delayRange[0], delayRange[1]));
      
    } catch (e) {
      console.warn('[BossAutoApply] 消息发送失败:', e.message);
      await delay(5000); // 出错后等久一点
    }
  }
}

// ===== 快捷键命令 =====
chrome.commands?.onCommand.addListener(async (command) => {
  if (command === 'toggle-delivery') {
    if (state.isRunning) {
      stopService();
    } else {
      startAutoDelivery();
    }
  }
});

// ===== 手动投递结果回调 =====
chrome.runtime.onMessage.addListener((msg, sender, sendResp) => {
  if (msg.action === 'jobDelivered') {
    saveProgress(1, 0);
    state.currentBatch++;
    sendResp({ ok: true });
  } else if (msg.action === 'jobSkipped') {
    saveProgress(0, 1);
    state.currentBatch++;
    sendResp({ ok: true });
  }
});
