# Boss直聘智能投递助手 - Chrome 浏览器扩展

## 功能概述

这是一款基于 Chrome Manifest V3 的浏览器扩展，用于在 [Boss直聘](https://www.zhipin.com) 上实现智能自动投递。主要特性：

- **关键字筛选** — 支持多个职位关键词，模糊匹配
- **多维条件过滤** — 从 Boss 直聘页面提取筛选字段（区域、薪资、经验、学历、公司规模等）
- **自动投递** — 自动打招呼消息，批量投递
- **防封号频率控制** — 随机间隔 + 批次休息 + 安全模式
- **自定义投递数量** — 可配置每日上限、批次大小、单次间隔
- **去重机制** — localStorage 记录已投递职位，避免重复
- **可视化面板** — Popup 悬浮面板实时监控投递状态

## 项目结构

```
boss/
├── manifest.json          # Chrome 扩展配置 (Manifest V3)
├── background.js           # 后台服务: 调度引擎、频率控制
├── content.js              # 内容脚本: 解析页面、提取卡片、执行投递
├── popup.html              # 主控制面板 UI
├── popup.js                # 控制面板逻辑
├── options.html            # 高级设置页 UI
├── options.js              # 高级设置页逻辑
├── styles/
│   ├── popup.css           # 控制面板样式
│   ├── options.css         # 高级设置页样式
│   └── content.css         # 注入页面的增强样式
├── libs/
│   └── jquery.min.js       # jQuery (需下载: https://code.jquery.com/jquery-3.7.1.min.js)
└── icons/                  # 图标资源 (需自行添加)
    ├── icon16.png
    ├── icon48.png
    └── icon128.png
```

## 安装步骤

### 1. 下载依赖

将 jQuery 放到 `libs/` 目录:
```bash
curl -o libs/jquery.min.js https://code.jquery.com/jquery-3.7.1.min.js
```

### 2. 准备图标 (可选)

在 `icons/` 目录下放置 16x16, 48x48, 128x128 的 PNG 图标。
也可以先用 emoji 替代，或生成简单图标:

可以用在线工具生成: https://favicon.io/favicon-converter/

### 3. 加载到 Chrome

1. 打开 Chrome，访问 `chrome://extensions/`
2. 右上角开启「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择本项目的根目录 (`C:\Users\46375\Documents\boss`)
5. 扩展即加载成功

### 4. 使用方式

1. 打开 [Boss直聘](https://www.zhipin.com/hangzhou/) 首页
2. 在搜索框输入你的目标职位（如「前端工程师」）进行搜索
3. 点击扩展图标打开控制面板
4. 设置关键字、筛选条件、投递数量
5. 点击「启动自动投递」

## 核心参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 每日最大投递数 | 50 | 超过后自动停止，次日重置 |
| 间隔最小值 | 10秒 | 每次投递的最小等待时间 |
| 间隔最大值 | 30秒 | 每次投递的最大等待时间（实际延迟会在此范围随机） |
| 批次大小 | 5 | 每批投递几个后休息 |
| 批次间休息 | 2分钟 | 模拟人类休息时间 |
| 安全模式 | 开启 | 启用后使用更保守的频率策略 |

## ⚠️ 注意事项

1. **请勿过度频繁投递** — 推荐的每天不超过 50 个
2. **安全模式是关键** — 务必保持开启，它会大幅增加随机间隔
3. **定期手动操作** — 不要让扩展长时间连续运行
4. **Boss直聘页面结构可能变化** — 如果选择器失效，需要更新 `content.js` 中的选择器
5. **免责声明** — 本工具仅供个人学习使用，请遵守 Boss直聘的服务条款和使用政策

## 技术要点

- **Manifest V3** — 使用 Service Worker 替代 Background Page
- **Chrome Storage API** — 持久化所有用户配置
- **Content Script Injection** — 解析 DOM 提取职位数据
- **Rate Limiting** — 双重随机间隔 + 批次暂停防封号
- **Deduplication** — localStorage 记录已投递职位名称+公司
