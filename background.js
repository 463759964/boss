// ============================================================
// Background Service Worker - Boss直聘投递调度引擎
// 负责: 投递循环、频率控制、进度管理、消息转发
// ============================================================

var STORAGE_KEYS = {
  KEYWORDS: 'ba_keywords',
  FILTERS: 'ba_filters',
  DELIVERY: 'ba_delivery',
  ACTIVE: 'ba_active',
  PROGRESS: 'ba_progress'
};

var DEFAULT_DELIVERY = {
  maxDailyCount: 50,
  intervalMinSec: 10,
  intervalMaxSec: 30,
  greetingMsg: '您好,我对这个职位很感兴趣。',
  safeModeEnabled: true,
  pauseBetweenBatchesMin: 2,
  batchSize: 5
};

var DEFAULT_FILTERS = {
  area: [], salaryMin: '', salaryMax: '',
  experience: [], education: [], companySize: [], jobType: ['fulltime']
};

var state = {
  running: false,
  todayDelivered: 0,
  todaySkipped: 0,
  todayDate: null,
  currentBatch: 0,
  activeTabId: null
};

// ===== 安装初始化 =====
chrome.runtime.onInstalled.addListener(function() {
  chrome.storage.local.get([STORAGE_KEYS.DELIVERY, STORAGE_KEYS.FILTERS], function(items) {
    if (!items[STORAGE_KEYS.DELIVERY]) {
      chrome.storage.local.set({ [STORAGE_KEYS.DELIVERY]: DEFAULT_DELIVERY });
    }
    if (!items[STORAGE_KEYS.FILTERS]) {
      chrome.storage.local.set({ [STORAGE_KEYS.FILTERS]: DEFAULT_FILTERS });
    }
  });
});

// ===== 每日重置 =====
function resetDailyIfNeeded(cb) {
  var today = new Date().toISOString().slice(0, 10);
  chrome.storage.local.get([STORAGE_KEYS.PROGRESS], function(items) {
    var p = items[STORAGE_KEYS.PROGRESS] || {};
    if (p.date !== today) {
      chrome.storage.local.set({
        [STORAGE_KEYS.PROGRESS]: { date: today, delivered: 0, skipped: 0, matched: 0 }
      }, cb);
      state.todayDelivered = 0;
      state.todaySkipped = 0;
      state.todayDate = today;
    } else {
      state.todayDelivered = p.delivered || 0;
      state.todaySkipped = p.skipped || 0;
      state.todayDate = today;
      cb();
    }
  });
}

// ===== 保存进度 =====
function saveProgress(added, skipped) {
  state.todayDelivered += added;
  state.todaySkipped += skipped;
  var today = state.todayDate || new Date().toISOString().slice(0, 10);
  chrome.storage.local.set({
    [STORAGE_KEYS.PROGRESS]: {
      date: today,
      delivered: state.todayDelivered,
      skipped: state.todaySkipped,
      matched: state.todayDelivered + state.todaySkipped
    }
  });
}

// ===== 获取配置 =====
function getConfig(cb) {
  chrome.storage.local.get([STORAGE_KEYS.KEYWORDS, STORAGE_KEYS.FILTERS,
                            STORAGE_KEYS.DELIVERY, STORAGE_KEYS.ACTIVE], function(items) {
    cb({
      keywords: items[STORAGE_KEYS.KEYWORDS] || [],
      filters: Object.assign({}, DEFAULT_FILTERS, items[STORAGE_KEYS.FILTERS] || {}),
      delivery: Object.assign({}, DEFAULT_DELIVERY, items[STORAGE_KEYS.DELIVERY] || {}),
      active: items[STORAGE_KEYS.ACTIVE] || false
    });
  });
}

function randInt(a, b) { return Math.floor(Math.random() * (b - a + 1)) + a; }

// ===== 广播状态 =====
function broadcastStatus(type, extra) {
  var payload = { type: type };
  if (extra) Object.keys(extra).forEach(function(k) { payload[k] = extra[k]; });
  payload.state = {
    running: state.running,
    delivered: state.todayDelivered,
    skipped: state.todaySkipped,
    batch: state.currentBatch
  };

  chrome.tabs.query({}, function(tabs) {
    tabs.forEach(function(tab) {
      if (tab.id && tab.url && tab.url.indexOf('zhipin.com') !== -1) {
        chrome.tabs.sendMessage(tab.id, payload).catch(function(){});
      }
    });
  });
  chrome.storage.local.set({ [STORAGE_KEYS.ACTIVE]: state.running });
}

// ===== 停止 =====
function stopService() {
  state.running = false;
  state.currentBatch = 0;
  broadcastStatus('stopped');
}

// ===== 主循环 =====
function startCycle() {
  getConfig(function(config) {
    if (!config.active) {
      config.active = true;
      chrome.storage.local.set({ [STORAGE_KEYS.ACTIVE]: true });
    }

    resetDailyIfNeeded(function() {
      chrome.tabs.query({ url: 'https://www.zhipin.com/*' }, function(tabs) {
        if (!tabs || tabs.length === 0) {
          broadcastStatus('no_tab');
          state.running = false;
          return;
        }

        var tab = tabs[0];
        state.activeTabId = tab.id;
        broadcastStatus('starting', { tabUrl: tab.url });
        runLoop(config, tab.id);
      });
    });
  });
}

// ===== 投递循环 =====
function runLoop(config, tabId) {
  if (!state.running) return;

  // 检查限制
  checkLimit(config, tabId, function(canContinue) {
    if (!canContinue) return;

    // 通知 content script 处理当前页
    chrome.tabs.sendMessage(tabId, {
      action: 'processNextPage',
      config: {
        keywords: config.keywords,
        filters: config.filters,
        deliverySettings: config.delivery
      }
    }, function(response) {
      if (response && response.success && response.matched > 0) {
        // 有匹配的职位 — 继续下一轮（延迟后自动处理下一页）
        state.currentBatch += response.matched;
        showLog(tabId, '命中 ' + response.matched + ' 个职位');
      } else if (response && !response.noJobs) {
        showLog(tabId, '无匹配，等翻页...');
      }

      // 计算延迟
      var delayRange = config.delivery.safeModeEnabled
        ? [config.delivery.intervalMaxSec * 1000, config.delivery.intervalMaxSec * 2.5 * 1000]
        : [config.delivery.intervalMinSec * 1000, config.delivery.intervalMaxSec * 1000];

      setTimeout(function() {
        if (state.running) runLoop(config, tabId);
      }, randInt(delayRange[0], delayRange[1]));
    });
  });
}

// ===== 检查限制 =====
function checkLimit(config, tabId, cb) {
  var d = config.delivery;
  if (state.todayDelivered >= d.maxDailyCount) {
    console.log('[BossAutoApply] 今日已达上限:', state.todayDelivered);
    stopService();
    cb(false);
    return;
  }
  if (state.currentBatch >= d.batchSize) {
    var pauseMs = d.pauseBetweenBatchesMin * 60000;
    console.log('[BossAutoApply] 批次完成,休息', pauseMs / 1000, '秒');
    broadcastStatus('batch_paused');
    setTimeout(function() {
      state.currentBatch = 0;
      cb(true);
    }, pauseMs);
    return;
  }
  cb(true);
}

// ===== 日志转发 =====
function showLog(tabId, message) {
  // 转发到 popup
  chrome.tabs.query({ url: 'chrome-extension://*/popup.html' }, function(popTabs) {
    if (popTabs.length > 0) {
      chrome.tabs.sendMessage(popTabs[0].id, {
        action: 'deliveryResult',
        result: {
          type: 'info',
          title: message,
          time: new Date().toLocaleTimeString()
        }
      }).catch(function(){});
    }
  });
}

// ===== 消息监听 =====
chrome.runtime.onMessage.addListener(function(msg, sender, sendResponse) {
  if (msg.action === 'startDelivery') {
    state.running = true;
    startCycle();
    sendResponse({ ok: true });
  }
  if (msg.action === 'stopDelivery') {
    stopService();
    sendResponse({ ok: true });
  }
  // 从 content script 接收筛选数据并转发给 popup
  if (msg.action === 'sendFilterData') {
    chrome.tabs.query({ url: 'chrome-extension://*/popup.html' }, function(popTabs) {
      if (popTabs.length > 0) {
        chrome.tabs.sendMessage(popTabs[0].id, {
          action: 'filterDataUpdate',
          data: msg.data
        }).catch(function(){});
      }
    });
    sendResponse({ ok: true });
  }
  return true;
});
