// ============================================================
// Content Script - Boss直聘页面解析与增强
// 核心能力:
//   1. 提取Boss自带筛选字段（区域、薪资段、经验档位等）
//   2. 职位卡片解析 + 高亮标记
//   3. 投递结果实时上报 popup
//   4. 悬浮状态指示器 + 日志面板
// ============================================================

(function() {
  'use strict';

  // ===== 运行时状态 =====
  var state = {
    running: false,
    keywords: [],
    filters: {},
    currentPage: 1,
    consecutiveNonMatch: 0,
    deliveredJobs: new Set(),
    // 从页面提取的筛选数据
    filterData: { areas: [], salaryRanges: [] }
  };

  // 每天只提取一次筛选数据
  var filterDataDate = '';

  // ===== DOM 选择器 =====
  var S = {
    jobCard: '.job-card',
    jobTitle: '.job-card .job-card-left .job-card-main a',
    jobSalary: '.job-card .information span:first-child',
    jobCompany: '.job-card .company-info h3',
    jobArea: '.job-card .job-area',
    jobInfo: '.job-card .job-card-main p',
    nextBtn: '.btn.page-next, .pager_next_btn',
    hasNext: '.page-box .pager_next:not(.disabled)',
    // Boss直聘筛选面板
    filterTrigger: '.job_filter_wrapper .filter-trigger, .job_filter .filter-trigger',
    filterAreaPanel: '.area-list li, [data-field="area"] li, .job_filter .area-list li',
    filterSalaryPanel: '.salary-range li, [data-field="salary"] li, .j-filter .salary-box .label'
  };

  // ===== 已投递记录 =====
  function loadDelivered() {
    try {
      var stored = localStorage.getItem('boss_delivered_jobs');
      if (stored) state.deliveredJobs = new Set(JSON.parse(stored));
    } catch(e) {}
  }

  function markDelivered(key) {
    state.deliveredJobs.add(key);
    if (state.deliveredJobs.size > 300) {
      var arr = [];
      state.deliveredJobs.forEach(function(v) { arr.push(v); });
      state.deliveredJobs = new Set(arr.slice(-300));
    }
    localStorage.setItem('boss_delivered_jobs', JSON.stringify([...state.deliveredJobs]));
  }

  loadDelivered();

  // ===== 提取Boss页面筛选字段 =====
  function extractFilterData() {
    // 同一天只提取一次
    var today = new Date().toISOString().slice(0, 10);
    if (filterDataDate === today && (state.filterData.areas.length > 0 || state.filterData.salaryRanges.length > 0)) {
      return;
    }

    console.log('[BossAutoApply] 开始提取页面筛选字段...');

    // 区域筛选 — 在搜索页面的左侧/顶部筛选栏中提取区域名
    var areaEls = document.querySelectorAll('.filter-areabox .filter-label, .job_filter_wrapper [data-field="area"], .filter-list .label-text, [class*="area"] [class*="label"]');
    var areas = [];
    areaEls.forEach(function(el) {
      var text = el.textContent.trim();
      if (text && text.length < 20 && text.indexOf('年') === -1 && text.indexOf('万') === -1) {
        // 排除薪资数字
        if (/^[0-9]+/.test(text)) return;
        areas.push(text);
      }
    });

    // 更通用的区域提取 — 从筛选面板标签中提取
    if (areas.length === 0) {
      var labelEls = document.querySelectorAll('.filter-body .label, .filter-panel .label, .job_filter .filter-section .item-text, .filter-item');
      labelEls.forEach(function(el) {
        var text = el.textContent.trim();
        // 常见的城市/区域名不会太长
        if (/^(上海|北京|深圳|广州|杭州|成都|重庆|武汉|苏州|西安|南京|长沙|天津|郑州|青岛|宁波|厦门|合肥|福州|无锡|济南|东莞|佛山|常州|南通|徐州|南昌|沈阳|昆明|哈尔滨|贵阳|兰州|太原|石家庄|长春|海口|南宁|乌鲁木齐|呼和浩特|银川|西宁|拉萨)$/.test(text) ||
            text.length >= 2 && text.length <= 6 && !/[\d\w]/.test(text)) {
          // 避免重复
          if (areas.indexOf(text) === -1) {
            areas.push(text);
          }
        }
      });
    }

    // 尝试从URL中的路径推断当前城市
    var pathMatch = window.location.pathname.match(/\/([^/]+)/);
    if (pathMatch) {
      var cityFromUrl = pathMatch[1];
      // 如果不是 job/list 这类API路径
      if (!/^(job|list|api)/.test(cityFromUrl)) {
        var alreadyAdded = false;
        for (var i = 0; i < areas.length; i++) {
          if (areas[i].indexOf(cityFromUrl) !== -1) {
            alreadyAdded = true;
            break;
          }
        }
        if (!alreadyAdded) {
          areas.unshift(cityFromUrl);
        }
      }
    }

    state.filterData.areas = areas.slice(0, 50); // 限制数量

    // 薪资段 — 从筛选面板的薪资选项提取
    var salaryLabels = document.querySelectorAll('.salary-box .label, [class*="salary"] .label-text, .job_filter .salary [class*="label"]');
    var salaryRanges = [];
    salaryLabels.forEach(function(el) {
      var text = el.textContent.trim();
      if (/^\d+[-~]\d+/g.test(text.replace(/\D/g, '')) || /(\d+-(\d+)).{0,4}k/i.test(text.toLowerCase())) {
        salaryRanges.push(text);
      } else if (/^\d+k?\s*-\s*\d+k?$/i.test(text.trim())) {
        salaryRanges.push(text.trim());
      }
    });

    // 备用：从职位卡片的薪资文本中收集
    if (salaryRanges.length === 0) {
      var salaryEls = document.querySelectorAll('.job-card .information span, .job-card .salary');
      var seenSalaries = {};
      salaryEls.forEach(function(el) {
        var text = el.textContent.trim();
        if (text && seenSalaries[text] === undefined) {
          seenSalaries[text] = true;
          salaryRanges.push(text);
        }
      });
    }

    state.filterData.salaryRanges = salaryRanges.slice(0, 20);

    // 保存今天的提取结果
    filterDataDate = today;

    console.log('[BossAutoApply] 筛选数据: 区域', areas.length, '个,', '薪资段', salaryRanges.length, '个');

    // 上报给 popup
    chrome.runtime.sendMessage({
      action: 'sendFilterData',
      data: { areas: state.filterData.areas, salaryRanges: state.filterData.salaryRanges }
    }).catch(function(){});
  }

  // ===== 提取职位卡片 =====
  function extractJobCards() {
    var cards = document.querySelectorAll(S.jobCard);
    var jobs = [];

    cards.forEach(function(card) {
      try {
        var titleEl = card.querySelector(S.jobTitle);
        if (!titleEl) {
          // 回退: 尝试更宽松的匹配
          titleEl = card.querySelector('a[href*="job_detail"]');
        }
        if (!titleEl) return;

        var title = titleEl.textContent.trim();
        var salaryEl = card.querySelector(S.jobSalary);
        var salaryText = salaryEl ? salaryEl.textContent.trim() : '';
        var companyEl = card.querySelector(S.jobCompany);
        var company = companyEl ? companyEl.textContent.trim() : '';
        var areaEl = card.querySelector(S.jobArea);
        var area = areaEl ? areaEl.textContent.trim() : '';
        var infoEl = card.querySelector(S.jobInfo);
        var infoText = infoEl ? infoEl.textContent.trim() : '';

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

    return jobs;
  }

  function parseSalary(text) {
    var m = text.match(/(\d+)-(\d+)/g);
    if (m && m[0]) {
      var nums = m[0].split('-').map(Number);
      return { min: nums[0], max: nums[1], unit: 'K/月' };
    }
    return { min: 0, max: 0 };
  }

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
    var f = state.filters || {};
    var kw = state.keywords || [];

    // 关键字匹配
    if (kw.length > 0) {
      var matched = false;
      for (var i = 0; i < kw.length; i++) {
        if (job.title.indexOf(kw[i]) !== -1) { matched = true; break; }
      }
      if (!matched) return false;
    }

    // 薪资
    if (f.salaryMin || f.salaryMax) {
      var minS = parseInt(f.salaryMin) || 0;
      var maxS = parseInt(f.salaryMax) || 999;
      if (job.salary.max < minS || job.salary.min > maxS) return false;
    }

    // 经验
    if (f.experience && f.experience.length > 0 && job.experience) {
      if (f.experience.indexOf(job.experience) === -1) return false;
    }

    // 学历
    if (f.education && f.education.length > 0 && job.education) {
      if (f.education.indexOf(job.education) === -1) return false;
    }

    // 区域
    if (f.area && f.area.length > 0 && job.area) {
      var areaMatched = false;
      for (var i = 0; i < f.area.length; i++) {
        if (job.area.indexOf(f.area[i]) !== -1) { areaMatched = true; break; }
      }
      if (!areaMatched) return false;
    }

    return true;
  }

  // ===== 高亮卡片 =====
  function highlightCards(jobs) {
    for (var i = 0; i < jobs.length; i++) {
      var card = jobs[i].element;
      if (!card) continue;
      var delivered = state.deliveredJobs.has(jobs[i].key);
      var match = applyFilters(jobs[i]);

      card.classList.remove('autoapply-highlight', 'autoapply-delivered');

      if (delivered) {
        card.classList.add('autoapply-delivered');
        card.style.opacity = '0.55';
      } else if (match) {
        card.classList.add('autoapply-highlight');
      }
    }
  }

  // ===== 翻页 =====
  function clickNextPage() {
    var btn = document.querySelector(S.nextBtn);
    if (btn) { btn.click(); return true; }

    var pageBox = document.querySelector('.page-box');
    if (pageBox) {
      var totalStr = pageBox.getAttribute('data-total-page');
      var currentStr = pageBox.getAttribute('data-current-page');
      if (totalStr && currentStr && parseInt(currentStr) < parseInt(totalStr)) {
        // 通过模拟滚动到页脚触发翻页
        window.scrollTo(0, document.body.scrollHeight);
        return true;
      }
    }
    return false;
  }

  function hasMorePages() {
    if (document.querySelector(S.hasNext)) return true;
    var pageBox = document.querySelector('.page-box');
    if (pageBox) {
      var total = pageBox.getAttribute('data-total-page');
      var current = pageBox.getAttribute('data-current-page');
      if (total && current && parseInt(current) < parseInt(total)) return true;
    }
    return false;
  }

  // ===== 处理当前页 =====
  function processCurrentPage(config) {
    if (!state.running) return { success: false, reason: 'not_running' };

    state.keywords = config.keywords || [];
    state.filters = config.filters || {};

    // 首次或页面变化时提取筛选数据
    extractFilterData();

    var jobs = extractJobCards();
    highlightCards(jobs);

    if (jobs.length === 0) {
      showLog('info', '当前页面无职位卡片');
      return { success: true, noJobs: true };
    }

    var matched = [];
    var skipped = 0;
    var nonMatchStreak = 0;
    var MAX_STREAK = 8;

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
        if (nonMatchStreak >= MAX_STREAK) {
          showLog('info', '连续' + nonMatchStreak + '个不匹配，准备翻页');
          break;
        }
        continue;
      }

      nonMatchStreak = 0;
      matched.push(job);
    }

    // 如果有匹配的
    if (matched.length > 0) {
      // 上报给 popup 显示
      chrome.runtime.sendMessage({
        action: 'deliveryResult',
        result: {
          type: 'match',
          title: matched[0].title + ' | ' + matched[0].company,
          meta: matched[0].salaryText + ' · ' + (matched[0].experience || ''),
          time: new Date().toLocaleTimeString()
        }
      }).catch(function(){});

      showLog('success', '命中 ' + matched.length + ' 个职位');
      for (var j = 0; j < Math.min(matched.length, 3); j++) {
        markDelivered(matched[j].key);
      }
    } else {
      showLog('info', '本页无匹配职位（跳过 ' + skipped + ' 个）');
      if (hasMorePages()) {
        clickNextPage();
        setTimeout(function() {
          showLog('info', '已翻页');
          extractFilterData();
        }, 3000);
      }
    }

    return { success: true, matched: matched.length, skipped: skipped, jobs: matched.map(function(m) { return m.key; }) };
  }

  // ===== 日志面板 =====
  var logShown = false;
  function showLog(type, message) {
    var panel = document.getElementById('aa-log-panel');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'aa-log-panel';
      panel.innerHTML = '<div id="aa-log-title" style="font-weight:600;margin-bottom:8px;font-size:13px;">📋 运行日志</div><div id="aa-log-entries"></div>';
      panel.style.cssText = 'position:fixed;top:20px;left:20px;z-index:99999;background:rgba(20,20,40,0.92);color:#ccc;border-radius:10px;padding:12px 14px;font-size:11px;font-family:Menlo,Consolas,monospace;width:280px;max-height:240px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,0.25);backdrop-filter:blur(8px);line-height:1.5;';
      document.body.appendChild(panel);
      logShown = true;
    }

    var entries = panel.querySelector('#aa-log-entries');
    var div = document.createElement('div');
    div.style.cssText = 'padding:2px 0;border-top:1px solid rgba(255,255,255,0.08);opacity:0.85;';
    if (type === 'success') div.style.color = '#69f0ae';
    if (type === 'error') div.style.color = '#ff5252';
    div.textContent = '[' + new Date().toLocaleTimeString() + '] ' + message;
    entries.appendChild(div);

    while (entries.children.length > 60) entries.removeChild(entries.firstChild);
    panel.scrollTop = panel.scrollHeight;
  }

  // ===== 悬浮指示器 =====
  function injectIndicator() {
    var wrapper = document.createElement('div');
    wrapper.id = 'aa-wrapper';
    wrapper.style.cssText = 'position:fixed;top:80px;right:20px;z-index:99999;display:flex;flex-direction:column;align-items:center;gap:8px;';

    var circle = document.createElement('div');
    circle.id = 'aa-indicator';
    circle.style.cssText = 'width:44px;height:44px;border-radius:50%;background:linear-gradient(135deg,#667eea,#764ba2);display:flex;align-items:center;justify-content:center;font-size:20px;cursor:pointer;box-shadow:0 4px 12px rgba(102,126,234,0.4);transition:all 0.3s;user-select:none;';
    circle.innerHTML = '🤖';
    circle.title = '点击切换自动投递状态';

    var countBadge = document.createElement('div');
    countBadge.id = 'aa-count';
    countBadge.style.cssText = 'background:rgba(0,0,0,0.7);color:white;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;white-space:nowrap;';
    countBadge.textContent = '0/日';

    var tooltip = document.createElement('div');
    tooltip.id = 'aa-tooltip';
    tooltip.style.cssText = 'position:absolute;right:50px;top:50%;transform:translateY(-50%);background:rgba(20,20,40,0.92);color:#fff;padding:5px 8px;border-radius:6px;font-size:11px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity 0.2s;font-family:-apple-system,sans-serif;';
    tooltip.textContent = '就绪';

    circle.appendChild(tooltip);
    wrapper.appendChild(tooltip); // positioned above circle
    wrapper.appendChild(circle);
    wrapper.appendChild(countBadge);
    document.body.appendChild(wrapper);

    circle.addEventListener('click', toggleRunning);
    circle.addEventListener('mouseenter', function() { tooltip.style.opacity = '1'; });
    circle.addEventListener('mouseleave', function() { tooltip.style.opacity = '0'; });

    updateIndicator();
  }

  function toggleRunning() {
    state.running = !state.running;
    var action = state.running ? 'startDelivery' : 'stopDelivery';
    chrome.runtime.sendMessage({ action: action });
    updateIndicator();
  }

  function updateIndicator() {
    var circle = document.getElementById('aa-indicator');
    var badge = document.getElementById('aa-count');
    var tip = document.getElementById('aa-tooltip');
    if (!circle || !badge || !tip) return;

    if (state.running) {
      circle.style.background = 'linear-gradient(135deg,#f093fb,#f5576c)';
      circle.innerHTML = '⏸';
      tip.textContent = '运行中 - 点击停止';
      badge.textContent = '⏳';
    } else {
      circle.style.background = 'linear-gradient(135deg,#667eea,#764ba2)';
      circle.innerHTML = '🤖';
      tip.textContent = '就绪 - 点击启动';
      badge.textContent = '0/日';
    }
  }

  // ===== CSS 注入 =====
  function injectCSS() {
    var style = document.createElement('style');
    style.textContent =
      '.job-card.autoapply-highlight {' +
      '  outline: 2px solid #667eea !important;' +
      '  outline-offset: 2px !important;' +
      '  box-shadow: 0 0 0 0 rgba(102,126,234,0.3) !important;' +
      '}' +
      '.job-card.autoapply-delivered {' +
      '  opacity: 0.55 !important;' +
      '}';
    document.head.appendChild(style);
  }

  // ===== 初始化 =====
  function init() {
    injectCSS();
    injectIndicator();
    showLog('info', 'Boss投递助手已加载');

    // Boss是SPA，定期检查DOM变化
    setInterval(function() {
      if (state.running) {
        var cards = document.querySelectorAll(S.jobCard);
        if (cards.length > 0) {
          var jobs = extractJobCards();
          highlightCards(jobs);
        }
      }
    }, 5000);
  }

  // ===== 消息监听 =====
  chrome.runtime.onMessage.addListener(function(msg, sender, sendResponse) {
    if (msg.action === 'processNextPage') {
      var result = processCurrentPage(msg.config);
      sendResponse(result);
      return false;
    }
    if (msg.action === 'getStatus') {
      sendResponse({ running: state.running });
      return false;
    }
    if (msg.action === 'startDelivery') {
      state.running = true;
      updateIndicator();
      showLog('success', '自动投递已启动');
      sendResponse({ ok: true });
      return false;
    }
    if (msg.action === 'stopDelivery') {
      state.running = false;
      updateIndicator();
      showLog('info', '已停止');
      sendResponse({ ok: true });
      return false;
    }
  });

  // 接收 background 的状态广播
  chrome.runtime.onMessage.addListener(function(msg) {
    if (msg.state) {
      updateIndicator();
    }
  });

  // 启动
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
