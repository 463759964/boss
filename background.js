// ============================================================
// Background Service Worker - Boss直聘智能投递调度引擎
// 负责: 投递循环控制、频率防封、进度管理、多标签页协调
// ============================================================

const STORAGE_KEYS = {
  KEYWORDS: 'ba_keywords',
  FILTERS: 'ba_filters',
  DELIVERY: 'ba_delivery',
  ACTIVE: 'ba_active',
  PROGRESS: 'ba_progress'
};

// 默认投递配置
var DEFAULT_DELIVERY = {
  maxDailyCount: 50,
  intervalMinSec: 10,
  intervalMaxSec: 30,
  greetingMsg: '您好,我对这个职位很感兴趣。',
  safeModeEnabled: true,
  pauseBetweenBatchesMin: 2,
  batchSize: 5
};

// 默认筛选配置
var DEFAULT_FILTERS = {
  area: [], salaryMin: '', salaryMax: '',
  experience: [], education: [], companySize: [], jobType: ['fulltime']
};

// 运行时状态
var runtimeState = {
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

// ===== 每日计数重置 =====
function resetDailyCountIfNeeded(callback) {
  var today = new Date().toISOString().slice(0, 10);
  chrome.storage.local.get([STORAGE_KEYS.PROGRESS], function(items) {
    var progress = items[STORAGE_KEYS.PROGRESS] || {};
    if (progress.date !== today) {
      chrome.storage.local.set({
        [STORAGE_KEYS.PROGRESS]: { date: today, delivered: 0, skipped: 0, matched: 0 }
      }, function() {
        runtimeState.todayDelivered = 0;
        runtimeState.todaySkipped = 0;
        runtimeState.todayDate = today;
        callback();
      });
    } else {
      runtimeState.todayDelivered = progress.delivered || 0;
      runtimeState.todaySkipped = progress.skipped || 0;
      runtimeState.todayDate = today;
      callback();
    }
  });
}

// ===== 保存进度 =====
function saveProgress(added, skipped) {
  runtimeState.todayDelivered += added;
  runtimeState.todaySkipped += skipped;
  var today = runtimeState.todayDate || new Date().toISOString().slice(0, 10);
  chrome.storage.local.set({
    [STORAGE_KEYS.PROGRESS]: {
      date: today,
      delivered: runtimeState.todayDelivered,
      skipped: runtimeState.todaySkipped,
      matched: runtimeState.todayDelivered + runtimeState.todaySkipped
    }
  });
}

// ===== 获取完整配置 =====
function getConfig(callback) {
  chrome.storage.local.get([
    STORAGE_KEYS.KEYWORDS,
    STORAGE_KEYS.FILTERS,
    STORAGE_KEYS.DELIVERY,
    STORAGE_KEYS.ACTIVE
  ], function(items) {
    callback({
      keywords: items[STORAGE_KEYS.KEYWORDS] || [],
      filters: Object.assign({}, DEFAULT_FILTERS, items[STORAGE_KEYS.FILTERS] || {}),
      delivery: Object.assign({}, DEFAULT_DELIVERY, items[STORAGE_KEYS.DELIVERY] || {}),
      active: items[STORAGE_KEYS.ACTIVE] || false
    });
  });
}

// ===== 随机整数 =====
function randInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

// ===== 检查投递限制 =====
function checkDeliveryLimit(config, onLimit, onContinue) {
  var d = config.delivery;
  if (runtimeState.todayDelivered >= d.maxDailyCount) {
    console.log('[BossAutoApply] 今日已达上限:', runtimeState.todayDelivered);
    stopService();
    if (onLimit) onLimit('limit_reached');
    return;
  }
  if (runtimeState.currentBatch >= d.batchSize) {
    var pauseMs = d.pauseBetweenBatchesMin * 60000;
    console.log('[BossAutoApply] 批次完成,休息', pauseMs / 1000, '秒');
    broadcastStatus('batch_paused', { remaining: pauseMs });
    setTimeout(function() {
      runtimeState.currentBatch = 0;
      onContinue();
    }, pauseMs);
    return;
  }
  onContinue();
}

// ===== 广播状态 =====
function broadcastStatus(type, extra) {
  var payload = { type: type };
  if (extra) Object.keys(extra).forEach(function(k) { payload[k] = extra[k]; });
  payload.state = {
    running: runtimeState.running,
    delivered: runtimeState.todayDelivered,
    skipped: runtimeState.todaySkipped,
    batch: runtimeState.currentBatch
  };

  chrome.tabs.query({}, function(tabs) {
    tabs.forEach(function(tab) {
      if (tab.id && tab.url && tab.url.indexOf('zhipin.com') !== -1) {
        chrome.tabs.sendMessage(tab.id, payload).catch(function(){});
      }
    });
  });

  chrome.storage.local.set({ [STORAGE_KEYS.ACTIVE]: runtimeState.running });
}

// ===== 停止服务 =====
function stopService() {
  runtimeState.running = false;
  runtimeState.currentBatch = 0;
  broadcastStatus('stopped');
}

// ===== 消息监听 =====
chrome.runtime.onMessage.addListener(function(msg, sender, sendResponse) {
  if (msg.action === 'toggleStart') {
    runtimeState.running = true;
    runtimeState.activeTabId = sender.tab ? sender.tab.id : null;
    startDeliveryCycle();
    sendResponse({ ok: true });
  }
  if (msg.action === 'stopDelivery') {
    stopService();
    sendResponse({ ok: true });
  }
  // 处理 content script 的投递结果反馈
  if (msg.action === 'jobDelivered') {
    saveProgress(1, 0);
    runtimeState.currentBatch++;
    sendResponse({ ok: true });
  }
  if (msg.action === 'jobSkipped') {
    saveProgress(0, 1);
    runtimeState.currentBatch++;
    sendResponse({ ok: true });
  }
  if (msg.action === 'processResult') {
    if (msg.result && msg.result.matched) {
      var matched = msg.result.matched;
      runtimeState.currentBatch += matched;
    }
    saveProgress(msg.result ? msg.result.delivered || 0 : 0,
                 msg.result ? msg.result.skipped || 0 : 0);
    sendResponse({ ok: true });
  }
  return true; // keep channel open for async response
});

// ===== 启动投递循环 =====
function startDeliveryCycle() {
  getConfig(function(config) {
    if (!config.active) {
      config.active = true;
      chrome.storage.local.set({ [STORAGE_KEYS.ACTIVE]: true });
    }

    resetDailyCountIfNeeded(function() {
      // 找到 Boss 直聘的页面
      chrome.tabs.query({ url: 'https://www.zhipin.com/*' }, function(tabs) {
        if (!tabs || tabs.length === 0) {
          console.warn('[BossAutoApply] 未找到 Boss 直聘标签页');
          broadcastStatus('no_tab');
          runtimeState.running = false;
          return;
        }

        var mainTab = tabs[0];
        runtimeState.activeTabId = mainTab.id;
        console.log('[BossAutoApply] 开始在 Tab', mainTab.id, '上执行投递');
        broadcastStatus('starting', { tabUrl: mainTab.url });

        runDeliveryLoop(config, mainTab.id);
      });
    });
  });
}

// ===== 核心投递循环 =====
function runDeliveryLoop(config, tabId) {
  if (!runtimeState.running) return;

  // 先通知 content script 处理当前页并找出匹配的职位
  chrome.tabs.sendMessage(tabId, {
    action: 'processNextPage',
    config: {
      keywords: config.keywords,
      filters: config.filters,
      deliverySettings: config.delivery
    }
  }, function(response) {
    if (!response || !response.success) {
      // 如果当前页面无数据或没有匹配,等待后重试
      var waitMs = randInt(5000, 10000);
      setTimeout(function() {
        if (runtimeState.running) runDeliveryLoop(config, tabId);
      }, waitMs);
      return;
    }

    var matched = response.matched || 0;
    var skipped = response.skipped || 0;

    // 如果有匹配到的职位,需要在新标签页中打开并操作
    // 但由于 Boss 是 SPA,我们直接通过 chrome.tabs.sendMessage 触发翻页
    // 并重复这个流程,因为每页都需要独立处理

    if (matched > 0) {
      // 有匹配的职位,逐条投递
      var jobsToDeliver = response.jobs || [];
      deliverMatchedJobs(config, tabId, jobsToDeliver.slice(0, 5));
    } else if (!response.noJobs) {
      // 本页没有匹配,检查是否还有下一页
      checkAndGoNextPage(tabId, config);
    }

    // 计算下次投递延迟 (防封号核心)
    var delayRange;
    if (config.delivery.safeModeEnabled) {
      delayRange = [config.delivery.intervalMaxSec * 1000, config.delivery.intervalMaxSec * 2.5 * 1000];
    } else {
      delayRange = [config.delivery.intervalMinSec * 1000, config.delivery.intervalMaxSec * 1000];
    }

    var nextDelay = randInt(delayRange[0], delayRange[1]);
    console.log('[BossAutoApply] 等待', nextDelay / 1000, '秒后进行下一轮');
    setTimeout(function() {
      checkDeliveryLimit(config,
        function(reason) { /* stopped due to limit */ },
        function() {
          if (runtimeState.running) runDeliveryLoop(config, tabId);
        }
      );
    }, nextDelay);
  });
}

// ===== 投递匹配到的职位 =====
function deliverMatchedJobs(config, tabId, jobs) {
  if (!jobs || jobs.length === 0) return;

  var jobKey = jobs[0]; // key is "title|company"

  // 通知 content script 高亮标记这个职位
  chrome.tabs.sendMessage(tabId, {
    action: 'deliverJobFor',
    jobKey: jobKey
  }, function(resp) {
    saveProgress(1, 0);
    runtimeState.currentBatch++;
    // 移除已投递的,继续投递下一个
    if (jobs.length > 1) {
      setTimeout(function() {
        deliverMatchedJobs(config, tabId, jobs.slice(1));
      }, randInt(2000, 4000));
    }
  });
}

// ===== 检查并翻到下一页 =====
function checkAndGoNextPage(tabId, config) {
  chrome.tabs.sendMessage(tabId, {
    action: 'goNextPage'
  }, function(resp) {
    if (resp && resp.clicked) {
      // 成功翻页,等待 DOM 加载
      setTimeout(function() {
        // 翻页后重新发送 processNextPage 处理新页面
        chrome.tabs.sendMessage(tabId, {
          action: 'processNextPage',
          config: config
        });
      }, config.delivery.waitPageLoad || 3000);
    }
  });
}
