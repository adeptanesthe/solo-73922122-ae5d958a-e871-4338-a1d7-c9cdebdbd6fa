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

/* ================= 段落复序（排版故障恢复） =================
 * 输入失效语义：编辑修改段落或标记后，立即撤下旧建议（清空结果区），
 * 并递增请求序号；响应返回时核对序号与输入指纹，迟到的旧结果一律丢弃，
 * 不得覆盖新输入。
 */
const reorderState = { token: 0 };

// 打乱后的示例稿件（正确顺序为 段3→段5→段1→段4→段2）
const SAMPLE_PARAGRAPHS = [
  "值班编辑根据校对记录修改版面，每改一处都要重新检查上下文，避免改动引入新的差错，全部疑问清零后在流程单上签字，版面才允许进入制版环节。",
  "样张确认后印刷机正式开机，印好的报纸经过折页、计数和打包，由发行车队按线路送往各个投递点，清晨之前送到读者手中，整个夜班流程至此结束。",
  "夜班编辑在截稿前最后通读全部版面，重点核对标题、图片说明与引文出处，确认没有事实性差错之后，才把版面文件发送给校对组进行最终复核。",
  "制版车间把签字的版面文件转换成印刷用的印版，检查印版的网点与套准标记，确认无误后装上印刷机，先印出少量样张供机长做最后的颜色确认。",
  "校对组收到版面文件后逐字逐句比对原稿，用红笔标出错别字、标点误用和格式不统一之处，并把疑问处汇总成校对记录，退回给值班编辑逐条处理。",
];
const SAMPLE_MARKS = { first: 2, last: 1, adjacent: [[0, 3]], before: [[3, 1]] };

function parseParagraphs() {
  return $("#para-input").value
    .split(/\n\s*\n/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function fingerprintOf(payload) {
  // djb2：只用于识别“输入是否变化”，不用于安全用途
  const s = JSON.stringify([
    payload.paragraphs, payload.first, payload.last, payload.adjacent, payload.before,
  ]);
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(16);
}

function currentFingerprint() {
  try {
    return fingerprintOf(currentReorderPayload());
  } catch (err) {
    return null;
  }
}

function invalidateReorder() {
  // 任何输入变化：旧建议立即撤下，进行中的请求作废
  reorderState.token++;
  const box = $("#reorder-result");
  if (box.dataset.hasResult === "1") {
    box.innerHTML = "";
    const note = document.createElement("div");
    note.className = "reorder-stale";
    note.textContent = "输入已修改，旧建议已撤下，请重新生成。";
    box.appendChild(note);
  } else {
    box.innerHTML = "";
  }
  box.dataset.hasResult = "0";
}

function refreshReorderPanel() {
  const paras = parseParagraphs();
  const msg = $("#para-parse-msg");
  if (paras.length === 0) {
    msg.textContent = "";
    msg.className = "msg";
  } else if (paras.length < 4 || paras.length > 8) {
    msg.textContent = `已识别 ${paras.length} 段（要求 4～8 段）`;
    msg.className = "msg";
  } else {
    msg.textContent = `已识别 ${paras.length} 段`;
    msg.className = "msg ok";
  }

  const preview = $("#para-preview");
  preview.innerHTML = "";
  paras.forEach((t, i) => {
    const chip = document.createElement("span");
    const len = t.length;
    const okLen = len >= 20 && len <= 400;
    chip.className = `para-chip ${okLen ? "ok" : "bad"}`;
    chip.textContent = `段${i + 1} · ${len}字` +
      (okLen ? "" : len < 20 ? "（不足20字）" : "（超过400字）");
    preview.appendChild(chip);
  });

  const label = (i) => {
    const t = paras[i];
    return `段${i + 1}（${t.slice(0, 10)}${t.length > 10 ? "…" : ""}）`;
  };
  const fill = (sel, placeholder) => {
    const old = sel.value;
    sel.innerHTML = "";
    const ph = document.createElement("option");
    ph.value = "";
    ph.textContent = placeholder;
    sel.appendChild(ph);
    paras.forEach((_, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = label(i);
      sel.appendChild(opt);
    });
    if (old !== "" && parseInt(old, 10) < paras.length) sel.value = old;
  };
  fill($("#first-select"), "（选择首段）");
  fill($("#last-select"), "（选择末段）");
  document
    .querySelectorAll(".adj-from, .adj-to, .bef-from, .bef-to")
    .forEach((sel) => fill(sel, "（不标记）"));
}

function currentReorderPayload() {
  const paras = parseParagraphs();
  if (paras.length < 4 || paras.length > 8) {
    throw new Error(`段落数量须为 4～8 段，当前识别到 ${paras.length} 段`);
  }
  paras.forEach((t, i) => {
    if (t.length < 20) throw new Error(`段${i + 1}只有 ${t.length} 字，少于下限 20 字`);
    if (t.length > 400) throw new Error(`段${i + 1}有 ${t.length} 字，超过上限 400 字`);
  });
  const first = $("#first-select").value;
  const last = $("#last-select").value;
  if (first === "" || last === "") throw new Error("请选择首段与末段");
  if (first === last) throw new Error("首段与末段必须是不同的段落");
  const readPairs = (fromSel, toSel, name) => {
    const pairs = [];
    for (let r = 0; r < 2; r++) {
      const a = document.querySelector(`${fromSel}[data-row="${r}"]`).value;
      const b = document.querySelector(`${toSel}[data-row="${r}"]`).value;
      if (a === "" && b === "") continue;
      if (a === "" || b === "") throw new Error(`第 ${r + 1} 组“${name}”标记只选了一侧`);
      if (a === b) throw new Error(`“${name}”标记不能指向同一段落（段${parseInt(a, 10) + 1}）`);
      pairs.push([parseInt(a, 10), parseInt(b, 10)]);
    }
    return pairs;
  };
  return {
    paragraphs: paras,
    first: parseInt(first, 10),
    last: parseInt(last, 10),
    adjacent: readPairs(".adj-from", ".adj-to", "紧接"),
    before: readPairs(".bef-from", ".bef-to", "先于"),
  };
}

function renderReorderResult(resp) {
  const box = $("#reorder-result");
  box.innerHTML = "";
  box.dataset.hasResult = "1";
  if (!resp.ok) {
    const div = document.createElement("div");
    div.className = "reorder-fail";
    const title = document.createElement("b");
    title.textContent = "无法给出复序建议";
    const p = document.createElement("p");
    p.textContent = resp.reason || "未知原因";
    div.appendChild(title);
    div.appendChild(p);
    box.appendChild(div);
    return;
  }
  const head = document.createElement("div");
  head.className = "reorder-summary";
  head.textContent =
    `复序建议 · 总衔接得分 ${resp.total_score.toFixed(4)} · ` +
    `字符 2~3 元特征 ${resp.feature_count} 个（仅本批段落拟合）· 同分按原输入序号取最小`;
  box.appendChild(head);
  resp.paragraphs.forEach((p, i) => {
    if (i > 0) {
      const link = resp.links[i - 1];
      const conn = document.createElement("div");
      conn.className = `link-connector${link.marked ? " marked" : ""}`;
      conn.textContent =
        `↓ 衔接得分 ${link.score.toFixed(4)}（段${link.from + 1} → 段${link.to + 1}）` +
        (link.marked ? " · 标记紧接" : "");
      box.appendChild(conn);
    }
    const card = document.createElement("div");
    card.className = "para-card";
    const meta = document.createElement("div");
    meta.className = "para-meta";
    meta.textContent = `位置 ${i + 1} · 原段${p.index + 1}`;
    const body = document.createElement("div");
    body.className = "para-text";
    body.textContent = p.text;
    card.appendChild(meta);
    card.appendChild(body);
    box.appendChild(card);
  });
}

async function requestReorder() {
  const msg = $("#reorder-msg");
  msg.textContent = "";
  msg.className = "msg";
  let payload;
  try {
    payload = currentReorderPayload();
  } catch (err) {
    showMsg(msg, err.message);
    return;
  }
  payload.fingerprint = fingerprintOf(payload);
  const token = ++reorderState.token;
  const btn = $("#btn-reorder");
  btn.disabled = true;
  try {
    const resp = await api("/api/reorder", { method: "POST", body: JSON.stringify(payload) });
    // 迟到保护：已有更新的输入/请求，或响应指纹与当前输入不符，一律丢弃
    if (token !== reorderState.token) return;
    if (resp.fingerprint !== currentFingerprint()) return;
    renderReorderResult(resp);
  } catch (err) {
    if (token !== reorderState.token) return; // 迟到的错误同样不得覆盖
    showMsg(msg, err.message);
  } finally {
    if (token === reorderState.token) btn.disabled = false;
  }
}

function fillSample() {
  $("#para-input").value = SAMPLE_PARAGRAPHS.join("\n\n");
  refreshReorderPanel();
  $("#first-select").value = String(SAMPLE_MARKS.first);
  $("#last-select").value = String(SAMPLE_MARKS.last);
  const [adj] = [SAMPLE_MARKS.adjacent[0]];
  const [bef] = [SAMPLE_MARKS.before[0]];
  document.querySelector('.adj-from[data-row="0"]').value = String(adj[0]);
  document.querySelector('.adj-to[data-row="0"]').value = String(adj[1]);
  document.querySelector('.bef-from[data-row="0"]').value = String(bef[0]);
  document.querySelector('.bef-to[data-row="0"]').value = String(bef[1]);
  invalidateReorder();
  requestReorder();
}

$("#para-input").addEventListener("input", () => {
  refreshReorderPanel();
  invalidateReorder();
});
$("#first-select").addEventListener("change", invalidateReorder);
$("#last-select").addEventListener("change", invalidateReorder);
document
  .querySelectorAll(".adj-from, .adj-to, .bef-from, .bef-to")
  .forEach((el) => el.addEventListener("change", invalidateReorder));
$("#btn-reorder").addEventListener("click", requestReorder);
$("#btn-sample").addEventListener("click", fillSample);
refreshReorderPanel();
