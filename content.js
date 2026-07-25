// ============================================================
// Content Script - Boss直聘页面解析与增强
// 负责: 职位卡片的提取/高亮/筛选显示, 状态指示器
// 投递执行交由 background.js 通过 chrome.tabs.executeScript 完成
// ============================================================

(function() {
  'use strict';

  // ---- 运行时状态 ----
  let state = {
    running: false,
    keywords: [],
    filters: {},
    deliverySettings: {},
    currentPage: 1,
    consecutiveNonMatch: 0,
    deliveredJobs: new Set()
  };

  const MAX_CONSECUTIVE_NON_MATCH = 8;

  // ---- 加载已投递记录 ----
  function loadDeliveredJobs() {
    try {
      const stored = localStorage.getItem('boss_delivered_jobs');
      if (stored) state.deliveredJobs = new Set(JSON.parse(stored));
    } catch(e) {}
  }

  function saveDeliveredJob(key) {
    state.deliveredJobs.add(key);
    if (state.deliveredJobs.size > 200) {
      const arr = [...state.deliveredJobs];
      state.deliveredJobs = new Set(arr.slice(-200));
    }
    localStorage.setItem('boss_delivered_jobs', JSON.stringify([...state.deliveredJobs]));
  }

  loadDeliveredJobs();

  // ===== DOM 选择器 ----
  // Boss直聘搜索列表页的元素结构
  var SELECTORS = {
    jobCard: '.job-card',
    jobTitle: '.job-card .job-card-left .job-card-main a',
    jobSalary: '.job-card .job-card-left .information span:first-child',
    jobCompany: '.job-card .company-info h3',
    jobArea: '.job-card .job-card-left .job-area',
    jobInfoP: '.job-card .job-card-left p',
    nextBtn: '.btn.page-next',
    hasNext: '.page-box .pager_next:not(.disabled)',
    filterPanels: '.job_filter_wrapper .filter-body'
  };

  // ===== 提取职位卡片数据 =====
  function extractJobCards() {
    var cards = document.querySelectorAll(SELECTORS.jobCard);
    var jobs = [];

    cards.forEach(function(card) {
      try {
        var titleEl = card.querySelector(SELECTORS.jobTitle.split(' ').pop());
        var salaryEl = card.querySelector('.job-card-left .information span:first-child');
        var companyEl = card.querySelector('.company-info h3');
        var infoP = card.querySelector('.job-card-left p');
        var areaEl = card.querySelector('.job-card-left .job-area');

        if (!titleEl) return;

        var title = titleEl.textContent.trim();
        var salaryText = salaryEl ? salaryEl.textContent.trim() : '';
        var company = companyEl ? companyEl.textContent.trim() : '';
        var area = areaEl ? areaEl.textContent.trim() : '';
        var infoText = infoP ? infoP.textContent.trim() : '';

        var salary = parseSalary(salaryText);
        var tags = parseInfoTags(infoText);

        jobs.push({
          title: title,
          salary: salary,
          salaryText: salaryText,
          company: company,
          area: area,
          experience: tags.experience,
          education: tags.education,
          element: card,
          titleEl: titleEl,
          key: title + '|' + company
        });
      } catch(e) {
        console.warn('[BossAutoApply] 解析卡片失败:', e.message);
      }
    });

    console.log('[BossAutoApply] 提取到 ' + jobs.length + ' 个职位');
    return jobs;
  }

  // ===== 薪资解析 =====
  function parseSalary(text) {
    var m = text.match(/(\d+)-(\d+)/g);
    if (m && m[0]) {
      var nums = m[0].split('-').map(Number);
      return { min: nums[0], max: nums[1], unit: 'K/月' };
    }
    return { min: 0, max: 0, unit: '' };
  }

  // ===== 信息标签解析 =====
  function parseInfoTags(text) {
    var parts = text.split(/[|\s]+/);
    var result = { experience: null, education: null };

    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].trim();
      if (/^[\d+-]+年$/.test(p) || /^[\d+-]+以下$/.test(p)) {
        result.experience = p;
      } else if (/^(本科|大专|硕士|博士|不限|高中)$/.test(p)) {
        result.education = p;
      }
    }
    return result;
  }

  // ===== 应用筛选条件 =====
  function applyFilters(job) {
    var f = state.filters;
    var kw = state.keywords;

    // 1. 关键字匹配
    if (kw.length > 0) {
      var matched = false;
      for (var i = 0; i < kw.length; i++) {
        if (job.title.indexOf(kw[i]) !== -1) {
          matched = true;
          break;
        }
      }
      if (!matched) return false;
    }

    // 2. 薪资筛选
    if (f.salaryMin || f.salaryMax) {
      var minS = parseInt(f.salaryMin) || 0;
      var maxS = parseInt(f.salaryMax) || 999;
      if (job.salary.max < minS || job.salary.min > maxS) return false;
    }

    // 3. 经验筛选
    if (f.experience && f.experience.length > 0 && job.experience) {
      if (f.experience.indexOf(job.experience) === -1) return false;
    }

    // 4. 学历筛选
    if (f.education && f.education.length > 0 && job.education) {
      if (f.education.indexOf(job.education) === -1) return false;
    }

    // 5. 区域筛选
    if (f.area && f.area.length > 0 && job.area) {
      var areaMatched = false;
      for (var i = 0; i < f.area.length; i++) {
        if (job.area.indexOf(f.area[i]) !== -1) {
          areaMatched = true;
          break;
        }
      }
      if (!areaMatched) return false;
    }

    return true;
  }

  // ===== 翻页 =====
  function clickNextPage() {
    var btn = document.querySelector(SELECTORS.nextBtn);
    if (btn) {
      btn.click();
      return true;
    }
    // 备用选择器
    var btn2 = document.querySelector('.pager_next_btn, .j-list .btn-next');
    if (btn2) {
      btn2.click();
      return true;
    }
    return false;
  }

  // ===== 判断是否有下一页 =====
  function hasMorePages() {
    var el = document.querySelector(SELECTORS.hasNext);
    if (el) return true;
    // 备用检测
    var pageBox = document.querySelector('.page-box');
    if (pageBox) {
      var text = pageBox.getAttribute('data-total-page');
      if (text) return true;
    }
    return document.querySelectorAll(SELECTORS.jobCard).length > 0;
  }

  // ===== 高亮匹配的卡片 =====
  function highlightJobCards(jobs) {
    for (var i = 0; i < jobs.length; i++) {
      var card = jobs[i].element;
      if (!card) continue;
      var match = applyFilters(jobs[i]);
      var delivered = state.deliveredJobs.has(jobs[i].key);

      card.classList.remove('autoapply-highlight', 'autoapply-delivered');

      if (delivered) {
        card.classList.add('autoapply-delivered');
        card.style.opacity = '0.6';
      } else if (match) {
        card.classList.add('autoapply-highlight');
      }
    }
  }

  // ===== 显示日志面板 =====
  function showLogEntry(type, message) {
    var existing = document.getElementById('autoapply-log-panel');
    if (!existing) {
      var panel = document.createElement('div');
      panel.id = 'autoapply-log-panel';
      panel.className = 'autoapply-log-panel';
      panel.innerHTML = '<div class="log-title">📋 投递日志</div><div class="log-entries"></div>';
      panel.style.cssText = 'position:fixed;top:20px;left:20px;z-index:99999;background:rgba(30,30,50,0.95);color:#e0e0e0;border-radius:10px;padding:14px 16px;font-size:12px;font-family:Menlo,Consolas,monospace;width:320px;max-height:280px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,0.3);backdrop-filter:blur(10px);';
      document.body.appendChild(panel);
    }

    var logPanel = document.getElementById('autoapply-log-panel');
    var entries = logPanel.querySelector('.log-entries');
    var div = document.createElement('div');
    div.className = 'log-entry' + (type === 'error' ? ' log-error' : type === 'success' ? ' log-success' : '');
    div.style.cssText = 'padding:3px 0;border-top:1px solid rgba(255,255,255,0.1);opacity:0.9;';
    div.textContent = '[' + new Date().toLocaleTimeString() + '] ' + message;
    entries.appendChild(div);

    // 保留最近 50 条
    while (entries.children.length > 50) {
      entries.removeChild(entries.firstChild);
    }
    logPanel.scrollTop = logPanel.scrollHeight;
  }

  // ===== 处理当前页 - 由 background 触发 =====
  function processCurrentPage(config) {
    if (!state.running) {
      showLogEntry('info', '自动投递未运行');
      return { success: false, reason: 'not_running' };
    }

    state.keywords = config.keywords;
    state.filters = config.filters;
    state.deliverySettings = config.deliverySettings;

    var jobs = extractJobCards();
    highlightJobCards(jobs);

    if (jobs.length === 0) {
      showLogEntry('info', '当前页面无职位卡片');
      if (hasMorePages()) {
        clickNextPage();
        showLogEntry('info', '已翻到下一页...');
      }
      return { success: true, delivered: 0, skipped: 0, noJobs: true };
    }

    var matched = [];
    var skipped = 0;
    var nonMatchStreak = 0;

    for (var i = 0; i < jobs.length; i++) {
      var job = jobs[i];

      if (state.deliveredJobs.has(job.key)) {
        skipped++;
        nonMatchStreak++;
        continue;
      }

      if (!applyFilters(job)) {
        skipped++;
        nonMatchStreak++;

        if (nonMatchStreak >= MAX_CONSECUTIVE_NON_MATCH) {
          showLogEntry('info', '连续 ' + nonMatchStreak + ' 个不匹配，准备翻页');
          break;
        }
        continue;
      }

      nonMatchStreak = 0;
      matched.push(job);
    }

    showLogEntry(matched.length > 0 ? 'success' : 'info',
      '找到 ' + matched.length + ' 个匹配职位（跳过 ' + skipped + ' 个）');

    if (matched.length === 0 && hasMorePages()) {
      showLogEntry('info', '本页无匹配，翻页...');
      clickNextPage();
      setTimeout(function() { showLogEntry('info', '已翻到下一页'); }, 3000);
    }

    return { success: true, matched: matched.length, skipped: skipped, jobs: matched.map(function(j) { return j.key; }) };
  }

  // ===== 监听消息 =====
  chrome.runtime.onMessage.addListener(function(msg, sender, sendResponse) {
    if (msg.action === 'processNextPage') {
      var result = processCurrentPage(msg.config);
      sendResponse(result);
      return false;
    }
    if (msg.action === 'getStatus') {
      sendResponse({
        running: state.running,
        keywords: state.keywords,
        pages: [document.querySelectorAll(SELECTORS.jobCard).length, 0]
      });
      return false;
    }
    if (msg.action === 'startDelivery') {
      state.running = true;
      sendResponse({ ok: true });
      updateIndicatorUI();
      showLogEntry('success', '自动投递已启动');
      return false;
    }
    if (msg.action === 'stopDelivery') {
      state.running = false;
      sendResponse({ ok: true });
      updateIndicatorUI();
      showLogEntry('info', '自动投递已停止');
      return false;
    }
    if (msg.action === 'loadDelivered') {
      loadDeliveredJobs();
      sendResponse({ count: state.deliveredJobs.size });
      return false;
    }
    if (msg.action === 'clearDelivered') {
      state.deliveredJobs.clear();
      localStorage.removeItem('boss_delivered_jobs');
      sendResponse({ ok: true });
      showLogEntry('info', '已清除投递记录');
      return false;
    }
  });

  // ===== 悬浮指示器 =====
  function injectIndicator() {
    var wrapper = document.createElement('div');
    wrapper.id = 'boss-autoapply-wrapper';
    wrapper.style.cssText = 'position:fixed;top:80px;right:20px;z-index:99999;';

    var circle = document.createElement('div');
    circle.id = 'boss-autoapply-indicator';
    circle.style.cssText = 'width:48px;height:48px;border-radius:50%;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);display:flex;align-items:center;justify-content:center;font-size:22px;cursor:pointer;box-shadow:0 4px 16px rgba(102,126,234,0.4);transition:all 0.3s;user-select:none;';
    circle.innerHTML = '🤖';
    circle.title = 'Boss直聘投递助手 - 点击切换状态';

    // Tooltip
    var tooltip = document.createElement('div');
    tooltip.id = 'aa-tooltip';
    tooltip.style.cssText = 'position:absolute;right:55px;top:50%;transform:translateY(-50%);background:rgba(20,20,40,0.92);color:#fff;padding:6px 10px;border-radius:6px;font-size:11px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity 0.2s;font-family:-apple-system,sans-serif;line-height:1.4;';
    tooltip.id = 'aa-tooltip';
    tooltip.innerHTML = '<div style="font-weight:600;margin-bottom:2px;">⚡ Boss投递助手</div><div id="aa-status-text">就绪，点击启动</div>';
    circle.appendChild(tooltip);

    circle.addEventListener('mouseenter', function() { tooltip.style.opacity = '1'; });
    circle.addEventListener('mouseleave', function() { tooltip.style.opacity = '0'; });
    circle.addEventListener('click', function() { toggleState(); });

    wrapper.appendChild(circle);
    document.body.appendChild(wrapper);
    updateIndicatorUI();
  }

  function toggleState() {
    if (state.running) {
      chrome.runtime.sendMessage({ action: 'stopDelivery' });
    } else {
      chrome.runtime.sendMessage({ action: 'startDelivery' });
    }
  }

  function updateIndicatorUI() {
    var indicator = document.getElementById('boss-autoapply-indicator');
    var statusText = document.getElementById('aa-status-text');
    if (!indicator || !statusText) return;

    if (state.running) {
      indicator.innerHTML = '⏸ <div id="aa-tooltip" style="position:absolute;right:55px;top:50%;transform:translateY(-50%);background:rgba(20,20,40,0.92);color:#fff;padding:6px 10px;border-radius:6px;font-size:11px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity 0.2s;font-family:-apple-system,sans-serif;line-height:1.4;"><div style="font-weight:600;margin-bottom:2px;">⚡ 运行中</div></div>';
      indicator.style.background = 'linear-gradient(135deg,#f093fb 0%,#f5576c 100%)';
      statusText.textContent = '运行中 - 点击停止';
      statusText.style.display = 'block';
    } else {
      indicator.innerHTML = '🤖 <div id="aa-tooltip" style="position:absolute;right:55px;top:50%;transform:translateY(-50%);background:rgba(20,20,40,0.92);color:#fff;padding:6px 10px;border-radius:6px;font-size:11px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity 0.2s;font-family:-apple-system,sans-serif;line-height:1.4;"><div style="font-weight:600;margin-bottom:2px;">⚡ Boss投递助手</div><div id="aa-status-text">就绪，点击启动</div></div>';
      indicator.style.background = 'linear-gradient(135deg,#667eea 0%,#764ba2 100%)';
      statusText.textContent = '就绪，点击启动';
    }
  }

  // ===== 监听状态广播 =====
  chrome.runtime.onMessage.addListener(function(msg) {
    if (msg.type) {
      if (msg.type === 'starting') {
        state.running = true;
      } else if (msg.type === 'stopped') {
        state.running = false;
      }
      updateIndicatorUI();
    }
  });

  // ===== CSS 注入 =====
  function injectCSS() {
    var style = document.createElement('style');
    style.textContent =
      '.job-card.autoapply-highlight {' +
      '  outline: 2px solid #667eea !important;' +
      '  outline-offset: 2px !important;' +
      '}' +
      '.job-card.autoapply-delivered {' +
      '  opacity: 0.5 !important;' +
      '}' +
      '.autoapply-log-panel .log-title {' +
      '  font-weight:600;margin-bottom:8px;font-size:13px;' +
      '}';
    document.head.appendChild(style);
  }

  // ===== 初始化 =====
  function init() {
    injectCSS();
    injectIndicator();
    showLogEntry('info', 'Boss直聘投递助手已加载');

    // 定期检查页面变化（Boss是SPA，会动态更新DOM）
    setInterval(function() {
      if (state.running) {
        var cards = document.querySelectorAll(SELECTORS.jobCard);
        if (cards.length > 0) {
          var jobs = extractJobCards();
          highlightJobCards(jobs);
        }
      }
    }, 5000);
  }

  // Boss直聘是SPA，DOM会动态加载，延迟初始化确保页面就绪
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
