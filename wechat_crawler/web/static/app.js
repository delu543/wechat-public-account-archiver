const state = {
  selectedAccountId: null,
  selectedArticleId: null,
  activeJobId: null,
  activeAutomationJobId: null,
  automationPaused: false,
  pollTimer: null,
  automationTimer: null,
  articleOffset: 0,
  articleTotal: 0,
};

const nodes = {
  form: document.querySelector("#jobForm"),
  input: document.querySelector("#urlInput"),
  sinceDate: document.querySelector("#sinceDateInput"),
  scheduleForm: document.querySelector("#scheduleForm"),
  scheduleWeekday: document.querySelector("#scheduleWeekday"),
  scheduleTime: document.querySelector("#scheduleTime"),
  cleanup: document.querySelector("#cleanupBtn"),
  openOutput: document.querySelector("#openOutputBtn"),
  databasePath: document.querySelector("#databasePath"),
  schedulerBadge: document.querySelector("#schedulerBadge"),
  subscriptionMetric: document.querySelector("#subscriptionMetric"),
  pendingMetric: document.querySelector("#pendingMetric"),
  nextRunMetric: document.querySelector("#nextRunMetric"),
  cleanupMetric: document.querySelector("#cleanupMetric"),
  subscriptions: document.querySelector("#subscriptionsBody"),
  exports: document.querySelector("#exportsList"),
  refresh: document.querySelector("#refreshBtn"),
  accounts: document.querySelector("#accountsList"),
  accountHeader: document.querySelector("#accountHeader"),
  articles: document.querySelector("#articlesList"),
  loadMore: document.querySelector("#loadMoreBtn"),
  articleTitle: document.querySelector("#articleTitle"),
  articleMeta: document.querySelector("#articleMeta"),
  articleBody: document.querySelector("#articleBody"),
  jobPhase: document.querySelector("#jobPhase"),
  jobMessage: document.querySelector("#jobMessage"),
  etaText: document.querySelector("#etaText"),
  progressFill: document.querySelector("#progressFill"),
  progressText: document.querySelector("#progressText"),
  jobCounts: document.querySelector("#jobCounts"),
  workflowMode: document.querySelector("#workflowMode"),
  unresolvedCounts: document.querySelector("#unresolvedCounts"),
  sessionPanel: document.querySelector("#sessionPanel"),
  sessionAction: document.querySelector("#sessionAction"),
  sessionSteps: document.querySelector("#sessionSteps"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error((data && data.error) || `HTTP ${response.status}`);
  return data;
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return "预计时间 --";
  if (seconds <= 0) return "已完成";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes ? `约 ${minutes} 分 ${rest} 秒` : `约 ${rest} 秒`;
}

function formatDate(value, includeTime = false) {
  if (!value) return "--";
  const raw = String(value);
  const match = raw.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (!match) return raw.slice(0, includeTime ? 16 : 10);
  return includeTime && match[4]
    ? `${match[2]}/${match[3]} ${match[4]}:${match[5]}`
    : `${match[1]}/${match[2]}/${match[3]}`;
}

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

function setProgress(job) {
  const progress = job ? Number(job.progress || 0) : 0;
  nodes.progressFill.style.width = `${progress}%`;
  nodes.progressText.textContent = `${progress}%`;
  nodes.jobPhase.textContent = job ? phaseLabel(job.phase || job.status) : "暂无运行任务";
  nodes.jobMessage.textContent = job ? job.message || job.account_name || "运行中" : "等待计划运行";
  nodes.etaText.textContent = job ? formatEta(job.eta_seconds) : "预计时间 --";
  nodes.jobCounts.textContent = job
    ? `发现 ${job.discovered_count || 0} · 已归档跳过 ${job.already_archived_count || 0} · 新增保存 ${job.crawled_count || 0} · 失败 ${job.failed_count || 0} · 受限 ${job.restricted_count || 0} · Word 输出 ${job.exported_count || 0}`
    : "发现 0 · 保存 0 · 失败 0";
  const mode = job ? job.mode || job.discovery_mode : null;
  const route = job ? " · 链接/biz 直连优先" : "";
  nodes.workflowMode.textContent = `模式：${mode || "--"}${route}${job && job.since_date ? ` · 从 ${job.since_date} 起` : ""}`;
  nodes.unresolvedCounts.textContent = `未解决：${formatUnresolved(job && job.unresolved_by_reason)}`;
  const needsSession = job && job.status === "needs_session";
  nodes.sessionPanel.hidden = !needsSession;
  if (needsSession) {
    nodes.sessionAction.textContent = job.recovery_action || job.message || "链接直连和自动微信正文恢复均未取得有效会话，任务已保留进度。";
    nodes.sessionSteps.innerHTML = "";
    (job.recovery_steps || ["保持 VPN 正常运行", "保持 Mac 解锁且微信已登录", "如出现验证码，只在微信中正常完成"]).forEach((step) => {
      const item = document.createElement("li");
      item.textContent = step;
      nodes.sessionSteps.appendChild(item);
    });
  }
}

function phaseLabel(value) {
  const labels = {
    scheduled: "等待运行",
    preflight: "链接/biz 直连",
    discover: "发现历史文章",
    crawl_fast: "快速抓取正文",
    retry_failed: "重试失败项",
    retry_unavailable: "重试不可用项",
    export_markdown: "导出 Markdown",
    export_word: "生成 Word",
    cleanup: "清理临时数据",
    completed: "已完成",
    failed: "失败",
    needs_session: "等待微信会话",
    paused: "已暂停",
    blocked: "已暂停",
  };
  return labels[value] || value || "--";
}

function stateLabel(value) {
  const labels = { active: "正常", error: "异常", needs_session: "需会话", blocked: "已暂停" };
  return labels[value] || value || "正常";
}

function formatUnresolved(summary) {
  if (!summary || !Object.keys(summary).length) return "--";
  return Object.entries(summary)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 3)
    .map(([reason, count]) => `${reason} ${count}`)
    .join(" · ");
}

async function loadAutomation() {
  const [overview, subscriptions, jobs, batches] = await Promise.all([
    api("/api/automation/overview"),
    api("/api/automation/subscriptions"),
    api("/api/automation/jobs"),
    api("/api/automation/batches"),
  ]);
  renderOverview(overview);
  renderSubscriptions(subscriptions);
  renderExports(batches);
  const running = jobs.find((job) => !["completed", "failed", "blocked", "needs_session", "paused", "superseded"].includes(job.status));
  const selected = jobs.find((job) => job.job_id === state.activeAutomationJobId) || running || jobs[0];
  if (selected && !state.activeJobId) setProgress(selected);
}

function renderOverview(overview) {
  state.automationPaused = Boolean(overview.automation_paused);
  nodes.databasePath.textContent = `Word：${overview.output_dir} · 数据：${overview.database_path}`;
  nodes.databasePath.title = nodes.databasePath.textContent;
  nodes.subscriptionMetric.textContent = `${overview.enabled_count}/${overview.subscription_count}`;
  nodes.pendingMetric.textContent = `${overview.pending_export_count} 篇`;
  nodes.nextRunMetric.textContent = formatDate(overview.next_run_at, true);
  nodes.cleanupMetric.textContent = formatBytes(overview.reclaimable_bytes);
  const cacheReady = overview.wechat_session_cache && overview.wechat_session_cache.status === "accessible";
  const windowReady = overview.wechat_window && overview.wechat_window.state === "ready";
  const screenLocked = overview.wechat_window && overview.wechat_window.state === "screen_locked";
  const sessionReady = cacheReady && windowReady;
  nodes.schedulerBadge.textContent = overview.automation_paused
    ? "全部爬取已暂停"
    : overview.scheduler_running
      ? sessionReady ? "自动运行中 · 微信已识别" : screenLocked ? "自动运行中 · Mac 已锁定" : "自动运行中 · 微信待就绪"
      : "自动调度未启动";
  nodes.schedulerBadge.title = [
    overview.wechat_window && overview.wechat_window.message,
    overview.wechat_session_cache && overview.wechat_session_cache.message,
  ].filter(Boolean).join("；");
  nodes.schedulerBadge.className = `status-badge ${!overview.automation_paused && overview.scheduler_running && sessionReady ? "success" : "warning"}`;
  const schedule = overview.schedule || {};
  nodes.scheduleWeekday.value = String(schedule.weekday ?? 6);
  nodes.scheduleTime.value = `${String(schedule.hour ?? 20).padStart(2, "0")}:${String(schedule.minute ?? 0).padStart(2, "0")}`;
}

function renderSubscriptions(subscriptions) {
  nodes.subscriptions.innerHTML = "";
  if (!subscriptions.length) {
    nodes.subscriptions.innerHTML = '<tr><td colspan="6" class="empty-row">暂无公众号订阅</td></tr>';
    return;
  }
  subscriptions.forEach((item) => {
    const row = document.createElement("tr");
    const mode = item.onboarding_mode === "initial_full" ? "首次全量" : "每周增量";
    const statusClass = item.state === "needs_session" ? "warning" : item.enabled ? "success" : "neutral";
    row.innerHTML = `
      <td data-label="公众号"><strong>${escapeHtml(item.account_name)}</strong><small>${item.article_count} 篇 · ${escapeHtml(mode)}</small></td>
      <td data-label="归档"><span class="status-pill ${statusClass}">${escapeHtml(item.enabled ? stateLabel(item.state) : "已暂停")}</span></td>
      <td data-label="最新文章">${escapeHtml(formatDate(item.latest_publish_time))}</td>
      <td data-label="待输出"><strong>${item.pending_export_count}</strong></td>
      <td data-label="下次运行">${escapeHtml(formatDate(item.next_run_at, true))}</td>
      <td data-label="操作" class="row-actions"></td>
    `;
    const actions = row.querySelector(".row-actions");
    const run = document.createElement("button");
    run.type = "button";
    run.className = "table-action";
    run.textContent = "立即同步";
    run.disabled = state.automationPaused;
    if (state.automationPaused) run.title = "请先在菜单栏插件中点击“继续”";
    run.addEventListener("click", () => runSubscription(item.id, run));
    if (item.pending_export_count > 0) {
      const exportPending = document.createElement("button");
      exportPending.type = "button";
      exportPending.className = "table-action";
      exportPending.textContent = "导出待处理";
      exportPending.addEventListener("click", () => exportPendingArticles(item.id, exportPending));
      actions.appendChild(exportPending);
    }
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "table-action secondary";
    toggle.textContent = item.enabled ? "暂停" : "启用";
    toggle.addEventListener("click", () => toggleSubscription(item.id, !item.enabled, toggle));
    actions.append(run, toggle);
    nodes.subscriptions.appendChild(row);
  });
}

function renderExports(batches) {
  nodes.exports.innerHTML = "";
  const visible = batches.slice(0, 6);
  if (!visible.length) {
    nodes.exports.textContent = "暂无自动输出";
    return;
  }
  visible.forEach((batch) => {
    const row = document.createElement("div");
    row.className = "export-item";
    row.innerHTML = `<div><strong>${escapeHtml(batch.account_name)}</strong><span>${batch.article_count} 篇 · ${escapeHtml(batch.cycle_key)}</span></div>`;
    if (batch.download_url) {
      const link = document.createElement("a");
      link.href = batch.download_url;
      link.textContent = "下载";
      row.appendChild(link);
    }
    nodes.exports.appendChild(row);
  });
}

async function runSubscription(subscriptionId, button) {
  button.disabled = true;
  try {
    const job = await api(`/api/automation/subscriptions/${subscriptionId}/run`, { method: "POST", body: "{}" });
    state.activeAutomationJobId = job.job_id;
    state.activeJobId = null;
    setProgress(job);
    startAutomationPolling();
  } catch (error) {
    nodes.jobMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function exportPendingArticles(subscriptionId, button) {
  button.disabled = true;
  try {
    await api(`/api/automation/subscriptions/${subscriptionId}/export-pending`, { method: "POST", body: "{}" });
    nodes.jobMessage.textContent = "已开始导出确认补齐的新增文章";
    window.setTimeout(loadAutomation, 1200);
  } catch (error) {
    nodes.jobMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function toggleSubscription(subscriptionId, enabled, button) {
  button.disabled = true;
  try {
    await api(`/api/automation/subscriptions/${subscriptionId}/toggle`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    await loadAutomation();
  } finally {
    button.disabled = false;
  }
}

async function saveSchedule(event) {
  event.preventDefault();
  const [hour, minute] = nodes.scheduleTime.value.split(":").map(Number);
  await api("/api/automation/settings", {
    method: "POST",
    body: JSON.stringify({ weekday: Number(nodes.scheduleWeekday.value), hour, minute }),
  });
  await loadAutomation();
}

async function cleanupArchive() {
  const preview = await api("/api/automation/cleanup", { method: "POST", body: JSON.stringify({ execute: false }) });
  const bytes = preview.raw_html_bytes + preview.temporary_bytes;
  if (!bytes) {
    nodes.jobMessage.textContent = "当前没有可清理的产品数据";
    return;
  }
  if (!window.confirm(`确认清理 ${formatBytes(bytes)} 可再生成的原始网页和临时文件？`)) return;
  const result = await api("/api/automation/cleanup", { method: "POST", body: JSON.stringify({ execute: true }) });
  nodes.jobMessage.textContent = `已清理 ${result.raw_html_rows} 条原始网页和 ${result.temporary_files} 个临时文件`;
  await loadAutomation();
}

async function openOutputDirectory() {
  const label = nodes.openOutput.textContent;
  nodes.openOutput.disabled = true;
  try {
    await api("/api/automation/open-output", { method: "POST", body: "{}" });
    nodes.openOutput.textContent = "已打开";
  } catch (error) {
    nodes.jobMessage.textContent = error.message;
    nodes.openOutput.textContent = "打开失败";
  } finally {
    window.setTimeout(() => {
      nodes.openOutput.textContent = label;
      nodes.openOutput.disabled = false;
    }, 1600);
  }
}

function startAutomationPolling() {
  if (state.automationTimer) clearInterval(state.automationTimer);
  state.automationTimer = setInterval(pollAutomationJob, 2500);
  pollAutomationJob();
}

async function pollAutomationJob() {
  if (!state.activeAutomationJobId) return;
  const job = await api(`/api/automation/jobs/${state.activeAutomationJobId}`);
  setProgress(job);
  if (["completed", "failed", "blocked", "needs_session", "superseded"].includes(job.status)) {
    clearInterval(state.automationTimer);
    state.automationTimer = null;
    await Promise.all([loadAutomation(), loadAccounts()]);
  }
}

async function loadAccounts() {
  const accounts = await api("/api/accounts");
  nodes.accounts.innerHTML = "";
  accounts.forEach((account) => {
    const button = document.createElement("button");
    button.className = "folder-item";
    if (state.selectedAccountId === account.id) button.classList.add("active");
    button.type = "button";
    button.innerHTML = `<span class="folder-name">${escapeHtml(account.account_name)}</span><span class="folder-meta">${account.article_count} 篇 · ${escapeHtml(account.account_biz || "")}</span>`;
    button.addEventListener("click", () => selectAccount(account));
    nodes.accounts.appendChild(button);
  });
}

async function selectAccount(account) {
  state.selectedAccountId = account.id;
  state.selectedArticleId = null;
  nodes.accountHeader.innerHTML = `<h2>${escapeHtml(account.account_name)}</h2><p>${account.article_count} 篇文章 · ${escapeHtml(account.folder_name)}</p>`;
  nodes.articleTitle.textContent = "未选择文章";
  nodes.articleMeta.textContent = "";
  nodes.articleBody.textContent = "选择文章后显示正文。";
  await Promise.all([loadArticles(account.id), loadAccounts()]);
}

async function loadArticles(accountId, append = false) {
  const offset = append ? state.articleOffset : 0;
  const payload = await api(`/api/accounts/${accountId}/articles?limit=100&offset=${offset}`);
  if (!append) {
    nodes.articles.innerHTML = "";
    state.articleOffset = 0;
    state.articleTotal = payload.total;
  }
  payload.items.forEach((article) => {
    const button = document.createElement("button");
    button.className = "article-item";
    button.type = "button";
    button.dataset.articleId = String(article.id);
    button.innerHTML = `<span class="article-title">${escapeHtml(article.title)}</span><span class="article-date">${escapeHtml(article.publish_time || "未知时间")}</span>`;
    button.addEventListener("click", () => selectArticle(article.id));
    nodes.articles.appendChild(button);
  });
  state.articleOffset = offset + payload.items.length;
  nodes.loadMore.hidden = state.articleOffset >= state.articleTotal;
  nodes.loadMore.textContent = `加载更多（${Math.min(state.articleOffset, state.articleTotal)}/${state.articleTotal}）`;
}

async function selectArticle(articleId) {
  state.selectedArticleId = articleId;
  const article = await api(`/api/articles/${articleId}`);
  nodes.articleTitle.textContent = article.title;
  nodes.articleMeta.textContent = `${article.publish_time || "未知时间"} · ${article.author || "无作者"} · ${article.discovered_from || "未知来源"}`;
  nodes.articleBody.textContent = article.body_markdown;
  [...nodes.articles.querySelectorAll(".article-item")].forEach((button) => {
    button.classList.toggle("active", button.dataset.articleId === String(article.id));
  });
}

async function submitJob(event) {
  event.preventDefault();
  const url = nodes.input.value.trim();
  if (!url) return;
  const button = nodes.form.querySelector("button");
  button.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ url, since: nodes.sinceDate.value || null }),
    });
    state.activeJobId = job.job_id;
    state.activeAutomationJobId = null;
    setProgress(job);
    startManualPolling();
  } catch (error) {
    nodes.jobPhase.textContent = "提交失败";
    nodes.jobMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function startManualPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(pollManualJob, 2000);
  pollManualJob();
}

async function pollManualJob() {
  if (!state.activeJobId) return;
  const job = await api(`/api/jobs/${state.activeJobId}`);
  setProgress(job);
  if (["completed", "failed", "needs_session"].includes(job.status)) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    await Promise.all([loadAccounts(), loadAutomation()]);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

nodes.form.addEventListener("submit", submitJob);
nodes.scheduleForm.addEventListener("submit", (event) => saveSchedule(event).catch((error) => { nodes.jobMessage.textContent = error.message; }));
nodes.cleanup.addEventListener("click", () => cleanupArchive().catch((error) => { nodes.jobMessage.textContent = error.message; }));
nodes.openOutput.addEventListener("click", openOutputDirectory);
nodes.refresh.addEventListener("click", () => Promise.all([loadAccounts(), loadAutomation()]));
nodes.loadMore.addEventListener("click", () => state.selectedAccountId && loadArticles(state.selectedAccountId, true));

Promise.all([loadAccounts(), loadAutomation()]).catch((error) => {
  nodes.jobMessage.textContent = error.message;
});
setInterval(() => loadAutomation().catch(() => {}), 15000);
