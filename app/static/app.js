/* 技术编辑选题推荐系统前端（原生 JS） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { data: null, pollTimer: null };

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(body.detail || `请求失败 (${resp.status})`);
  }
  return body;
}

function articleMap() {
  const m = new Map();
  state.data.articles.forEach((a) => m.set(a.id, a));
  return m;
}

function renderQuotas() {
  const quotas = state.data.state.quotas;
  const grid = $("#quota-grid");
  grid.innerHTML = "";
  state.data.topics.forEach((topic) => {
    const wrap = document.createElement("div");
    wrap.className = "quota-item";
    wrap.innerHTML = `<label>${topic}</label>`;
    const input = document.createElement("input");
    input.type = "number";
    input.min = 0;
    input.max = 6;
    input.value = quotas[topic];
    input.dataset.topic = topic;
    input.addEventListener("input", renderQuotaSum);
    wrap.appendChild(input);
    grid.appendChild(wrap);
  });
  renderQuotaSum();
}

function renderQuotaSum() {
  const sum = [...document.querySelectorAll("#quota-grid input")]
    .reduce((acc, el) => acc + (parseInt(el.value, 10) || 0), 0);
  const el = $("#quota-sum");
  el.innerHTML = `当前合计：<b>${sum}</b> / 6`;
  el.className = `quota-sum ${sum === 6 ? "valid" : "invalid"}`;
}

function renderExclusions() {
  const excluded = new Set(state.data.state.excluded_ids);
  const select = $("#exclude-select");
  const revealedIds = new Set(state.data.state.revealed.map((r) => r.article_id));
  select.innerHTML = "";
  state.data.articles.forEach((a) => {
    if (revealedIds.has(a.id)) return;
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = `#${a.id} ${a.title}（${a.topic}/${a.author}）${excluded.has(a.id) ? " — 已排除" : ""}`;
    if (excluded.has(a.id)) opt.disabled = true;
    select.appendChild(opt);
  });

  const list = $("#exclusion-list");
  list.innerHTML = "";
  if (excluded.size === 0) {
    list.innerHTML = `<span class="msg" style="color:#64748b">暂无排除文章</span>`;
    return;
  }
  const amap = articleMap();
  excluded.forEach((id) => {
    const a = amap.get(id);
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `#${id} ${a ? a.title : "未知"} <button title="取消排除" data-id="${id}">×</button>`;
    chip.querySelector("button").addEventListener("click", () => removeExclusion(id));
    list.appendChild(chip);
  });
}

function renderQueue() {
  const { state: st, plan, job } = state.data;
  const jobEl = $("#job-status");
  const revealedIds = new Set(st.revealed.map((r) => r.article_id));
  const amap = articleMap();

  if (job) {
    const label = {
      pending: "等待计算",
      running: "正在重算（慢重算期间仍可修改条件，迟到结果不会揭示旧推荐）",
      done: "重算完成",
      failed: `重算失败：${job.error || "未知错误"}（进度未推进）`,
      stale: "该结果已过期（条件已变更）",
    }[job.status] || job.status;
    jobEl.className = `job-status ${job.status}`;
    jobEl.textContent = `世代 ${job.generation} · ${label}`;
  }

  const queueEl = $("#queue");
  queueEl.innerHTML = "";
  const activePlan = plan && job.status === "done" && job.generation === st.generation ? plan : null;

  for (let pos = 1; pos <= 6; pos++) {
    const li = document.createElement("li");
    const revealedItem = st.revealed.find((r) => r.position === pos);
    if (revealedItem) {
      const a = amap.get(revealedItem.article_id);
      li.className = "revealed";
      li.innerHTML = `
        <span class="pos">${pos}</span>
        <div class="q-main">
          <div class="q-title">${a ? a.title : "文章 #" + revealedItem.article_id}</div>
          <div class="q-sub">
            <span class="tag">${revealedItem.topic}</span>
            <span class="tag author">${revealedItem.author}</span>
            已揭示 · 固定
          </div>
        </div>
        <div class="q-rel">相关度<br/><b>${revealedItem.relevance.toFixed(4)}</b><br/>权重 ${7 - pos}</div>`;
    } else if (activePlan && activePlan.feasible) {
      const item = activePlan.queue.find((q) => q.position === pos);
      li.className = "hidden-item";
      li.innerHTML = `
        <span class="pos">${pos}</span>
        <div class="q-main">
          <div class="q-title">未揭示位置 ${pos}（候选已锁定，等待揭示）</div>
          <div class="q-sub">主题：${item ? item.topic : "—"}</div>
        </div>
        <div class="q-rel">相关度<br/><b>${item ? item.relevance.toFixed(4) : "—"}</b><br/>权重 ${7 - pos}</div>`;
    } else {
      li.className = "hidden-item";
      const reason = activePlan && !activePlan.feasible ? activePlan.reason : "等待重算结果";
      li.innerHTML = `
        <span class="pos">${pos}</span>
        <div class="q-main"><div class="q-title">未揭示</div><div class="q-sub">${reason}</div></div>
        <div class="q-rel">权重 ${7 - pos}</div>`;
    }
    queueEl.appendChild(li);
  }

  $("#generation-line").textContent =
    `当前世代 ${st.generation} · 已揭示 ${st.revealed.length}/6 篇` +
    (activePlan && activePlan.feasible ? ` · 队列目标分 Σ(7−位置)×相关度 = ${activePlan.score.toFixed(4)}` : "");

  const revealBtn = $("#btn-reveal");
  const blocked = !activePlan || !activePlan.feasible || st.revealed.length >= 6;
  revealBtn.disabled = !!blocked;
  if (st.revealed.length >= 6) revealBtn.textContent = "队列已全部揭示";
  else revealBtn.textContent = "揭示下一组（两篇）";

  renderExclusions();
  renderArticleList();
}

function renderArticleList() {
  const filter = $("#article-filter").value.trim().toLowerCase();
  const excluded = new Set(state.data.state.excluded_ids);
  const revealedIds = new Set(state.data.state.revealed.map((r) => r.article_id));
  const list = $("#article-list");
  list.innerHTML = "";
  state.data.articles
    .filter((a) => {
      if (!filter) return true;
      return `${a.id} ${a.title} ${a.topic} ${a.author}`.toLowerCase().includes(filter);
    })
    .forEach((a) => {
      const card = document.createElement("div");
      card.className = `article-card ${excluded.has(a.id) ? "excluded" : ""}`;
      card.innerHTML = `
        <div class="t">#${a.id} ${a.title}</div>
        <div class="s">
          <span class="tag">${a.topic}</span>
          <span class="tag author">${a.author}</span>
          ${excluded.has(a.id) ? '<span class="tag excluded-tag">已排除</span>' : ""}
          ${revealedIds.has(a.id) ? '<span class="tag" style="background:#dcfce7;color:#166534">已揭示</span>' : ""}
        </div>`;
      card.addEventListener("click", () => openArticle(a.id));
      list.appendChild(card);
    });
}

async function openArticle(id) {
  const a = await api(`/api/articles/${id}`);
  $("#dlg-title").textContent = `#${a.id} ${a.title}`;
  $("#dlg-meta").innerHTML = `<span class="tag">${a.topic}</span><span class="tag author">${a.author}</span>`;
  $("#dlg-body").textContent = a.body;
  $("#article-dialog").showModal();
}

function readQuotas() {
  const quotas = {};
  document.querySelectorAll("#quota-grid input").forEach((el) => {
    quotas[el.dataset.topic] = parseInt(el.value, 10) || 0;
  });
  return quotas;
}

function showMsg(el, text, ok = false) {
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : ""}`;
  if (text) setTimeout(() => { el.textContent = ""; el.className = "msg"; }, 5000);
}

async function refresh() {
  state.data = await api("/api/status");
  $("#interest").value = state.data.state.interest;
  renderQuotas();
  renderQueue();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  const j = state.data.job;
  if (j && (j.status === "pending" || j.status === "running")) {
    state.pollTimer = setTimeout(refresh, 600);
  }
}

async function applyCondition() {
  const msg = $("#condition-msg");
  try {
    const payload = {
      interest: $("#interest").value,
      quotas: readQuotas(),
      compute_delay_ms: parseInt($("#delay").value, 10) || 0,
    };
    $("#btn-apply").disabled = true;
    state.data = await api("/api/condition", { method: "PUT", body: JSON.stringify(payload) });
    renderQuotas();
    renderQueue();
    showMsg(msg, "条件已更新，新世代重算已开始", true);
    schedulePoll();
  } catch (err) {
    showMsg(msg, err.message);
  } finally {
    $("#btn-apply").disabled = false;
  }
}

async function revealNext() {
  const msg = $("#reveal-msg");
  try {
    const data = await api("/api/reveal", { method: "POST" });
    state.data = data;
    renderQueue();
    const outcome = data.reveal_outcome;
    if (outcome.advanced) {
      showMsg(msg, `已揭示位置 ${outcome.new_positions.join("、")}`, true);
    } else {
      showMsg(msg, outcome.reason || "没有推进");
    }
  } catch (err) {
    showMsg(msg, err.message);
  }
}

async function addExclusion() {
  const msg = $("#condition-msg");
  const id = parseInt($("#exclude-select").value, 10);
  if (!id) return;
  try {
    state.data = await api("/api/exclusions", {
      method: "POST",
      body: JSON.stringify({ article_id: id, compute_delay_ms: parseInt($("#delay").value, 10) || 0 }),
    });
    renderQueue();
    showMsg(msg, `已排除文章 #${id}，触发新世代重算`, true);
    schedulePoll();
  } catch (err) {
    showMsg(msg, err.message);
  }
}

async function removeExclusion(id) {
  const msg = $("#condition-msg");
  try {
    const delay = parseInt($("#delay").value, 10) || 0;
    state.data = await api(`/api/exclusions/${id}?compute_delay_ms=${delay}`, {
      method: "DELETE",
    });
    renderQueue();
    showMsg(msg, `已取消排除 #${id}`, true);
    schedulePoll();
  } catch (err) {
    showMsg(msg, err.message);
  }
}

async function resetDemo() {
  if (!confirm("确定重置全部演示数据？已揭示前缀将被清空。")) return;
  state.data = await api("/api/reset", { method: "POST" });
  renderQuotas();
  renderQueue();
  schedulePoll();
}

$("#btn-apply").addEventListener("click", applyCondition);
$("#btn-reveal").addEventListener("click", revealNext);
$("#btn-exclude").addEventListener("click", addExclusion);
$("#btn-reset").addEventListener("click", resetDemo);
$("#article-filter").addEventListener("input", renderArticleList);
$("#dlg-close").addEventListener("click", () => $("#article-dialog").close());

refresh().catch((err) => {
  $("#job-status").textContent = `加载失败：${err.message}`;
});
