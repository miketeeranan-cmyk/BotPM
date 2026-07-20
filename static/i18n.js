/* Lightweight client-side i18n for the Team Sheet UI (English / Simplified Chinese).
   Values are either plain strings with {param} placeholders, or functions
   (params) => string for pluralization / interpolation. Locale is persisted in
   localStorage and the language toggle reloads the page to re-render cleanly. */
(function () {
  const LOCALE_KEY = "relay.locale";

  const TRANSLATIONS = {
    en: {
      // --- shared chrome ---
      "app.quit_overlay": "The app has quit. You can close this tab.",
      "brand": "Team Sheet",
      "nav.workspace": "Workspace",
      "nav.send": "Send",
      "nav.scan": "Scan",
      "status.idle": "Idle",
      "status.running": "Running",
      "common.back": "Back",
      "common.cancel": "Cancel",
      "common.stop": "Stop",
      "common.stopping": "Stopping...",
      "common.quit": "Quit",
      "common.refresh_dashboard": "Refresh Dashboard",
      "archive.now_btn": "Move sent to Summary now",
      "archive.confirm": "Move all already-sent (red) recruits into the Summary tab now? They'll be removed from their source tabs.",
      "archive.moving": "Moving...",
      "archive.done": ({ n = 0 } = {}) => `Moved ${n} sent recruit(s) to Summary.`,
      "archive.none": "No sent recruits to move.",
      "archive.failed": ({ err = "" } = {}) => `Failed to move sent recruits: ${err}`,
      "common.select_vj_first": "Select a VJ first.",
      // --- VJ picker ---
      "vj.eyebrow": "VJ",
      "vj.choose": "Choose VJ",
      "vj.logout": "Log out",
      "vj.logging_out": "Logging out...",
      "vj.modal_title": "Select a VJ",
      "vj.modal_subtitle": "Browse by team, or search by name.",
      "vj.search_ph": "Search VJs…",
      "vj.loading": "Loading VJs…",
      "vj.no_teams": "No teams found in the sheet.",
      "vj.no_teams_match": "No teams match.",
      "vj.no_vjs_match": "No VJs match.",
      "vj.no_vjs_team": "No VJs found for this team.",
      "vj.from_team": ({ team = "" } = {}) => `From ${team}.`,
      // --- fields / options ---
      "field.from_date": "From date",
      "field.imported_on": "Imported on",
      "field.target": "Target",
      "field.message": "Message",
      "opt.all_dates": "All dates",
      "opt.new": "Never contacted",
      "opt.unseen_followup": "Unseen (follow-up)",
      "opt.seen_followup": "Seen (follow-up)",
      // --- panels / stats / table ---
      "panel.scan_status": "Scan status",
      "panel.recipients": "Recipients",
      "panel.log": "Log",
      "panel.send_message": "Send a message",
      "stat.total": "Total",
      "stat.sent": "Sent",
      "stat.chat": "Chat",
      "stat.unseen": "Unseen",
      "stat.seen": "Seen",
      "stat.replied": "Replied",
      "th.name": "Name",
      "th.date": "Date",
      "th.status": "Status",
      "users.empty": "No users found for this lady.",
      "users.loading": "Loading users…",
      "users.select_vj": "Select a VJ to get started.",
      // --- scan page ---
      "scan.h1": "Scan",
      "scan.subtitle": "Refresh delivery status for your recipients",
      "scan.dry_run": "Dry run — opens each profile but never writes a status",
      "scan.btn": "Scan Status",
      "scan.btn_running": "Scanning...",
      "scan.preview": ({ count = 0 } = {}) => `Will scan ${count} recipient${count === 1 ? "" : "s"}.`,
      "scan.stop_log": "Stopping -- in-progress tabs will wrap up in place and close, then the run halts. The browser stays open.",
      "quit.scan_in_progress": "A scan is still in progress. Click Stop first, then Quit.",
      // --- send page ---
      "send.h1": "Send",
      "send.subtitle": "Message recipients across your team sheets",
      "send.dry_run": "Dry run — opens each profile but never sends a message",
      "send.btn": "Send Messages",
      "send.btn_running": "Sending...",
      "send.msg_ph_new": "What message would you like to send to never-chatted users?",
      "send.msg_ph_followup": "Follow-up message — appended to their existing message history.",
      "send.preview": ({ count = 0 } = {}) => `Will send to ${count} recipient${count === 1 ? "" : "s"}.`,
      "send.stop_log": "Stopping -- tabs already sending will finish and close; no new sends will start. The browser stays open.",
      "quit.send_in_progress": "A send is still in progress. Click Stop first, then Quit.",
      // --- import modal ---
      "import.modal_title": "Import Recruits",
      "import.modal_subtitle": "Choose a tracker sheet to pull candidates from.",
      "import.min_level": "Min level",
      "import.any_ph": "Any",
      "import.select_n_label": "Select first",
      "import.count_ph": "N",
      "import.select_n": "Select",
      "import.deselect_all": "Deselect all",
      "import.done": "Done",
      "import.open_btn": "Import Recruits from Tracker Sheet…",
      "import.loading_sheets": "Loading sheets…",
      "import.no_sheets": "No sheets found.",
      "import.filter_hint": "Type a minimum level to filter, or leave blank for everyone.",
      "import.loading_candidates": "Loading candidates…",
      "import.no_candidates": "No candidates match.",
      "import.in_roster": "In roster",
      "import.selected": ({ n = 0 } = {}) => `${n} selected`,
      "import.level": ({ lvl = "" } = {}) => `Lv ${lvl}`,
      "import.sheet_label": ({ tab = "" } = {}) => `Sheet: ${tab}`,
      "import.showing": ({ shown = 0, total = 0 } = {}) => `Showing ${shown} of ${total} matches — raise the min level to narrow it down.`,
      "import.imported": ({ added = 0, skipped = 0 } = {}) => `imported ${added} recruit(s) (${skipped} already present, skipped)`,
      "import.removed": ({ removed = 0 } = {}) => `removed ${removed} recruit(s)`,
      "import.roster_summary": ({ lady = "", parts = [] } = {}) => `${lady}'s roster: ${parts.join(", ")}.`,
      // --- dashboard / quit ---
      "dash.choose_vj": "Choose a VJ first.",
      "dash.refreshing": "Refreshing...",
      "quit.confirm": "Quit the app and close all browser tabs?",
      "quit.quitting": "Quitting...",
      // --- launch / update (desktop app) ---
      "title.launch": "Team Sheet",
      "launch.checking": "Checking for updates...",
      "launch.update_available": "Update available",
      "launch.version_line": ({ latest = "", current = "" } = {}) => `Version ${latest} is ready. You have ${current}.`,
      "launch.update": "Update",
      "launch.skip": "Skip",
      "launch.starting": "Starting download...",
      "launch.downloading": ({ done = "0", total = "0" } = {}) => `Downloading... ${done} / ${total} MB`,
      "launch.restarting": "Restarting Team Sheet...",
      "launch.failed": ({ err = "" } = {}) => `Update failed. ${err}`,
      "launch.setup_title": "Setup needed",
      "launch.creds_missing": "credentials.json was not found. Put it in this folder, then click Retry.",
      "launch.retry": "Retry",
      "launch.chrome_title": "Google Chrome not found",
      "launch.chrome_missing": "Team Sheet uses the Chrome installed on this PC to send messages. Install Chrome, then restart.",
      "launch.download_chrome": "Download Chrome",
      "launch.continue": "Continue anyway",
      // --- Stripchat session ---
      "session.login": "Log in to Stripchat",
      "session.opening": "Opening browser...",
      "session.done": "Done — save login",
      "session.saving": "Saving...",
      "session.import": "Import session from Chrome",
      "session.importing": "Importing...",
      // --- badges ---
      "badge.pending": "Not sent",
      "badge.sent": "Sent",
      "badge.unseen": "Unseen",
      "badge.seen": "Seen",
      "badge.replied": "Replied",
      "badge.dry_run": "Would send (dry run)",
      "badge.error": "Error",
      "badge.scan_processing": "Scanning...",
      "badge.scan_chat": "Existing chat",
      "badge.send_processing": "Sending...",
      "badge.send_chat": "Existing chat (skipped)",
      "badge.dead": "Dead account (no reply after 2 sends)",
      // --- errors (static wrapper words; interpolated server text passes through) ---
      "err.server_returned": ({ status = "" } = {}) => `Server returned ${status}`,
      "err.reach_server": ({ msg = "" } = {}) => `Couldn't reach the server: ${msg}`,
      "err.load_vjs": ({ msg = "" } = {}) => `Couldn't load VJs: ${msg}`,
      "err.load_users": ({ msg = "" } = {}) => `Couldn't load users for this lady: ${msg}`,
      "err.load_sheets": ({ msg = "" } = {}) => `Couldn't load sheets: ${msg}`,
      "err.load_candidates": ({ msg = "" } = {}) => `Couldn't load candidates: ${msg}`,
      "err.save_roster": ({ msg = "" } = {}) => `Couldn't save roster changes: ${msg}`,
      "err.refresh_dashboard": ({ err = "" } = {}) => `Failed to refresh dashboard: ${err}`,
      // --- document titles ---
      "title.scan": "Team Sheet -- Scan",
      "title.send": "Team Sheet -- Send",
    },

    zh: {
      // --- shared chrome ---
      "app.quit_overlay": "应用已退出，你可以关闭此标签页。",
      "brand": "团队表",
      "nav.workspace": "工作区",
      "nav.send": "发送",
      "nav.scan": "扫描",
      "status.idle": "空闲",
      "status.running": "运行中",
      "common.back": "返回",
      "common.cancel": "取消",
      "common.stop": "停止",
      "common.stopping": "正在停止…",
      "common.quit": "退出",
      "common.refresh_dashboard": "刷新面板",
      "archive.now_btn": "立即移动已发送至汇总",
      "archive.confirm": "现在将所有已发送（红色）的候选人移动到“汇总”标签页吗？它们将从原始标签页中删除。",
      "archive.moving": "正在移动…",
      "archive.done": ({ n = 0 } = {}) => `已将 ${n} 位已发送候选人移动到汇总。`,
      "archive.none": "没有可移动的已发送候选人。",
      "archive.failed": ({ err = "" } = {}) => `移动已发送候选人失败：${err}`,
      "common.select_vj_first": "请先选择一位 VJ。",
      // --- VJ picker ---
      "vj.eyebrow": "VJ",
      "vj.choose": "选择 VJ",
      "vj.logout": "登出",
      "vj.logging_out": "正在登出…",
      "vj.modal_title": "选择一位 VJ",
      "vj.modal_subtitle": "按团队浏览，或按名称搜索。",
      "vj.search_ph": "搜索 VJ…",
      "vj.loading": "正在加载 VJ…",
      "vj.no_teams": "表中未找到任何团队。",
      "vj.no_teams_match": "没有匹配的团队。",
      "vj.no_vjs_match": "没有匹配的 VJ。",
      "vj.no_vjs_team": "该团队下未找到 VJ。",
      "vj.from_team": ({ team = "" } = {}) => `来自 ${team}。`,
      // --- fields / options ---
      "field.from_date": "起始日期",
      "field.imported_on": "导入日期",
      "field.target": "目标",
      "field.message": "消息内容",
      "opt.all_dates": "所有日期",
      "opt.new": "从未联系",
      "opt.unseen_followup": "未读（跟进）",
      "opt.seen_followup": "已读（跟进）",
      // --- panels / stats / table ---
      "panel.scan_status": "扫描状态",
      "panel.recipients": "接收人",
      "panel.log": "日志",
      "panel.send_message": "发送消息",
      "stat.total": "总数",
      "stat.sent": "已发送",
      "stat.chat": "会话",
      "stat.unseen": "未读",
      "stat.seen": "已读",
      "stat.replied": "已回复",
      "th.name": "姓名",
      "th.date": "日期",
      "th.status": "状态",
      "users.empty": "未找到该 lady 的用户。",
      "users.loading": "正在加载用户…",
      "users.select_vj": "请先选择一位 VJ 开始。",
      // --- scan page ---
      "scan.h1": "扫描",
      "scan.subtitle": "刷新接收人的送达状态",
      "scan.dry_run": "试运行 — 打开每个资料页但不写入任何状态",
      "scan.btn": "扫描状态",
      "scan.btn_running": "正在扫描…",
      "scan.preview": ({ count = 0 } = {}) => `将扫描 ${count} 位接收人。`,
      "scan.stop_log": "正在停止 —— 进行中的标签页会就地完成并关闭，随后运行停止。浏览器将保持打开。",
      "quit.scan_in_progress": "扫描仍在进行中。请先点击“停止”，再退出。",
      // --- send page ---
      "send.h1": "发送",
      "send.subtitle": "向各团队表中的接收人发送消息",
      "send.dry_run": "试运行 — 打开每个资料页但不实际发送消息",
      "send.btn": "发送消息",
      "send.btn_running": "正在发送…",
      "send.msg_ph_new": "你想向从未聊过的用户发送什么消息？",
      "send.msg_ph_followup": "跟进消息 — 将追加到他们已有的消息记录中。",
      "send.preview": ({ count = 0 } = {}) => `将发送给 ${count} 位接收人。`,
      "send.stop_log": "正在停止 —— 已在发送的标签页会完成并关闭；不会开始新的发送。浏览器将保持打开。",
      "quit.send_in_progress": "发送仍在进行中。请先点击“停止”，再退出。",
      // --- import modal ---
      "import.modal_title": "导入新人",
      "import.modal_subtitle": "选择一个追踪表以拉取候选人。",
      "import.min_level": "最低等级",
      "import.any_ph": "不限",
      "import.select_n_label": "选择前",
      "import.count_ph": "数量",
      "import.select_n": "选择",
      "import.deselect_all": "取消全选",
      "import.done": "完成",
      "import.open_btn": "从追踪表导入新人…",
      "import.loading_sheets": "正在加载表格…",
      "import.no_sheets": "未找到表格。",
      "import.filter_hint": "输入最低等级以筛选，留空则包含所有人。",
      "import.loading_candidates": "正在加载候选人…",
      "import.no_candidates": "没有匹配的候选人。",
      "import.in_roster": "已在名单",
      "import.selected": ({ n = 0 } = {}) => `已选 ${n} 个`,
      "import.level": ({ lvl = "" } = {}) => `等级 ${lvl}`,
      "import.sheet_label": ({ tab = "" } = {}) => `表格：${tab}`,
      "import.showing": ({ shown = 0, total = 0 } = {}) => `显示 ${total} 个匹配中的 ${shown} 个 —— 提高最低等级以缩小范围。`,
      "import.imported": ({ added = 0, skipped = 0 } = {}) => `已导入 ${added} 位新人（${skipped} 位已存在，已跳过）`,
      "import.removed": ({ removed = 0 } = {}) => `已移除 ${removed} 位新人`,
      "import.roster_summary": ({ lady = "", parts = [] } = {}) => `${lady} 的名单：${parts.join("，")}。`,
      // --- dashboard / quit ---
      "dash.choose_vj": "请先选择一位 VJ。",
      "dash.refreshing": "正在刷新…",
      "quit.confirm": "退出应用并关闭所有浏览器标签页？",
      "quit.quitting": "正在退出…",
      // --- launch / update (desktop app) ---
      "title.launch": "Team Sheet",
      "launch.checking": "正在检查更新…",
      "launch.update_available": "有可用更新",
      "launch.version_line": ({ latest = "", current = "" } = {}) => `${latest} 版本已就绪，当前版本为 ${current}。`,
      "launch.update": "更新",
      "launch.skip": "跳过",
      "launch.starting": "开始下载…",
      "launch.downloading": ({ done = "0", total = "0" } = {}) => `正在下载… ${done} / ${total} MB`,
      "launch.restarting": "正在重启 Team Sheet…",
      "launch.failed": ({ err = "" } = {}) => `更新失败。${err}`,
      "launch.setup_title": "需要设置",
      "launch.creds_missing": "未找到 credentials.json。请将其放入此文件夹，然后点击“重试”。",
      "launch.retry": "重试",
      "launch.chrome_title": "未找到 Google Chrome",
      "launch.chrome_missing": "Team Sheet 使用此电脑上安装的 Chrome 发送消息。请安装 Chrome 后重新启动。",
      "launch.download_chrome": "下载 Chrome",
      "launch.continue": "仍要继续",
      // --- Stripchat session ---
      "session.login": "登录 Stripchat",
      "session.opening": "正在打开浏览器…",
      "session.done": "完成 — 保存登录",
      "session.saving": "正在保存…",
      "session.import": "从 Chrome 导入会话",
      "session.importing": "正在导入…",
      // --- badges ---
      "badge.pending": "未发送",
      "badge.sent": "已发送",
      "badge.unseen": "未读",
      "badge.seen": "已读",
      "badge.replied": "已回复",
      "badge.dry_run": "将发送（试运行）",
      "badge.error": "错误",
      "badge.scan_processing": "扫描中…",
      "badge.scan_chat": "已有会话",
      "badge.send_processing": "发送中…",
      "badge.send_chat": "已有会话（已跳过）",
      "badge.dead": "无效账号（两次发送后无回复）",
      // --- errors ---
      "err.server_returned": ({ status = "" } = {}) => `服务器返回 ${status}`,
      "err.reach_server": ({ msg = "" } = {}) => `无法连接服务器：${msg}`,
      "err.load_vjs": ({ msg = "" } = {}) => `无法加载 VJ：${msg}`,
      "err.load_users": ({ msg = "" } = {}) => `无法加载该 lady 的用户：${msg}`,
      "err.load_sheets": ({ msg = "" } = {}) => `无法加载表格：${msg}`,
      "err.load_candidates": ({ msg = "" } = {}) => `无法加载候选人：${msg}`,
      "err.save_roster": ({ msg = "" } = {}) => `无法保存名单更改：${msg}`,
      "err.refresh_dashboard": ({ err = "" } = {}) => `刷新面板失败：${err}`,
      // --- document titles ---
      "title.scan": "团队表 — 扫描",
      "title.send": "团队表 — 发送",
    },
  };

  function getLocale() {
    const loc = localStorage.getItem(LOCALE_KEY);
    return loc === "zh" ? "zh" : "en";
  }

  function setLocale(loc) {
    localStorage.setItem(LOCALE_KEY, loc === "zh" ? "zh" : "en");
  }

  function t(key, params) {
    const loc = getLocale();
    let v = TRANSLATIONS[loc][key];
    if (v === undefined) v = TRANSLATIONS.en[key];
    if (v === undefined) return key;
    if (typeof v === "function") return v(params || {});
    if (params) return v.replace(/\{(\w+)\}/g, (_, k) => (params[k] !== undefined ? params[k] : `{${k}}`));
    return v;
  }

  // Translate all static [data-i18n] text and [data-i18n-ph] placeholders, and
  // set <html lang> + document.title. Safe to call once on load.
  function applyStatic(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    root.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-ph")));
    });
    document.documentElement.lang = getLocale() === "zh" ? "zh-CN" : "en";
    const titleKey = document.body && document.body.getAttribute("data-title-key");
    if (titleKey) document.title = t(titleKey);
  }

  // Language toggle: label shows the language you'll switch TO (中文 / ENG).
  function initToggle(btn) {
    if (!btn) return;
    const loc = getLocale();
    btn.textContent = loc === "zh" ? "ENG" : "中文";
    btn.setAttribute("aria-label", loc === "zh" ? "Switch to English" : "切换到中文");
    btn.addEventListener("click", () => {
      setLocale(loc === "zh" ? "en" : "zh");
      location.reload();
    });
  }

  window.i18n = { t, getLocale, setLocale, applyStatic, initToggle };
})();
