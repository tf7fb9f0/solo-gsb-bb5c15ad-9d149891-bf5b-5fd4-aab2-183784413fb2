"use strict";

// ---------- Session ----------
const SESSION_KEY = "calib_session_id";
function getSessionId() {
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = "sess-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    localStorage.setItem(SESSION_KEY, id);
  }
  return id;
}
const SESSION_ID = getSessionId();
document.getElementById("session-id").textContent = SESSION_ID;

// ---------- State ----------
let analysis = null;
let stability = null; // 排名稳定性实验结果（纯前端临时态，不持久化）
let compareData = null; // 双选手对比结果（只读，不落库，每次由当前评分实时计算）
let compareReqId = 0; // 丢弃过期的对比响应
let ruleDraft = null; // 规则编辑器草稿（数组），DOM 是输入中的真实来源
let rulePreviewData = null; // 最近一次规则预览结果（只读，不落库）
let rulePreviewStale = false; // 草稿在预览后又被修改
let backfillData = null; // 补评分派最近一次只读预览（不写入评分或裁决）
let backfillStale = false; // 预览参数在出结果后又被修改
let backfillCaps = {}; // 各评委本轮可接任务数（草稿，按评委名持久）
let backfillJudgeKey = ""; // 当前容量输入表对应的评委有序集合
let backfillSig = ""; // 最近一次预览所基于的原始评分签名，用于识别数据变化
let weightData = null; // 项目权重临界点分析最近一次只读结果
let weightReqId = 0; // 丢弃过期的权重分析响应
let weightSig = ""; // 最近一次权重结果所基于的（评分+裁决+范围）签名
let promoData = null; // 晋级门槛检查最近一次只读结果（不落库）
let promoReqId = 0; // 丢弃过期的门槛检查响应
let promoSig = ""; // 最近一次门槛结果所基于的（评分+裁决+范围）签名
let promoItemLines = {}; // 各项目最低线草稿（按项目名持久，留空表示不设门槛）
let promoItemKey = ""; // 当前输入表对应的项目有序集合
let promoTotalDraft = ""; // 总分线草稿（字符串，空串表示不检查总分）
let pendingData = null; // 待补评分名次边界最近一次只读结果（不落库）
let pendingReqId = 0; // 丢弃过期的名次边界响应
let pendingSig = ""; // 最近一次名次边界所基于的（范围+最终分+评分数）签名
let pendingStale = false; // 参数在上次计算后又被修改
let commonJudgeData = null; // 同评委可比对照最近一次只读结果（不落库）
let commonJudgeReqId = 0; // 丢弃过期的同评委对照响应
let commonJudgeSig = ""; // 最近一次同评委对照所基于的（范围+最终分+评委覆盖）签名

const $ = (sel) => document.querySelector(sel);
const dashboard = $("#dashboard");
const emptyState = $("#empty-state");

// ---------- Helpers ----------
function fmt(v, digits = 2) {
  if (v === null || v === undefined) return "-";
  return Number(v).toFixed(digits);
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function deltaHtml(d) {
  if (d > 0) return `<span class="delta-up">↑${d}</span>`;
  if (d < 0) return `<span class="delta-down">↓${-d}</span>`;
  return `<span class="delta-flat">—</span>`;
}
let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 4000 : 2200);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------- Rendering ----------
function render() {
  if (!analysis || analysis.counts.scores === 0) {
    dashboard.hidden = true;
    emptyState.hidden = false;
    compareReqId += 1;
    compareData = null;
    backfillData = null;
    backfillStale = false;
    backfillSig = "";
    setBackfillMsg("");
    renderBackfillPanel();
    weightReqId += 1;
    weightData = null;
    weightSig = "";
    renderWeightPanel();
    promoReqId += 1;
    promoData = null;
    promoSig = "";
    promoItemLines = {};
    promoItemKey = "";
    promoTotalDraft = "";
    renderPromotionPanel();
    pendingReqId += 1;
    pendingData = null;
    pendingSig = "";
    pendingStale = false;
    renderPendingPanel();
    commonJudgeReqId += 1;
    commonJudgeData = null;
    commonJudgeSig = "";
    setCjMsg("");
    renderCommonJudgePanel();
    return;
  }
  emptyState.hidden = true;
  dashboard.hidden = false;

  // 原始评分发生变化（上传/合并/载入示例）时，补评分派预览自动失效。
  const sig = backfillSignature(analysis);
  if (backfillData && sig !== backfillSig) {
    backfillData = null;
    backfillStale = false;
    setBackfillMsg("原始评分已变化，请重新生成分派预览。", "error");
  }
  backfillSig = sig;
  // 方案本身与校准范围无关，但横幅里的范围标签要随当前范围同步。
  if (backfillData) {
    backfillData.scope = analysis.scope;
    backfillData.scope_label = analysis.scope_label;
  }

  // 权重临界点结果依赖评分、逐项裁决与校准范围；任一变化都标记为过期。
  const ws = weightSignature(analysis);
  if (weightData && ws !== weightSig) {
    // 上传/合并/裁决/范围变化后旧结果作废：清除并以当前选择自动重算一次。
    weightData = null;
    setWeightMsg("基础数据已变化（评分/裁决/校准范围），正在重新计算…");
    runWeightAnalysis();
  } else if (!weightData && analysis.items.length) {
    // 首次有数据时，用当前选择自动计算一次（项目/上限改变由控件事件触发）。
    runWeightAnalysis();
  }

  // 晋级门槛结果依赖评分、逐项裁决与校准范围；任一变化都自动失效并重算。
  const ps = weightSignature(analysis); // 与权重分析同一组依赖：范围 + 最终分
  let promoAutoRun = false;
  if (promoData && ps !== promoSig) {
    promoData = null;
    // 旧门槛项目若仍存在则以当前填写值自动重算；门槛整体清空时不发请求。
    if (collectPromotionRequest({ silent: true })) {
      promoAutoRun = true;
      setPromoMsg("基础数据已变化（评分/裁决/校准范围），正在重新检查…");
    } else {
      setPromoMsg("基础数据已变化，请重新填写门槛。", "error");
    }
  }
  renderPromotionPanel();
  if (promoAutoRun) runPromotionCheck({ auto: true });

  // 待补评分名次边界依赖评分、逐项裁决与校准范围；任一变化都自动失效并重算。
  const pnd = pendingSignature(analysis);
  let pendingAutoRun = false;
  if (pendingData && pnd !== pendingSig) {
    pendingData = null;
    if (collectPendingRequest({ silent: true })) {
      pendingAutoRun = true;
      setPendingMsg("评分、裁决或校准范围已变化，正在重新计算名次边界…");
    } else {
      setPendingMsg("评分、裁决或校准范围已变化，请重新填写参数。", "error");
    }
  }
  renderPendingPanel();
  if (pendingAutoRun) runPendingCheck({ auto: true });

  // 同评委可比对照依赖评分（评委覆盖）、逐项裁决与校准范围（最终分）；任一变化自动重算。
  const cjs = commonJudgeSignature(analysis);
  if (commonJudgeData && cjs !== commonJudgeSig) {
    commonJudgeData = null;
    setCjMsg("基础数据已变化（评分/裁决/校准范围），正在重新计算同评委可比对照…");
    runCommonJudge();
  } else if (!commonJudgeData) {
    // 首次有数据或清空后：自动拉取一次。
    runCommonJudge();
  }

  const ref = analysis.reference;
  $("#reference-info").innerHTML =
    `校准范围：<b>${esc(analysis.scope_label)}</b><br>` +
    `对每个校准分组计算中位数与 MAD：<code>Z = (原始分 − 中位数) / (MAD × ${ref.mad_k})</code>，` +
    `再映射回全局尺度：<code>校准分 = ${fmt(ref.global_median, 3)} + Z × ${fmt(ref.global_mad, 3)}</code>` +
    `（全局中位数 ${fmt(ref.global_median, 3)}，全局 MAD ${fmt(ref.global_mad, 3)}）。` +
    `分组 MAD 为 0（无离散度）时取 Z = 0。`;

  const c = analysis.counts;
  $("#stats-row").innerHTML = [
    ["选手", c.contestants], ["评委", c.judges], ["项目", c.items],
    ["评分条数", c.scores], ["保留原分裁决", c.decisions],
  ].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");

  renderGroups();
  renderReversals();
  renderRanks();
  renderDisagreement();
  renderJudgeOffsets();
  initRuleDraft();
  renderDetails();
  renderStability();
  refreshCompare();
  renderBackfillPanel();
  renderWeightPanel();
  renderCommonJudgePanel();
}

// ---------- Ranking stability experiment ----------
const stabilityTypeSel = $("#stability-type");
const stabilityNameSel = $("#stability-name");
const stabilityBtnRun = $("#btn-stability-run");
const stabilityBtnClear = $("#btn-stability-clear");

function setStabilityMsg(text, cls = "") {
  const el = $("#stability-msg");
  el.textContent = text;
  el.className = "msg " + cls;
}

// Discard the what-if result and return to the baseline table.
function clearStability(silent = false) {
  stability = null;
  setStabilityMsg("");
  renderStability();
  if (!silent) toast("已恢复基线排名");
}

function syncStabilityControls() {
  const hasData = !!(analysis && analysis.counts.scores > 0);
  const type = stabilityTypeSel.value;
  const names = hasData ? (type === "judge" ? analysis.judges : analysis.items) : [];
  const prev = stabilityNameSel.value;
  stabilityNameSel.innerHTML =
    `<option value="" disabled${names.length ? "" : " selected"}>请选择${type === "judge" ? "评委" : "项目"}…</option>` +
    names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  if (names.includes(prev)) stabilityNameSel.value = prev;
  stabilityBtnRun.disabled = !hasData;
  stabilityNameSel.disabled = !hasData;
  stabilityTypeSel.disabled = !hasData;
  stabilityBtnClear.disabled = stability === null;
}

function signedNum(d, digits = 2) {
  if (d === null || d === undefined) return "-";
  return (d > 0 ? "+" : d < 0 ? "−" : "") + fmt(Math.abs(d), digits);
}

function totalDeltaHtml(d) {
  if (d === null || d === undefined) return `<span class="delta-flat">-</span>`;
  if (d > 0) return `<span class="delta-up">${signedNum(d)}</span>`;
  if (d < 0) return `<span class="delta-down">${signedNum(d)}</span>`;
  return `<span class="delta-flat">0.00</span>`;
}

function renderStability() {
  const panel = $("#stability-result");
  syncStabilityControls();
  if (!stability) {
    panel.innerHTML = "";
    return;
  }
  const ex = stability.exclusion;
  const s = stability.summary;

  const head = `
    <tr>
      <th class="num">基线<br>最终名次</th><th>选手</th><th class="num">受影响<br>评分数</th>
      <th class="num">原始<br>基线总分</th><th class="num">实验<br>原始总分</th><th class="num">原始<br>总分变化</th>
      <th class="num">基线<br>原始名次</th><th class="num">实验<br>原始名次</th><th class="num">原始<br>名次变化</th>
      <th class="num">校准<br>基线总分</th><th class="num">实验<br>校准总分</th><th class="num">校准<br>总分变化</th>
      <th class="num">基线<br>校准名次</th><th class="num">实验<br>校准名次</th><th class="num">校准<br>名次变化</th>
      <th class="num">最终<br>基线总分</th><th class="num">实验<br>最终总分</th><th class="num">最终<br>总分变化</th>
      <th class="num">基线<br>最终名次</th><th class="num">实验<br>最终名次</th><th class="num">最终<br>名次变化</th>
    </tr>`;

  const rows = stability.contestants.map((r) => {
    if (!r.present) {
      return `<tr class="gone-row"><td class="num"><b>${r.baseline.eff_rank}</b></td>
        <td><span class="gone-name">${esc(r.contestant)}</span></td>
        <td class="num">${r.affected_scores}</td>
        <td class="num">${fmt(r.baseline.raw_total)}</td>
        <td colspan="17" class="num gone-note">无剩余评分，无法计算名次（校准分 / 最终分同样不可计算）</td></tr>`;
    }
    const b = r.baseline, sc = r.scenario;
    const cls = r.eff_rank_delta && r.eff_rank_delta !== 0 ? "rank-shift-row" : "";
    return `
    <tr class="${cls}">
      <td class="num"><b>${b.eff_rank}</b></td>
      <td>${esc(r.contestant)}</td>
      <td class="num"><b>${r.affected_scores}</b></td>
      <td class="num">${fmt(b.raw_total)}</td><td class="num">${fmt(sc.raw_total)}</td><td class="num">${totalDeltaHtml(r.raw_total_delta)}</td>
      <td class="num">${b.raw_rank}</td><td class="num">${sc.raw_rank}</td><td class="num">${deltaHtml(r.raw_rank_delta)}</td>
      <td class="num">${fmt(b.cal_total)}</td><td class="num">${fmt(sc.cal_total)}</td><td class="num">${totalDeltaHtml(r.cal_total_delta)}</td>
      <td class="num">${b.cal_rank}</td><td class="num">${sc.cal_rank}</td><td class="num">${deltaHtml(r.cal_rank_delta)}</td>
      <td class="num"><b>${fmt(b.eff_total)}</b></td><td class="num"><b>${fmt(sc.eff_total)}</b></td><td class="num">${totalDeltaHtml(r.eff_total_delta)}</td>
      <td class="num">${b.eff_rank}</td><td class="num"><b>${sc.eff_rank}</b></td><td class="num">${deltaHtml(r.eff_rank_delta)}</td>
    </tr>`;
  }).join("");

  panel.innerHTML = `
    <div class="stability-banner">
      实验：临时排除${ex.type_label} <b>${esc(ex.name)}</b>（校准范围：${esc(stability.scope_label)}）｜
      共移除 <b>${stability.removed_scores}</b> 条评分（${stability.baseline_counts.scores} → ${stability.scenario_counts.scores}）。
      最终名次变化：<b class="${s.eff_rank_changed ? "delta-down" : "delta-flat"}">${s.eff_rank_changed}</b> 位
      <span class="delta-up">↑${s.eff_moved_up}</span> / <span class="delta-down">↓${s.eff_moved_down}</span>；
      原始名次变化 ${s.raw_rank_changed} 位，校准名次变化 ${s.cal_rank_changed} 位；
      最终总分最大绝对变化 ${fmt(s.max_eff_total_delta_abs)}。
      <span class="stability-note">本结果为临时模拟，不写入会话裁决或数据库。</span>
    </div>
    <div class="table-wrap stability-table-wrap"><table>${head}${rows}</table></div>`;
}

// ---------- Head-to-head comparison ----------
const compareASel = $("#compare-a");
const compareBSel = $("#compare-b");

function setCompareMsg(text, cls = "") {
  const el = $("#compare-msg");
  el.textContent = text;
  el.className = "msg " + cls;
}

function fillCompareSelect(sel, prevVal) {
  const blocks = analysis && analysis.counts.scores > 0 ? analysis.contestants : [];
  sel.innerHTML =
    `<option value="" disabled${prevVal ? "" : " selected"}>请选择选手…</option>` +
    blocks
      .map((b) => `<option value="${esc(b.contestant)}">#${b.eff_rank} ${esc(b.contestant)}</option>`)
      .join("");
  if (blocks.some((b) => b.contestant === prevVal)) sel.value = prevVal;
}

function syncCompareControls() {
  const hasData = !!(analysis && analysis.counts.scores > 0);
  fillCompareSelect(compareASel, compareASel.value);
  fillCompareSelect(compareBSel, compareBSel.value);
  compareASel.disabled = !hasData;
  compareBSel.disabled = !hasData;
}

// Recompute the comparison whenever the underlying analysis changes
// (scope switch, decisions, re-upload) — nothing is stored client-side.
function refreshCompare() {
  syncCompareControls();
  const a = compareASel.value;
  const b = compareBSel.value;
  if (!analysis || analysis.counts.scores === 0 || !a || !b || a === b) {
    compareReqId += 1; // invalidate any in-flight response
    compareData = null;
    renderCompare();
    return;
  }
  if (compareData && (compareData.a !== a || compareData.b !== b)) {
    compareData = null; // selection changed; drop the stale pair immediately
    renderCompare();
  }
  const myId = ++compareReqId;
  setCompareMsg("对比计算中…");
  api(`/api/sessions/${SESSION_ID}/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`)
    .then((data) => {
      if (myId !== compareReqId) return; // a newer request superseded this one
      compareData = data;
      setCompareMsg("");
      renderCompare();
    })
    .catch((err) => {
      if (myId !== compareReqId) return;
      compareData = null;
      setCompareMsg(err.message, "error");
      renderCompare();
    });
}

function renderCompare() {
  const panel = $("#compare-result");
  const a = compareASel.value;
  const b = compareBSel.value;
  if (!compareData) {
    if (a && b && a === b) {
      panel.innerHTML = `<p class="hint">选手 A 与选手 B 相同，请选择两名不同的选手。</p>`;
    } else if (a || b) {
      panel.innerHTML = `<p class="hint">再选择一名选手即可开始对比。</p>`;
    } else {
      panel.innerHTML = "";
    }
    return;
  }

  const t = compareData.totals;
  const ranks = compareData.ranks;
  const s = compareData.summary;

  const rankDiff = ranks.b.eff - ranks.a.eff; // >0: A 名次更靠前
  const rankCell =
    rankDiff > 0
      ? `<span class="delta-up">${esc(compareData.a)} 领先 ${rankDiff} 名</span>`
      : rankDiff < 0
        ? `<span class="delta-down">${esc(compareData.b)} 领先 ${-rankDiff} 名</span>`
        : `<span class="delta-flat">并列</span>`;

  const totalsRows = [
    ["原始总分", t.a.raw, t.b.raw, t.delta.raw, false],
    ["校准总分", t.a.cal, t.b.cal, t.delta.cal, false],
    ["最终总分", t.a.eff, t.b.eff, t.delta.eff, true],
  ]
    .map(
      ([label, va, vb, d, bold]) => `
    <tr>
      <td>${label}</td>
      <td class="num">${bold ? `<b>${fmt(va)}</b>` : fmt(va)}</td>
      <td class="num">${bold ? `<b>${fmt(vb)}</b>` : fmt(vb)}</td>
      <td class="num">${totalDeltaHtml(d)}</td>
    </tr>`,
    )
    .join("");

  const totalsTable = `
    <div class="table-wrap compare-totals-wrap"><table>
      <tr><th></th><th class="num">${esc(compareData.a)}</th><th class="num">${esc(compareData.b)}</th><th class="num">差值 (A−B)</th></tr>
      ${totalsRows}
      <tr><td>最终名次</td><td class="num"><b>${ranks.a.eff}</b></td><td class="num"><b>${ranks.b.eff}</b></td><td class="num">${rankCell}</td></tr>
    </table></div>`;

  const eff = s.eff;
  const statText = (k) => `${s[k].a_leads} / ${s[k].b_leads} / ${s[k].ties}`;
  const missing = [];
  if (s.a_only > 0) missing.push(`${s.a_only} 个项目仅 ${esc(compareData.a)} 有评分`);
  if (s.b_only > 0) missing.push(`${s.b_only} 个项目仅 ${esc(compareData.b)} 有评分`);
  const banner = `
    <div class="compare-banner">
      逐项目胜负（按最终分，共 <b>${s.compared}</b> 个可比项目）：
      <b>${esc(compareData.a)}</b> 领先 <b class="delta-up">${eff.a_leads}</b> 项 ｜
      <b>${esc(compareData.b)}</b> 领先 <b class="delta-down">${eff.b_leads}</b> 项 ｜
      持平 <b>${eff.ties}</b> 项。
      <span class="compare-note">（A胜 / B胜 / 持平）按原始分 ${statText("raw")}，按校准分 ${statText("cal")}。${missing.length ? missing.join("，") + "，已标为无数据、不计入差值与胜负统计。" : ""}</span>
    </div>`;

  const valCell = (snap, k, bold) =>
    snap
      ? `<td class="num">${bold ? `<b>${fmt(snap[k])}</b>` : fmt(snap[k])}</td>`
      : `<td class="num nodata">无数据</td>`;
  const deltaCell = (d, k) =>
    d ? `<td class="num">${totalDeltaHtml(d[k])}</td>` : `<td class="num nodata">—</td>`;
  const leadCell = (lead) => {
    if (lead === "a") return `<td><span class="delta-up">${esc(compareData.a)}</span></td>`;
    if (lead === "b") return `<td><span class="delta-down">${esc(compareData.b)}</span></td>`;
    if (lead === "tie") return `<td><span class="delta-flat">持平</span></td>`;
    return `<td><span class="delta-flat">—</span></td>`;
  };

  const head = `
    <tr>
      <th>项目</th>
      <th class="num">${esc(compareData.a)}<br>原始</th><th class="num">${esc(compareData.b)}<br>原始</th><th class="num">原始差<br>(A−B)</th>
      <th class="num">${esc(compareData.a)}<br>校准</th><th class="num">${esc(compareData.b)}<br>校准</th><th class="num">校准差<br>(A−B)</th>
      <th class="num">${esc(compareData.a)}<br>最终</th><th class="num">${esc(compareData.b)}<br>最终</th><th class="num">最终差<br>(A−B)</th>
      <th>领先<br>(最终)</th>
    </tr>`;
  const rows = compareData.items
    .map(
      (r) => `
    <tr>
      <td>${esc(r.item)}</td>
      ${valCell(r.a, "raw")}${valCell(r.b, "raw")}${deltaCell(r.delta, "raw")}
      ${valCell(r.a, "cal")}${valCell(r.b, "cal")}${deltaCell(r.delta, "cal")}
      ${valCell(r.a, "eff", true)}${valCell(r.b, "eff", true)}${deltaCell(r.delta, "eff")}
      ${leadCell(r.lead)}
    </tr>`,
    )
    .join("");

  panel.innerHTML =
    banner +
    totalsTable +
    `<h3 class="sub">逐项目对比（项目内各评委打分的均值）</h3>` +
    `<div class="table-wrap compare-table-wrap"><table>${head}${rows}</table></div>`;
}

compareASel.addEventListener("change", refreshCompare);
compareBSel.addEventListener("change", refreshCompare);

// ---------- Judge ranking disagreement (read-only, raw scores only) ----------
function pctText(rate) {
  return rate === null || rate === undefined ? "—" : (rate * 100).toFixed(1) + "%";
}

function renderDisagreement() {
  const panel = $("#disagreement-panel");
  if (!panel) return;
  const dg = analysis.judge_disagreement;
  if (!dg || dg.summary.groups === 0) {
    panel.innerHTML = `<p class="no-reversal">当前数据没有任何「项目 × 评委对」分组（至少需要两位评委给同一项目打分）。</p>`;
    return;
  }
  const s = dg.summary;

  const banner = `
    <div class="dg-banner">
      共 <b>${s.groups}</b> 个「项目 × 评委对」分组（样本充足 <b>${s.sufficient_groups}</b>、
      <span class="dg-ins">样本不足 <b>${s.insufficient_groups}</b></span>），
      共评选手对（含同分对）<b>${s.common_contestant_pairs}</b> 对：
      <span class="dg-con">同向 <b>${s.counts.concordant}</b></span>、
      <span class="dg-dis">逆向 <b>${s.counts.discordant}</b></span>、
      <span class="dg-one">单方同分 <b>${s.counts.one_tie}</b></span>、
      <span class="dg-both">双方同分 <b>${s.counts.both_tie}</b></span>。
      可比较对数（双方均非同分）<b>${s.comparable_pairs}</b>，
      整体逆向占比 <b class="${s.discordance_rate ? "dg-rate" : ""}">${pctText(s.discordance_rate)}</b>
      <span class="dg-note">（样本不足分组不给分歧率，也不计入整体占比）。</span>
    </div>`;

  // 样本充足的分组优先，并按逆向占比降序（无分歧率沉底）。
  const ordered = dg.groups.slice().sort((g1, g2) => {
    if (g1.sufficient !== g2.sufficient) return g1.sufficient ? -1 : 1;
    const r1 = g1.discordance_rate, r2 = g2.discordance_rate;
    if (r1 === null && r2 === null) return 0;
    if (r1 === null) return 1;
    if (r2 === null) return -1;
    return r2 - r1;
  });

  const head = `
    <tr>
      <th>项目</th><th>评委对</th><th class="num">共评<br>选手数</th>
      <th class="num">选手对<br>总数</th><th class="num">同向</th><th class="num">逆向</th>
      <th class="num">单方<br>同分</th><th class="num">双方<br>同分</th>
      <th class="num">可比较<br>对数</th><th class="num">逆向占比</th><th>明细</th>
    </tr>`;

  // 某位评委一列：给 X / 给 Y 原分，以及该评委更看好谁（▲ 标注在更高分一侧）。
  const judgeCells = (scoreOfX, scoreOfY, prefIsX) => `
    <td class="num">${fmt(scoreOfX)}${prefIsX ? ' <small class="dg-mark">▲</small>' : ""}</td>
    <td class="num">${fmt(scoreOfY)}${prefIsX ? "" : ' <small class="dg-mark">▲</small>'}</td>
    <td><span class="${prefIsX ? "dg-pref-a" : "dg-pref-b"}">更看好 ${esc(prefIsX ? "X" : "Y")}</span></td>`;

  const rows = ordered.map((g, idx) => {
    const pair = `${esc(g.judge_a)}<span class="dg-vs"> × </span>${esc(g.judge_b)}`;
    const detailHead = `
      <tr>
        <th colspan="3">选手 X</th><th colspan="3">选手 Y</th>
        <th colspan="3">${esc(g.judge_a)} 的原分</th>
        <th colspan="3">${esc(g.judge_b)} 的原分</th>
      </tr>
      <tr>
        <th colspan="3"></th><th colspan="3"></th>
        <th class="num">给 X</th><th class="num">给 Y</th><th>更看好</th>
        <th class="num">给 X</th><th class="num">给 Y</th><th>更看好</th>
      </tr>`;
    const detailRows = g.discordant_pairs.map((p) => `
      <tr>
        <td colspan="3">${esc(p.contestant_a)}</td>
        <td colspan="3">${esc(p.contestant_b)}</td>
        ${judgeCells(p.judge_a_score_of_a, p.judge_a_score_of_b, p.judge_a_prefers === p.contestant_a)}
        ${judgeCells(p.judge_b_score_of_a, p.judge_b_score_of_b, p.judge_b_prefers === p.contestant_a)}
      </tr>`).join("");
    const detailRow = `
      <tr class="dg-detail-row" data-detail="${idx}" hidden>
        <td colspan="11">
          <div class="table-wrap"><table class="dg-detail-table">
            ${detailHead}${detailRows}
          </table></div>
        </td>
      </tr>`;

    if (!g.sufficient) {
      return `
      <tr class="dg-row dg-insufficient" data-row="${idx}">
        <td>${esc(g.item)}</td><td>${pair}</td>
        <td class="num">${g.common_count}</td>
        <td class="num">—</td><td class="num">—</td><td class="num">—</td>
        <td class="num">—</td><td class="num">—</td><td class="num">—</td>
        <td><span class="dg-ins-badge">样本不足</span></td>
        <td><span class="dg-no-detail">共评选手少于 ${dg.min_common} 名</span></td>
      </tr>`;
    }

    const rateCell = g.discordance_rate === null
      ? `<span class="delta-flat">无可比较对</span>`
      : `<b class="${g.discordance_rate > 0 ? "dg-rate" : "delta-flat"}">${pctText(g.discordance_rate)}</b>`;
    const expandable = g.discordant_pairs.length > 0;
    return `
      <tr class="dg-row${expandable ? " dg-expandable" : ""}" data-row="${idx}">
        <td>${esc(g.item)}</td><td>${pair}</td>
        <td class="num">${g.common_count}</td>
        <td class="num">${g.pairs_total}</td>
        <td class="num">${g.counts.concordant}</td>
        <td class="num"><b class="${g.counts.discordant ? "dg-rate" : ""}">${g.counts.discordant}</b></td>
        <td class="num">${g.counts.one_tie}</td>
        <td class="num">${g.counts.both_tie}</td>
        <td class="num">${g.comparable_pairs}</td>
        <td class="num">${rateCell}</td>
        <td>${expandable
          ? `<span class="dg-toggle" data-toggle="${idx}">查看 ${g.counts.discordant} 个逆向选手对 ▾</span>`
          : '<span class="dg-no-detail">无逆向选手对</span>'}</td>
      </tr>
      ${detailRow}`;
  }).join("");

  panel.innerHTML = banner + `
    <div class="table-wrap dg-table-wrap"><table>${head}${rows}</table></div>
    <p class="hint dg-foot">
      所有数字仅由<b>原始分</b>计算，切换校准范围、逐项裁决或规则批处理都不会改变本面板；
      「选手对总数」= 共评选手两两组合数，其中同向 + 逆向 = 可比较对数（单方/双方同分不参与分歧率）。
    </p>`;

  panel.querySelectorAll(".dg-toggle").forEach((toggle) => {
    toggle.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const idx = toggle.dataset.toggle;
      const detail = panel.querySelector(`.dg-detail-row[data-detail="${idx}"]`);
      const n = toggle.textContent.match(/\d+/)[0];
      if (detail.hasAttribute("hidden")) {
        detail.removeAttribute("hidden");
        toggle.textContent = `收起 ${n} 个逆向选手对 ▴`;
      } else {
        detail.setAttribute("hidden", "");
        toggle.textContent = `查看 ${n} 个逆向选手对 ▾`;
      }
    });
  });
  // 点击整行同样可以展开/收起（仅存在逆向选手对的行）。
  panel.querySelectorAll(".dg-expandable").forEach((row) => {
    row.addEventListener("click", () => {
      panel.querySelector(`.dg-toggle[data-toggle="${row.dataset.row}"]`)?.click();
    });
  });
  // 展开区域内的点击不应冒泡触发行收起。
  panel.querySelectorAll(".dg-detail-row").forEach((detail) => {
    detail.addEventListener("click", (ev) => ev.stopPropagation());
  });
}

// ---------- Judge leniency/severity offset diagnosis (read-only, raw scores only) ----------
const OFFSET_EPS = 1e-9;

function offsetTendency(offset) {
  if (offset > OFFSET_EPS) return `<span class="jo-lenient">偏宽</span>`;
  if (offset < -OFFSET_EPS) return `<span class="jo-severe">偏严</span>`;
  return `<span class="jo-neutral">居中</span>`;
}

function renderJudgeOffsets() {
  const panel = $("#offsets-panel");
  if (!panel) return;
  const jo = analysis.judge_offsets;
  if (!jo || jo.summary.judges === 0) {
    panel.innerHTML = `<p class="no-reversal">当前数据没有评委，无法进行严宽差异诊断。</p>`;
    return;
  }
  const s = jo.summary;

  const banner = `
    <div class="jo-banner">
      共 <b>${s.judges}</b> 位评委：可估计 <b>${s.estimable_judges}</b> 位
      （分属 <b>${s.components}</b> 个连通分量，各分量内偏移量均值为零），
      <span class="jo-no">不可估计 <b>${s.non_estimable_judges}</b></span> 位（无共评边）。
      评委对 <b>${s.pairs}</b> 对，共评记录合计 <b>${s.common_records}</b> 条；
      加权残差平方和 <b>${fmt(s.weighted_residual_ss, 4)}</b>，
      最大绝对残差 <b class="${s.max_abs_residual > OFFSET_EPS ? "jo-res" : ""}">${s.max_abs_residual === null ? "—" : fmt(s.max_abs_residual, 4)}</b>
      <span class="jo-note">（残差大表示该评委对的观测分差无法被分量内偏移量一致解释）。</span>
    </div>`;

  const judgeRows = jo.judges.map((j) => {
    if (!j.estimable) {
      return `
      <tr class="jo-row jo-not-estimable">
        <td>${esc(j.judge)}</td>
        <td class="num">—</td>
        <td class="num"><span class="jo-no-badge">不可估计</span></td>
        <td><span class="jo-neutral">—</span></td>
        <td class="num">${j.coverage}</td>
        <td class="num">${j.partners}</td>
      </tr>`;
    }
    return `
      <tr class="jo-row">
        <td>${esc(j.judge)}</td>
        <td class="num">分量 ${j.component}</td>
        <td class="num"><b>${signedNum(j.offset, 3)}</b></td>
        <td>${offsetTendency(j.offset)}</td>
        <td class="num">${j.coverage}</td>
        <td class="num">${j.partners}</td>
      </tr>`;
  }).join("");
  const judgeTable = `
    <h3 class="sub">评委偏移量与共评覆盖（偏移量为分量内相对值，均值为零，不跨分量比较）</h3>
    <div class="table-wrap jo-table-wrap"><table>
      <tr><th>评委</th><th class="num">连通分量</th><th class="num">偏移量<br>(+偏宽 / −偏严)</th>
        <th>相对倾向</th><th class="num">共评<br>覆盖量</th><th class="num">共评<br>评委数</th></tr>
      ${judgeRows}
    </table></div>`;

  const pairRows = jo.pairs.map((p) => `
      <tr class="jo-row">
        <td class="num">分量 ${p.component}</td>
        <td>${esc(p.judge_a)}<span class="jo-vs"> × </span>${esc(p.judge_b)}</td>
        <td class="num">${p.common_count}</td>
        <td class="num">${signedNum(p.observed_diff, 3)}</td>
        <td class="num">${signedNum(p.fitted_diff, 3)}</td>
        <td class="num"><b class="${Math.abs(p.residual) > OFFSET_EPS ? "jo-res" : "delta-flat"}">${signedNum(p.residual, 3)}</b></td>
      </tr>`).join("");
  const pairTable = `
    <h3 class="sub">评委对分差拟合（观测 = 共评记录原始分差中位数，权重 = 共评数）</h3>
    <div class="table-wrap jo-table-wrap"><table>
      <tr><th class="num">连通分量</th><th>评委对</th><th class="num">共评数</th>
        <th class="num">观测分差<br>(甲−乙)</th><th class="num">拟合分差<br>(甲−乙)</th><th class="num">残差<br>(观测−拟合)</th></tr>
      ${pairRows || '<tr><td colspan="6" class="no-reversal">没有任何评委对共同评过同一「选手 × 项目」。</td></tr>'}
    </table></div>`;

  panel.innerHTML = banner + judgeTable + pairTable + `
    <p class="hint jo-foot">
      所有数字仅由<b>原始分</b>计算：观测分差 = 两位评委共同评过的「选手 × 项目」记录上
      原始分差（评委甲 − 评委乙）的中位数，共评记录数为权重；逐连通分量做加权最小二乘拟合，
      各分量内偏移量均值为零（正 = 相对偏宽，负 = 相对偏严），<b>不跨分量比较</b>；
      无共评边的评委标为「不可估计」。切换校准范围、逐项裁决或规则批处理都不会改变本面板。
    </p>`;
}

function renderGroups() {
  const byItem = analysis.scope === "judge_item";  const head = byItem
    ? `<tr><th>评委</th><th>项目</th><th class="num">条数</th><th class="num">中位数</th><th class="num">MAD</th><th class="num">MAD×1.4826</th></tr>`
    : `<tr><th>评委</th><th class="num">条数</th><th class="num">中位数</th><th class="num">MAD</th><th class="num">MAD×1.4826</th></tr>`;
  const rows = analysis.groups.map((g) => {
    const flat = g.mad === 0 ? ' <span class="zline">(无离散度)</span>' : "";
    return byItem
      ? `<tr><td>${esc(g.judge)}</td><td>${esc(g.item)}</td><td class="num">${g.n}</td><td class="num">${fmt(g.median)}</td><td class="num">${fmt(g.raw_mad, 3)}${flat}</td><td class="num">${fmt(g.mad, 3)}</td></tr>`
      : `<tr><td>${esc(g.judge)}</td><td class="num">${g.n}</td><td class="num">${fmt(g.median)}</td><td class="num">${fmt(g.raw_mad, 3)}${flat}</td><td class="num">${fmt(g.mad, 3)}</td></tr>`;
  }).join("");
  $("#group-table-wrap").innerHTML = `<table>${head}${rows}</table>`;
}

function renderReversals() {
  const rvs = analysis.reversals;
  const badge = $("#reversal-count");
  badge.textContent = rvs.length;
  badge.className = "badge" + (rvs.length === 0 ? " zero" : "");
  const panel = $("#reversal-panel");
  if (rvs.length === 0) {
    panel.innerHTML = `<p class="no-reversal">✓ 标准化前后未检测到选手对名次反转。</p>`;
    return;
  }
  panel.innerHTML = rvs.map((r) => `
    <div class="reversal-card">
      <b>${esc(r.winner_before)}</b>（原始第 ${r.raw_rank_before} 名）
      <span class="arrow">→ 反超 →</span>
      <b>${esc(r.winner_after)}</b>（原始第 ${r.raw_rank_loser} 名）
      <br>
      <small>校准后：<b>${esc(r.winner_after)}</b> 第 ${r.cal_rank_winner} 名，${esc(r.winner_before)} 第 ${r.cal_rank_loser} 名；
      原始分差 ${fmt(r.raw_gap)}，校准后反向分差 ${fmt(r.cal_gap)}</small>
    </div>`).join("");
}

function renderRanks() {
  const head = `<tr>
    <th class="num">最终名次</th><th>选手</th>
    <th class="num">原始总分</th><th class="num">原始名次</th>
    <th class="num">校准总分</th><th class="num">校准名次</th>
    <th class="num">校准后变化</th>
    <th class="num">最终总分</th><th class="num">保留原分条数</th>
  </tr>`;
  // Show final-rank ordering; blocks already arrive sorted by eff_rank.
  const rows = analysis.contestants.map((b) => `
    <tr>
      <td class="num"><b>${b.eff_rank}</b></td>
      <td>${esc(b.contestant)}</td>
      <td class="num">${fmt(b.raw_total)}</td>
      <td class="num">${b.raw_rank}</td>
      <td class="num">${fmt(b.cal_total)}</td>
      <td class="num">${b.cal_rank}</td>
      <td class="num">${deltaHtml(b.rank_delta)}</td>
      <td class="num"><b>${fmt(b.eff_total)}</b></td>
      <td class="num">${b.raw_overrides}</td>
    </tr>`).join("");
  $("#rank-table-wrap").innerHTML = `<table>${head}${rows}</table>`;
}

function renderDetails() {
  const panel = $("#detail-panel");
  panel.innerHTML = analysis.contestants.map((b, idx) => {
    const items = b.items.map((ib) => {
      const scores = ib.scores.map((s) => {
        const flat = s.group_mad === 0;
        const useCal = !s.use_raw;
        return `
        <div class="score-row" data-id="${s.id}">
          <div class="who">${esc(s.judge)}<small>${esc(ib.item)}</small></div>
          <div class="score-val">
            <span class="raw">原分 ${fmt(s.raw)}</span><br>
            <span class="cal">校准 ${fmt(s.calibrated)}</span>
            <small>（Z=${fmt(s.z, 2)}，中位数 ${fmt(s.group_median)}）</small>
            ${flat ? '<br><span class="zline">该分组 MAD=0，校准分恒等于全局中位数</span>' : ""}
          </div>
          <div class="score-val muted">
            最终分<br><span class="eff">${fmt(s.effective)}</span>
          </div>
          <div class="choice">
            <label><input type="radio" name="choice-${s.id}" value="cal" ${useCal ? "checked" : ""}> 采用校准分</label>
            <label><input type="radio" name="choice-${s.id}" value="raw" ${s.use_raw ? "checked" : ""}> 保留原分</label>
            <input type="text" class="reason-input" data-id="${s.id}" maxlength="2000"
                   placeholder="理由（可选，将写入审计导出）" value="${esc(s.reason)}">
          </div>
        </div>`;
      }).join("");
      return `<div style="margin:6px 0 12px;"><b>${esc(ib.item)}</b>
        <small style="color:var(--muted)">（原始均分 ${fmt(ib.raw_avg)} → 校准均分 ${fmt(ib.cal_avg)} → 最终均分 ${fmt(ib.eff_avg)}）</small>
        ${scores}</div>`;
    }).join("");
    return `
      <div class="contestant-block ${idx > 1 ? "collapsed" : ""}" data-contestant="${esc(b.contestant)}">
        <div class="contestant-head">
          <h4>${esc(b.contestant)}</h4>
          <span class="meta">最终名次 ${b.eff_rank} ｜ 最终总分 ${fmt(b.eff_total)} ｜ 原始名次 ${b.raw_rank} → 校准名次 ${b.cal_rank} ｜ ${b.item_count} 个项目（点击折叠）</span>
        </div>
        <div class="contestant-body">${items}</div>
      </div>`;
  }).join("");

  panel.querySelectorAll(".contestant-head").forEach((h) => {
    h.addEventListener("click", () => h.parentElement.classList.toggle("collapsed"));
  });
  panel.querySelectorAll('input[type="radio"]').forEach((radio) => {
    radio.addEventListener("change", onChoiceChange);
  });
  panel.querySelectorAll(".reason-input").forEach((input) => {
    input.addEventListener("input", onReasonInput);
  });
}

// ---------- Batch adjudication rules ----------
const rulesEditor = $("#rules-editor");
const rulesPreviewEl = $("#rules-preview");

function setRulesMsg(text, cls = "") {
  const el = $("#rules-msg");
  el.textContent = text;
  el.className = "msg " + cls;
}

function emptyRule() {
  return {
    name: "", judge: "", item: "",
    raw_min: "", raw_max: "", cal_min: "", cal_max: "",
    use_raw: false, reason_template: "", priority: 100,
  };
}

// Load drafts once per data set: prefer unsaved drafts, then server rules.
function initRuleDraft() {
  if (ruleDraft) return;
  const saved = (analysis.batch_rules || []).map((r) => ({
    name: r.name || "",
    judge: r.judge || "",
    item: r.item || "",
    raw_min: r.raw_min === null || r.raw_min === undefined ? "" : r.raw_min,
    raw_max: r.raw_max === null || r.raw_max === undefined ? "" : r.raw_max,
    cal_min: r.cal_min === null || r.cal_min === undefined ? "" : r.cal_min,
    cal_max: r.cal_max === null || r.cal_max === undefined ? "" : r.cal_max,
    use_raw: !!r.use_raw,
    reason_template: r.reason_template || "",
    priority: r.priority,
  }));
  ruleDraft = saved.length ? saved : [emptyRule()];
  renderRulesEditor();
}

function renderRulesEditor() {
  const judgeOpts = [`<option value="">（不限评委）</option>`]
    .concat(analysis.judges.map((j) => `<option value="${esc(j)}">${esc(j)}</option>`)).join("");
  const itemOpts = [`<option value="">（不限项目）</option>`]
    .concat(analysis.items.map((i) => `<option value="${esc(i)}">${esc(i)}</option>`)).join("");

  rulesEditor.innerHTML = ruleDraft.map((r, idx) => `
    <div class="rule-row" data-idx="${idx}">
      <div class="rule-head">
        <span class="rule-index">#${idx + 1}</span>
        <input type="text" class="rule-name" placeholder="规则名称（可选，便于审计）" value="${esc(r.name)}">
        <label class="rule-choice">
          裁决：
          <select class="rule-use-raw">
            <option value="cal"${r.use_raw ? "" : " selected"}>采用校准分</option>
            <option value="raw"${r.use_raw ? " selected" : ""}>保留原分</option>
          </select>
        </label>
        <label class="rule-prio">优先级（小者优先）
          <input type="number" class="rule-priority" min="0" max="1000000" step="1" value="${esc(String(r.priority))}">
        </label>
        <button type="button" class="btn btn-ghost-card rule-del" title="删除该规则">删除</button>
      </div>
      <div class="rule-conds">
        <label>评委<select class="rule-judge">${judgeOpts}</select></label>
        <label>项目<select class="rule-item">${itemOpts}</select></label>
        <label>原始分 ≥<input type="number" class="rule-raw-min" step="any" value="${esc(r.raw_min)}"></label>
        <label>原始分 ≤<input type="number" class="rule-raw-max" step="any" value="${esc(r.raw_max)}"></label>
        <label>校准分 ≥<input type="number" class="rule-cal-min" step="any" value="${esc(r.cal_min)}"></label>
        <label>校准分 ≤<input type="number" class="rule-cal-max" step="any" value="${esc(r.cal_max)}"></label>
      </div>
      <input type="text" class="rule-reason" maxlength="2000"
             placeholder="理由模板，支持 {judge} {item} {raw} {calibrated}（留空则不写理由）"
             value="${esc(r.reason_template)}">
    </div>`).join("");

  rulesEditor.querySelectorAll(".rule-row").forEach((row) => {
    const idx = Number(row.dataset.idx);
    const sel = (cls) => row.querySelector("." + cls);
    const bind = (cls, key) => {
      sel(cls).addEventListener("input", (ev) => {
        ruleDraft[idx][key] = ev.target.value;
        markRulesChanged();
      });
    };
    bind("rule-name", "name");
    bind("rule-reason", "reason_template");
    bind("rule-priority", "priority");
    bind("rule-judge", "judge");
    bind("rule-item", "item");
    bind("rule-raw-min", "raw_min");
    bind("rule-raw-max", "raw_max");
    bind("rule-cal-min", "cal_min");
    bind("rule-cal-max", "cal_max");
    sel("rule-use-raw").addEventListener("change", (ev) => {
      ruleDraft[idx].use_raw = ev.target.value === "raw";
      markRulesChanged();
    });
    sel("rule-del").addEventListener("click", () => {
      ruleDraft.splice(idx, 1);
      if (ruleDraft.length === 0) ruleDraft.push(emptyRule());
      markRulesChanged();
      renderRulesEditor();
    });
  });
}

// After a preview, any edit to the draft invalidates it until re-previewed.
function markRulesChanged() {
  if (rulePreviewData) {
    rulePreviewStale = true;
    renderRulesPreview();
  }
}

function collectRules() {
  return ruleDraft.map((r) => ({
    name: r.name,
    judge: r.judge || null,
    item: r.item || null,
    raw_min: r.raw_min === "" ? null : Number(r.raw_min),
    raw_max: r.raw_max === "" ? null : Number(r.raw_max),
    cal_min: r.cal_min === "" ? null : Number(r.cal_min),
    cal_max: r.cal_max === "" ? null : Number(r.cal_max),
    use_raw: r.use_raw,
    reason_template: r.reason_template,
    priority: Number.isFinite(Number(r.priority)) ? Number(r.priority) : 100,
  }));
}

function condSummary(r) {
  const parts = [];
  parts.push(r.judge ? `评委=${esc(r.judge)}` : "评委不限");
  parts.push(r.item ? `项目=${esc(r.item)}` : "项目不限");
  const iv = (lo, hi, label) => {
    if (lo === null || lo === undefined) {
      return hi === null || hi === undefined ? `${label}不限` : `${label}≤${fmt(hi)}`;
    }
    return hi === null || hi === undefined ? `${label}≥${fmt(lo)}` : `${fmt(lo)}≤${label}≤${fmt(hi)}`;
  };
  parts.push(iv(r.raw_min, r.raw_max, "原分"));
  parts.push(iv(r.cal_min, r.cal_max, "校准"));
  return parts.join("，");
}

function renderRulesPreview() {
  if (!rulePreviewData) {
    rulesPreviewEl.innerHTML = "";
    return;
  }
  const p = rulePreviewData;
  const banner = `
    <div class="rules-banner">
      预览（校准范围：<b>${esc(p.scope_label)}</b>，只读不落库）｜
      共 <b>${p.total_scores}</b> 条评分：命中 <b class="delta-up">${p.matched}</b>，
      未命中 <b>${p.unmatched}</b>；其中被多条规则同时命中（冲突）<b class="delta-down">${p.conflicted}</b> 条，
      已按优先级数字小者优先、同值按顺序前者优先裁决出唯一结果。
      相对当前裁决，预计实际变更 <b>${p.would_change}</b> 条。
      本次提交 ${p.rules_submitted} 条规则，库中已保存 ${p.rules_saved} 条。
      ${rulePreviewStale ? '<div class="stale-warn">⚠ 规则在预览后又被修改，以下结果已过期；应用前请重新预览。</div>' : ""}
    </div>`;

  const ruleRows = p.rules.map((r, i) => {
    const src = ruleDraft[i] || {};
    const conflictCls = r.conflicts > 0 ? " has-conflict" : "";
    return `
    <tr class="${conflictCls}">
      <td class="num">#${i + 1}</td>
      <td>${esc(src.name || "")}</td>
      <td class="num"><b>${r.priority}</b></td>
      <td>${condSummary({
        judge: src.judge, item: src.item,
        raw_min: src.raw_min === "" || src.raw_min === undefined ? null : Number(src.raw_min),
        raw_max: src.raw_max === "" || src.raw_max === undefined ? null : Number(src.raw_max),
        cal_min: src.cal_min === "" || src.cal_min === undefined ? null : Number(src.cal_min),
        cal_max: src.cal_max === "" || src.cal_max === undefined ? null : Number(src.cal_max),
      })}</td>
      <td>${r.use_raw ? "保留原分" : "采用校准分"}</td>
      <td class="num"><b>${r.hits}</b></td>
      <td class="num">${r.misses}</td>
      <td class="num">${r.conflicts ? `<span class="delta-down">${r.conflicts}</span>` : "0"}</td>
      <td class="num"><b>${r.wins}</b></td>
    </tr>`;
  }).join("");

  const matchedA = p.assignments.filter((a) => a.matched);
  const unmatchedA = p.assignments.filter((a) => !a.matched);
  const detailRows = matchedA.map((a) => {
    const winners = a.winner_index + 1;
    const conflictNote = a.matched_rule_indexes.length > 1
      ? `<span class="conflict-tag">冲突 ${a.matched_rule_indexes.length} 条，#${winners} 胜出</span>`
      : "";
    return `
    <tr class="matched-row">
      <td class="num">#${winners}</td>
      <td>${esc(a.contestant)}</td><td>${esc(a.judge)}</td><td>${esc(a.item)}</td>
      <td class="num">${fmt(a.raw)}</td><td class="num">${fmt(a.calibrated)}</td>
      <td><b>${a.use_raw ? "保留原分" : "采用校准分"}</b> ${conflictNote}</td>
      <td>${esc(a.reason)}</td>
    </tr>`;
  }).join("");

  rulesPreviewEl.innerHTML = banner + `
    <div class="table-wrap">
      <table>
        <tr><th class="num">#</th><th>规则名称</th><th class="num">优先级</th><th>条件</th><th>裁决</th>
        <th class="num">命中数</th><th class="num">未命中数</th><th class="num">冲突数</th><th class="num">胜出数</th></tr>
        ${ruleRows}
      </table>
    </div>
    <details class="assign-details" ${matchedA.length ? "open" : ""}>
      <summary>逐条裁决预览（命中 ${matchedA.length} 条 / 未命中 ${unmatchedA.length} 条，未命中评分保持现有裁决不变）</summary>
      <div class="table-wrap">
        <table>
          <tr><th class="num">胜出规则</th><th>选手</th><th>评委</th><th>项目</th>
          <th class="num">原始分</th><th class="num">校准分</th><th>裁决结果</th><th>理由</th></tr>
          ${detailRows || '<tr><td colspan="8" class="no-reversal">没有任何评分被命中。</td></tr>'}
        </table>
      </div>
    </details>`;
}

$("#btn-rule-add").addEventListener("click", () => {
  ruleDraft.push(emptyRule());
  markRulesChanged();
  renderRulesEditor();
});

$("#btn-rule-preview").addEventListener("click", async () => {
  setRulesMsg("预览计算中…");
  try {
    rulePreviewData = await api(`/api/sessions/${SESSION_ID}/rules/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules: collectRules() }),
    });
    rulePreviewStale = false;
    setRulesMsg("预览完成：未保存任何数据。", "ok");
    renderRulesPreview();
  } catch (err) {
    rulePreviewData = null;
    renderRulesPreview();
    setRulesMsg(err.message, "error");
    toast("预览失败：" + err.message, true);
  }
});

$("#btn-rule-save").addEventListener("click", async () => {
  try {
    const r = await api(`/api/sessions/${SESSION_ID}/rules`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules: collectRules() }),
    });
    setRulesMsg(`已保存 ${r.saved} 条规则（未改动任何裁决）。`, "ok");
    toast(`已保存 ${r.saved} 条规则`);
  } catch (err) {
    setRulesMsg(err.message, "error");
    toast("保存规则失败：" + err.message, true);
  }
});

$("#btn-rule-apply").addEventListener("click", async () => {
  let p = rulePreviewData;
  if (!p) {
    toast("请先预览规则再应用。", true);
    return;
  }
  if (rulePreviewStale) {
    toast("规则自上次预览后已修改，请重新预览后再应用。", true);
    return;
  }
  if (!confirm(`确认应用规则批处理？\n命中 ${p.matched} 条评分（其中冲突 ${p.conflicted} 条已按优先级裁决），未命中 ${p.unmatched} 条保持不变，预计实际变更 ${p.would_change} 条。\n规则与全部裁决将在同一个事务中写入。`)) return;
  setRulesMsg("正在一个事务中批量写入…");
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/rules/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules: collectRules() }),
    });
    stability = null; // 裁决已变，稳定性实验结果作废
    rulePreviewData = null;
    rulePreviewStale = false;
    clearMergePreview(); // 批量裁决改写后，预览中「旧裁决」信息已过期
    render();
    renderRulesPreview();
    setRulesMsg(
      `已在同一事务中保存 ${analysis.rules_saved} 条规则，写入裁决 ${analysis.written} 条，实际变更 ${analysis.changed} 条；排名已刷新。`,
      "ok",
    );
    toast(`批处理完成：变更 ${analysis.changed} 条，排名已刷新`);
  } catch (err) {
    setRulesMsg(err.message, "error");
    toast("批处理失败（未写入任何数据）：" + err.message, true);
  }
});

$("#btn-rule-clear").addEventListener("click", async () => {
  if (!confirm("清空面板中的全部规则？（仅清空编辑区与已保存规则，不改动已有裁决；如未保存请先应用）")) return;
  try {
    await api(`/api/sessions/${SESSION_ID}/rules`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules: [] }),
    });
    ruleDraft = [emptyRule()];
    rulePreviewData = null;
    renderRulesEditor();
    renderRulesPreview();
    setRulesMsg("已清空全部规则（裁决未改动）。", "ok");
  } catch (err) {
    setRulesMsg(err.message, "error");
    toast("清空规则失败：" + err.message, true);
  }
});

// ---------- Supplementary scoring assignment (read-only preview) ----------
const backfillTargetInput = $("#backfill-target");
const backfillCapsEl = $("#backfill-caps");
const backfillResultEl = $("#backfill-result");
const backfillRunBtn = $("#btn-backfill-run");
const backfillClearBtn = $("#btn-backfill-clear");

function setBackfillMsg(text, cls = "") {
  const el = $("#backfill-msg");
  if (el) {
    el.textContent = text;
    el.className = "msg " + cls;
  }
}

// 原始评分签名：只看评委/选手/项目的存在与打分事实（分数值变化也重算预览，
// 因为它可能影响会话内容；补评分派实际只依赖打分事实）。
function backfillSignature(a) {
  const triples = [];
  for (const b of a.contestants) {
    for (const ib of b.items) {
      for (const s of ib.scores) triples.push(`${b.contestant}|${s.judge}|${ib.item}`);
    }
  }
  triples.sort();
  return triples.join("#");
}

// 容量输入表只在评委集合变化时整体重建；重建会保留旧评委已填的容量。
function renderBackfillCaps() {
  if (!analysis || analysis.counts.scores === 0) {
    backfillCapsEl.innerHTML = "";
    backfillRunBtn.disabled = true;
    backfillTargetInput.disabled = true;
    return;
  }
  backfillRunBtn.disabled = false;
  backfillTargetInput.disabled = false;
  const key = analysis.judges.join("|");
  if (key === backfillJudgeKey) return;
  backfillJudgeKey = key;
  const rows = analysis.judges.map((j, i) => {
    const val = backfillCaps[j] !== undefined ? backfillCaps[j] : 0;
    return `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(j)}</td>
        <td class="num"><input type="number" class="bf-cap-input" data-judge="${esc(j)}"
          min="0" max="1000000" step="1" value="${Number(val)}"></td>
      </tr>`;
  }).join("");
  backfillCapsEl.innerHTML = `
    <p class="hint" style="margin-bottom:6px;">各评委本轮可接任务数（留空或 0 表示本轮不接单；仅可接「已评过该项目、未评该单元」的任务）：</p>
    <div class="table-wrap"><table>
      <tr><th class="num">#</th><th>评委</th><th class="num">本轮可接任务数</th></tr>
      ${rows}
    </table></div>`;
  backfillCapsEl.querySelectorAll(".bf-cap-input").forEach((input) => {
    input.addEventListener("input", (ev) => {
      backfillCaps[ev.target.dataset.judge] = ev.target.value === "" ? 0 : Number(ev.target.value);
      markBackfillStale();
    });
  });
}

// 预览生成后再改参数 → 标记结果过期（不自动重算）。
function markBackfillStale() {
  if (backfillData && !backfillStale) {
    backfillStale = true;
    renderBackfillResult();
  }
}
backfillTargetInput.addEventListener("input", markBackfillStale);

function collectBackfillCaps() {
  const caps = {};
  for (const j of analysis.judges) {
    caps[j] = backfillCaps[j] !== undefined ? Number(backfillCaps[j]) || 0 : 0;
  }
  return caps;
}

function renderBackfillPanel() {
  renderBackfillCaps();
  backfillClearBtn.disabled = !backfillData;
  renderBackfillResult();
}

function renderBackfillResult() {
  if (!backfillData) {
    backfillResultEl.innerHTML = "";
    return;
  }
  const p = backfillData;
  const s = p.summary;
  const banner = `
    <div class="bf-banner">
      补评分派预览（校准范围：<b>${esc(p.scope_label)}</b>，只读，<b>不写入评分或裁决</b>）｜
      每单元目标 <b>${p.target}</b> 位评委：需求单元 <b>${s.units}</b> 个，其中有缺口的单元
      <b>${s.gap_units}</b> 个（全部缺口 <b>${s.total_gap}</b> 个任务）；
      评委容量合计 <b>${s.capacity_total}</b>，有资格的「评委×单元」候选 <b>${s.eligible_edges}</b> 个。<br>
      最优分派 <b class="bf-assigned">${s.assigned_total}</b> 个任务
      （${s.fully_met_units} 个单元全部补足），未满足 <b class="bf-unmet">${s.unmet_total}</b> 个；
      候选存在多个最优并列方案时，结果按固定的「评委 → 项目 → 选手」次序稳定选取。
      ${backfillStale ? '<span class="bf-stale">⚠ 参数在上次预览后已修改，以下结果已过期，请重新生成。</span>'
        : '<span class="bf-note">预览仅展示任务分派，不会创建评分或裁决。</span>'}
    </div>`;

  // 评委容量使用情况。
  const judgeRows = p.judges.map((j) => {
    const full = j.remaining <= 0 && j.capacity > 0;
    return `
      <tr${full ? ' class="bf-met-row"' : ""}>
        <td>${esc(j.judge)}</td>
        <td class="num">${j.capacity}</td>
        <td class="num"><b class="bf-assigned">${j.assigned}</b></td>
        <td class="num">${j.remaining}</td>
      </tr>`;
  }).join("");
  const judgeTable = `
    <h3 class="sub">各评委任务分配（容量 / 已派 / 剩余）</h3>
    <div class="table-wrap"><table>
      <tr><th>评委</th><th class="num">可接任务数</th><th class="num">本轮已派</th><th class="num">剩余</th></tr>
      ${judgeRows}
    </table></div>`;

  // 逐条分派：评委 → 选手 → 项目（assignments 已由服务端固定排序）。
  const assignRows = p.assignments.map((a) => `
      <tr>
        <td>${esc(a.judge)}</td>
        <td>${esc(a.contestant)}</td>
        <td>${esc(a.item)}</td>
      </tr>`).join("");
  const assignTable = `
    <h3 class="sub">分派任务明细（共 ${p.assignments.length} 条，按评委 → 选手 → 项目）</h3>
    <div class="table-wrap bf-table-wrap"><table>
      <tr><th>评委</th><th>选手</th><th>项目</th></tr>
      ${assignRows || '<tr><td colspan="3" class="no-reversal">在当前容量与缺口约束下没有可分派的任务。</td></tr>'}
    </table></div>`;

  // 单元汇总：仅展示有缺口的单元（按项目 → 选手固定次序），逐条给出未满足数量。
  const gapUnits = p.units.filter((u) => u.gap > 0);
  const unitRows = gapUnits.map((u) => `
      <tr class="${u.unmet > 0 ? "bf-unmet-row" : "bf-met-row"}">
        <td>${esc(u.item)}</td>
        <td>${esc(u.contestant)}</td>
        <td class="num">${u.existing}</td>
        <td class="num">${u.target}</td>
        <td class="num">${u.gap}</td>
        <td class="num"><b class="bf-assigned">${u.assigned}</b></td>
        <td class="num"><b class="${u.unmet > 0 ? "bf-unmet" : "delta-flat"}">${u.unmet}</b></td>
        <td class="num">${u.eligible_count}</td>
        <td class="bf-elig">${u.eligible_judges.length ? u.eligible_judges.map(esc).join("、") : "（无合格评委）"}</td>
      </tr>`).join("");
  const unitTable = `
    <h3 class="sub">各单元缺口汇总（仅列有缺口的 ${gapUnits.length} 个；按项目 → 选手）</h3>
    <div class="table-wrap bf-table-wrap"><table>
      <tr><th>项目</th><th>选手</th><th class="num">已有<br>评分数</th><th class="num">目标</th>
      <th class="num">缺口</th><th class="num">本轮<br>分派</th><th class="num">未满足</th>
      <th class="num">合格<br>评委数</th><th>可接该单元的评委</th></tr>
      ${unitRows || '<tr><td colspan="9" class="no-reversal">所有需求单元都已达到目标人数。</td></tr>'}
    </table></div>`;

  backfillResultEl.innerHTML = banner + judgeTable + assignTable + unitTable;
}

backfillRunBtn.addEventListener("click", async () => {
  if (!analysis || analysis.counts.scores === 0) return;
  const target = Number(backfillTargetInput.value);
  if (!Number.isInteger(target) || target < 1) {
    setBackfillMsg("每单元目标评委人数必须是不小于 1 的整数。", "error");
    toast("请填写有效的目标评委人数", true);
    return;
  }
  const caps = collectBackfillCaps();
  for (const [j, v] of Object.entries(caps)) {
    if (!Number.isInteger(v) || v < 0) {
      setBackfillMsg(`评委「${j}」的可接任务数必须是非负整数。`, "error");
      toast("评委容量必须是非负整数", true);
      return;
    }
  }
  setBackfillMsg("正在计算最大分派方案…");
  try {
    backfillData = await api(`/api/sessions/${SESSION_ID}/backfill/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, capacities: caps }),
    });
    backfillStale = false;
    backfillSig = backfillSignature(analysis);
    renderBackfillPanel();
    const s = backfillData.summary;
    setBackfillMsg(
      `预览完成：最优分派 ${s.assigned_total} 个任务，未满足 ${s.unmet_total} 个（未写入任何数据）。`,
      "ok",
    );
  } catch (err) {
    backfillData = null;
    renderBackfillPanel();
    setBackfillMsg(err.message, "error");
    toast("生成补评分派预览失败：" + err.message, true);
  }
});

backfillClearBtn.addEventListener("click", () => {
  backfillData = null;
  backfillStale = false;
  renderBackfillPanel();
  setBackfillMsg("已清除预览，数据未做任何改动。", "ok");
});

// ---------- Item weight threshold analysis (read-only) ----------
const weightItemSel = $("#weight-item");
const weightUpperInput = $("#weight-upper");
const weightRunBtn = $("#btn-weight-run");
const weightResultEl = $("#weight-result");

function setWeightMsg(text, cls = "") {
  const el = $("#weight-msg");
  if (el) {
    el.textContent = text;
    el.className = "msg " + cls;
  }
}

// 结果依赖当前校准范围、逐项裁决（最终总分/项目最终均分）与评分本身。
function weightSignature(a) {
  const parts = ["scope:" + a.scope];
  for (const b of a.contestants) {
    parts.push(`${b.contestant}=${Number(b.eff_total).toFixed(10)}`);
    for (const ib of b.items) {
      parts.push(`${b.contestant}|${ib.item}=${Number(ib.eff_avg).toFixed(10)}`);
    }
  }
  return parts.join("#");
}

function syncWeightControls() {
  const hasData = !!(analysis && analysis.counts.scores > 0);
  const prev = weightItemSel.value;
  weightItemSel.innerHTML = hasData
    ? analysis.items.map((it) => `<option value="${esc(it)}">${esc(it)}</option>`).join("")
    : "";
  if (hasData && analysis.items.includes(prev)) weightItemSel.value = prev;
  weightRunBtn.disabled = !hasData;
  weightItemSel.disabled = !hasData;
  weightUpperInput.disabled = !hasData;
}

function renderWeightPanel() {
  syncWeightControls();
  renderWeightResult();
}

// 倍率显示精度：彼此接近的倍率（如 1 与 1.0000000005）必须显示为不同的
// 字符串——从 4 位小数起逐位增加，直到所有不同的 w 值都彼此可区分
// （同一浮点值仍显示相同；15 位小数已覆盖双精度有效位）。
function weightWDigits(entries) {
  const vals = [];
  for (const e of entries) {
    vals.push(e.w);
    if (e.kind === "midpoint") vals.push(e.left, e.right);
  }
  for (let d = 4; d < 15; d++) {
    const seen = new Map();
    let clash = false;
    for (const v of vals) {
      const s = Number(v).toFixed(d);
      if (seen.has(s) && seen.get(s) !== v) { clash = true; break; }
      seen.set(s, v);
    }
    if (!clash) return d;
  }
  return 15;
}

function renderWeightResult() {
  if (!weightData) {
    weightResultEl.innerHTML = "";
    return;
  }
  const p = weightData;
  const s = p.summary;
  const missingNote = p.contestants_missing_item.length
    ? `缺失该项目（按 <b>0 贡献</b>，斜率为 0）：${p.contestants_missing_item.map(esc).join("、")}。`
    : "所有选手在该项目均有评分。";
  const alwaysNote = p.always_tied_groups.length
    ? `<br><span class="wt-note">始终并列（不产生交点）：</span>` +
      p.always_tied_groups.map((g) => `<b>${g.map(esc).join(" = ")}</b>`).join("；")
    : "";
  const banner = `
    <div class="wt-banner">
      项目 <b>${esc(p.item)}</b> 倍率从 <b>${fmt(p.lower, 1)}</b> 连续变化到上限 <b>${fmt(p.upper_bound, 1)}</b>
      （校准范围：${esc(p.scope_label)}，只读、实时计算，不写入数据）｜
      选手 <b>${s.contestants}</b> 名，区间内选手对相交 <b>${s.crossing_pairs}</b> 次，
      合并同位置交点后临界倍率 <b class="wt-crit">${s.critical_points}</b> 个，
      相邻开区间中点 <b>${s.interval_midpoints}</b> 个。<br>
      <span class="wt-note">${missingNote}</span>${alwaysNote}
    </div>`;

  const rankTable = (entry) => {
    const rows = entry.ranking.map((r) => `
      <tr class="${r.tied ? "wt-tie-row" : ""}">
        <td class="num"><b>${r.rank}</b></td>
        <td>${esc(r.contestant)}${r.tied ? ' <span class="wt-tie-tag">交点并列</span>' : ""}</td>
        <td class="num">${r.has_item ? fmt(r.item_avg) : '<span class="wt-missing">无（0 贡献）</span>'}</td>
        <td class="num"><b>${fmt(r.total)}</b></td>
      </tr>`).join("");
    return `
      <div class="table-wrap wt-table-wrap"><table>
        <tr><th class="num">名次</th><th>选手</th><th class="num">该项目最终均分</th><th class="num">总分</th></tr>
        ${rows}
      </table></div>`;
  };

  // 彼此接近的倍率按足够精度显示，避免不同的临界点都写成 1.0000。
  const wDigits = weightWDigits(p.entries);

  const blocks = p.entries.map((e) => {
    if (e.kind === "critical") {
      const atEdge = e.boundary === "start" ? "（区间起点 w=0）"
        : e.boundary === "end" ? `（区间上限 w=${fmt(p.upper_bound, 1)}）` : "";
      const tieHtml = e.ties
        .map((g) => g.map((n) => `<span class="wt-tie-name">${esc(n)}</span>`).join('<span class="wt-eq"> = </span>'))
        .join('<span class="wt-or">　｜　</span>');
      return `
        <div class="wt-entry wt-entry-critical">
          <h4>🔶 临界倍率 w = <b class="wt-crit">${fmt(e.w, wDigits)}</b> ${atEdge}</h4>
          <p class="hint wt-tie-line">交点处并列：${tieHtml}</p>
          ${rankTable(e)}
        </div>`;
    }
    return `
      <div class="wt-entry wt-entry-mid">
        <h4>▫ 开区间中点 w = <b>${fmt(e.w, wDigits)}</b>
          <small>（相邻临界点 <code>${fmt(e.left, wDigits)}</code> &lt; w &lt; <code>${fmt(e.right, wDigits)}</code> 的中点）</small>
        </h4>
        ${rankTable(e)}
      </div>`;
  }).join("");

  weightResultEl.innerHTML = banner +
    (blocks
      ? blocks
      : '<p class="no-reversal">该区间内没有任何选手对相交：所有选手总分的相对次序在整个区间保持不变。</p>');
}

function runWeightAnalysis() {
  if (!analysis || analysis.counts.scores === 0) return;
  let item = weightItemSel.value;
  // 重新上传后旧选择可能已不存在：回落到第一个项目并同步控件。
  if (!analysis.items.includes(item)) {
    item = analysis.items[0];
    weightItemSel.value = item;
  }
  const upper = Number(weightUpperInput.value);
  if (!item) {
    setWeightMsg("请先选择要分析的项目。", "error");
    return;
  }
  if (!Number.isFinite(upper) || upper < 1 || upper > 5) {
    setWeightMsg("倍率上限必须是 1～5 之间的数字。", "error");
    toast("倍率上限必须在 1～5 之间", true);
    return;
  }
  const myId = ++weightReqId;
  setWeightMsg("计算中…");
  api(`/api/sessions/${SESSION_ID}/weight-thresholds`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item, upper_bound: upper }),
  })
    .then((data) => {
      if (myId !== weightReqId) return;
      weightData = data;
      weightSig = weightSignature(analysis);
      renderWeightPanel();
      setWeightMsg(
        `计算完成：${data.summary.critical_points} 个临界倍率（未写入任何数据，只读）。`,
        "ok",
      );
    })
    .catch((err) => {
      if (myId !== weightReqId) return;
      weightData = null;
      renderWeightPanel();
      setWeightMsg(err.message, "error");
      toast("权重临界点分析失败：" + err.message, true);
    });
}

weightRunBtn.addEventListener("click", runWeightAnalysis);
weightItemSel.addEventListener("change", runWeightAnalysis);
weightUpperInput.addEventListener("change", runWeightAnalysis);

// ---------- Promotion threshold check (read-only) ----------
const promoTotalInput = $("#promo-total");
const promoInputsEl = $("#promo-inputs");
const promoResultEl = $("#promo-result");
const promoRunBtn = $("#btn-promo-run");
const promoClearBtn = $("#btn-promo-clear");

function setPromoMsg(text, cls = "") {
  const el = $("#promo-msg");
  if (el) {
    el.textContent = text;
    el.className = "msg " + cls;
  }
}

// 门槛输入表只在项目集合变化时整体重建；重建保留同名项目已填的最低线。
function renderPromotionInputs() {
  if (!analysis || analysis.counts.scores === 0) {
    promoInputsEl.innerHTML = "";
    promoRunBtn.disabled = true;
    promoTotalInput.disabled = true;
    return;
  }
  promoRunBtn.disabled = false;
  promoTotalInput.disabled = false;
  const key = analysis.items.join("|");
  if (key === promoItemKey) return;
  promoItemKey = key;
  const rows = analysis.items.map((it) => {
    const val = promoItemLines[it] !== undefined ? promoItemLines[it] : "";
    return `
      <tr>
        <td>${esc(it)}</td>
        <td class="num"><input type="number" class="promo-line-input" data-item="${esc(it)}"
          step="any" placeholder="不设门槛" value="${esc(String(val))}"></td>
      </tr>`;
  }).join("");
  promoInputsEl.innerHTML = `
    <p class="hint" style="margin:8px 0 6px;">各项目最终均分最低线（留空表示该项目不设门槛；至少填写一个项目）：</p>
    <div class="table-wrap promo-table-wrap"><table>
      <tr><th>项目</th><th class="num">最终均分最低线</th></tr>
      ${rows}
    </table></div>`;
  promoInputsEl.querySelectorAll(".promo-line-input").forEach((input) => {
    input.addEventListener("input", (ev) => {
      promoItemLines[ev.target.dataset.item] = ev.target.value;
      syncPromoClearBtn();
    });
  });
}

// 从草稿收集请求体；校验失败时按 silent 决定是否提示。返回 null 表示不发请求。
function collectPromotionRequest({ silent = false } = {}) {
  const fail = (msg) => {
    if (!silent) {
      setPromoMsg(msg, "error");
      toast(msg, true);
    }
    return null;
  };
  const item_thresholds = [];
  for (const it of analysis.items) {
    const raw = String(promoItemLines[it] !== undefined ? promoItemLines[it] : "").trim();
    if (raw === "") continue;
    const v = Number(raw);
    if (!Number.isFinite(v)) return fail(`项目「${it}」的最低线必须是数字。`);
    item_thresholds.push({ item: it, min_avg: v });
  }
  if (!item_thresholds.length) return fail("请至少为一个项目填写最终项目均分最低线。");
  let total_threshold = null;
  const totalRaw = String(promoTotalDraft).trim();
  if (totalRaw !== "") {
    const v = Number(totalRaw);
    if (!Number.isFinite(v)) return fail("最终总分线必须是数字（或留空不检查总分）。");
    total_threshold = v;
  }
  return { item_thresholds, total_threshold };
}

function syncPromoClearBtn() {
  const hasDraft =
    String(promoTotalDraft).trim() !== "" ||
    Object.values(promoItemLines).some((v) => String(v).trim() !== "");
  promoClearBtn.disabled = !promoData && !hasDraft;
}

function renderPromotionPanel() {
  renderPromotionInputs();
  if (promoTotalInput.value !== promoTotalDraft) promoTotalInput.value = promoTotalDraft;
  syncPromoClearBtn();
  renderPromotionResult();
}

function renderPromotionResult() {
  if (!promoData) {
    promoResultEl.innerHTML = "";
    return;
  }
  const p = promoData;
  const s = p.summary;
  const t = p.thresholds;
  const lineItems = t.items.map((x) => `${esc(x.item)}≥${fmt(x.min_avg)}`).join("，");
  const totalText = t.total === null ? "未设置总分线" : `最终总分线 <b>${fmt(t.total)}</b>`;
  const banner = `
    <div class="promo-banner">
      晋级门槛检查（校准范围：<b>${esc(p.scope_label)}</b>，只读、实时计算，不写入数据）｜
      门槛：${lineItems}；${totalText}。<br>
      <b class="promo-pass">达标 ${s.pass}</b> 名 ｜
      <b class="promo-fail">未达标 ${s.fail}</b> 名 ｜
      <b class="promo-ins">数据不足 ${s.insufficient_data}</b> 名
      （共 ${s.contestants} 名）。
      <span class="promo-note">缺失门槛项目不按 0 分处理，也不参与总分线判定。</span>
    </div>`;

  const statusBadge = (row) =>
    `<span class="promo-badge promo-${row.status}">${esc(row.status_label)}</span>`;

  const avgCell = (cell) => {
    if (!cell.has_data) return `<td class="num promo-no-data">无数据</td>`;
    let cls = "";
    if (cell.threshold !== null && cell.threshold !== undefined) {
      cls = cell.passes ? " promo-cell-pass" : " promo-cell-fail";
    }
    return `<td class="num${cls}">${fmt(cell.avg)}${cell.threshold !== null && cell.threshold !== undefined ? `<small> / ${fmt(cell.threshold)}</small>` : ""}</td>`;
  };

  const totalCell = (row) => {
    if (row.status === "insufficient_data") {
      return `<td class="num"><b>${fmt(row.total)}</b></td>
        <td class="num promo-no-data">—（不判定）</td><td><span class="promo-ins">数据不足</span></td>`;
    }
    if (row.total_check === null) {
      return `<td class="num"><b>${fmt(row.total)}</b></td><td class="num">—</td><td><span class="delta-flat">未设总分线</span></td>`;
    }
    const cls = row.total_check.passes ? "promo-pass" : "promo-fail";
    return `<td class="num"><b>${fmt(row.total)}</b></td>
      <td class="num">${fmt(row.total_check.threshold)}</td>
      <td><span class="${cls}">${row.total_check.passes ? "达标" : `差 ${fmt(row.total_check.gap)}`}</span></td>`;
  };

  const rows = p.contestants.map((row) => `
    <tr class="promo-row-${row.status}">
      <td class="num"><b>${row.rank}</b></td>
      <td>${esc(row.contestant)}</td>
      ${p.contestants[0].items.map((cell) => avgCell(row.items.find((x) => x.item === cell.item))).join("")}
      ${totalCell(row)}
      <td>${statusBadge(row)}</td>
      <td class="promo-reasons">${row.reasons.length
        ? row.reasons.map((r) => `<div>${esc(r.message)}</div>`).join("")
        : '<span class="delta-flat">—</span>'}</td>
    </tr>`).join("");

  // 表头：每个项目一列（门槛线在副标题中已列出），其后总分 / 总分线 / 总分判定 / 状态 / 未达标原因。
  const itemHead = p.contestants[0].items
    .map((cell) => {
      const line = cell.threshold !== null && cell.threshold !== undefined;
      return `<th class="num">${esc(cell.item)}${line ? '<br><small>均分/门槛</small>' : '<br><small>最终均分</small>'}</th>`;
    })
    .join("");

  promoResultEl.innerHTML = banner + `
    <div class="table-wrap promo-result-wrap"><table>
      <tr><th class="num">最终名次</th><th>选手</th>${itemHead}
        <th class="num">最终总分</th><th class="num">总分线</th><th>总分判定</th>
        <th>结论</th><th>未达标 / 数据不足原因</th>
      </tr>
      ${rows}
    </table></div>
    <p class="hint promo-foot">
      列中 <code>均分 / 门槛</code> 分别为该选手该项目的最终均分与所设最低线；绿色为达标、红色为低于门槛、
      「无数据」不按 0 分计。所有数字由当前评分、裁决与校准范围实时计算，清空会话或清空门槛后本结果消失。
    </p>`;
}

function runPromotionCheck({ auto = false } = {}) {
  if (!analysis || analysis.counts.scores === 0) return;
  const payload = collectPromotionRequest({ silent: auto });
  if (!payload) {
    if (!auto) promoData = null;
    renderPromotionPanel();
    return;
  }
  const myId = ++promoReqId;
  if (!auto) setPromoMsg("检查中…");
  api(`/api/sessions/${SESSION_ID}/promotion-check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then((data) => {
      if (myId !== promoReqId) return;
      promoData = data;
      promoSig = weightSignature(analysis);
      promoTotalDraft = promoTotalInput.value;
      renderPromotionPanel();
      const s = data.summary;
      setPromoMsg(
        `检查完成：达标 ${s.pass}、未达标 ${s.fail}、数据不足 ${s.insufficient_data}（只读，未写入数据）。`,
        "ok",
      );
    })
    .catch((err) => {
      if (myId !== promoReqId) return;
      promoData = null;
      renderPromotionPanel();
      setPromoMsg(err.message, "error");
      if (!auto) toast("晋级门槛检查失败：" + err.message, true);
    });
}

promoRunBtn.addEventListener("click", () => runPromotionCheck());
promoTotalInput.addEventListener("input", () => {
  promoTotalDraft = promoTotalInput.value;
  syncPromoClearBtn();
});

promoClearBtn.addEventListener("click", () => {
  promoReqId += 1;
  promoData = null;
  promoSig = "";
  promoItemLines = {};
  promoItemKey = "";
  promoTotalDraft = "";
  renderPromotionPanel();
  setPromoMsg("已清空全部门槛与结果，数据未做任何改动。", "ok");
});

// ---------- Pending-score rank bounds (read-only) ----------
const pendingItemSel = $("#pending-item");
const pendingTargetInput = $("#pending-target");
const pendingMinInput = $("#pending-min");
const pendingMaxInput = $("#pending-max");
const pendingResultEl = $("#pending-result");
const pendingRunBtn = $("#btn-pending-run");
const pendingClearBtn = $("#btn-pending-clear");

function setPendingMsg(text, cls = "") {
  const el = $("#pending-msg");
  if (el) {
    el.textContent = text;
    el.className = "msg " + cls;
  }
}

// 结果依赖当前校准范围、逐项裁决（最终总分/项目最终均分）与该项目已有评分数。
function pendingSignature(a) {
  const parts = ["scope:" + a.scope];
  for (const b of a.contestants) {
    parts.push(`${b.contestant}=${Number(b.eff_total).toFixed(10)}`);
    for (const ib of b.items) {
      parts.push(`${b.contestant}|${ib.item}|${ib.scores.length}=${Number(ib.eff_avg).toFixed(10)}`);
    }
  }
  return parts.join("#");
}

function syncPendingControls() {
  const hasData = !!(analysis && analysis.counts.scores > 0);
  const prev = pendingItemSel.value;
  pendingItemSel.innerHTML = hasData
    ? analysis.items.map((it) => `<option value="${esc(it)}">${esc(it)}</option>`).join("")
    : "";
  if (hasData && analysis.items.includes(prev)) pendingItemSel.value = prev;
  pendingRunBtn.disabled = !hasData;
  pendingItemSel.disabled = !hasData;
  pendingTargetInput.disabled = !hasData;
  pendingMinInput.disabled = !hasData;
  pendingMaxInput.disabled = !hasData;
}

// 从控件收集请求体；校验失败时按 silent 决定是否提示。返回 null 表示不发请求。
function collectPendingRequest({ silent = false } = {}) {
  const fail = (msg) => {
    if (!silent) {
      setPendingMsg(msg, "error");
      toast(msg, true);
    }
    return null;
  };
  const item = pendingItemSel.value;
  if (!item) return fail("请选择要分析的项目。");
  const target = Number(pendingTargetInput.value);
  if (!Number.isInteger(target) || target < 1) {
    return fail("目标评委人数必须是不小于 1 的整数。");
  }
  const loRaw = String(pendingMinInput.value).trim();
  const hiRaw = String(pendingMaxInput.value).trim();
  const lo = Number(loRaw);
  const hi = Number(hiRaw);
  if (loRaw === "" || !Number.isFinite(lo)) return fail("待补评分下限必须是数字。");
  if (hiRaw === "" || !Number.isFinite(hi)) return fail("待补评分上限必须是数字。");
  if (lo > hi) return fail("待补评分下限不能大于上限。");
  return { item, target, score_min: lo, score_max: hi };
}

// 结果生成后再改参数 → 标记结果过期（不自动重算）。
function markPendingStale() {
  if (pendingData && !pendingStale) {
    pendingStale = true;
    renderPendingResult();
  }
}

function renderPendingPanel() {
  syncPendingControls();
  pendingClearBtn.disabled = !pendingData;
  renderPendingResult();
}

function renderPendingResult() {
  if (!pendingData) {
    pendingResultEl.innerHTML = "";
    return;
  }
  const p = pendingData;
  const s = p.summary;
  const banner = `
    <div class="pending-banner">
      名次边界（项目 <b>${esc(p.item)}</b>，目标 <b>${p.target}</b> 位评委，待补分区间
      <b>[${fmt(p.score_min)}, ${fmt(p.score_max)}]</b>；校准范围：<b>${esc(p.scope_label)}</b>，只读、实时计算，不写入数据）｜
      选手 <b>${s.contestants}</b> 名，有缺口 <b>${s.contestants_with_gap}</b> 名，缺口合计 <b>${s.gap_total}</b> 条
      ${s.missing_item ? `（其中 <b>${s.missing_item}</b> 名在该项目尚无评分，按整区间补足）` : ""}。
      ${pendingStale
        ? '<span class="pending-stale">⚠ 参数在上次计算后已修改，以下结果已过期，请重新计算。</span>'
        : '<span class="pending-note">名次区间按「本人总分上限 vs 他人下限 / 本人下限 vs 他人上限」计算，同分容差与主排名一致；其余项目保持现值。</span>'}
    </div>`;

  const rows = p.contestants.map((r) => {
    const curAvg = r.current_item_avg === null
      ? '<span class="pending-none">无评分</span>'
      : fmt(r.current_item_avg);
    return `
    <tr>
      <td class="num">${r.rank}</td>
      <td>${esc(r.contestant)}</td>
      <td class="num">${r.existing_count}</td>
      <td class="num"><b class="${r.gap > 0 ? "pending-gap" : "delta-flat"}">${r.gap}</b></td>
      <td class="num">${curAvg}</td>
      <td class="num">${fmt(r.item_avg_min)} ~ ${fmt(r.item_avg_max)}</td>
      <td class="num"><b>${fmt(r.total_min)} ~ ${fmt(r.total_max)}</b></td>
      <td class="num"><b class="pending-rank">${r.best_rank} ~ ${r.worst_rank}</b></td>
    </tr>`;
  }).join("");

  pendingResultEl.innerHTML = banner + `
    <div class="table-wrap pending-result-wrap"><table>
      <tr><th class="num">当前<br>名次</th><th>选手</th>
        <th class="num">已有<br>评分数</th><th class="num">缺口</th>
        <th class="num">当前项目<br>最终均分</th>
        <th class="num">补足后项目<br>均分区间</th>
        <th class="num">最终总分区间</th>
        <th class="num">可达到名次区间<br>（最好 ~ 最差）</th>
      </tr>
      ${rows}
    </table></div>
    <p class="hint pending-foot">
      缺口 = 目标评委人数 − 该项目已有评分数；每条待补分可独立取区间内任意值，项目均分区间 =
      （已有最终分之和 + 缺口 × 下/上限）÷ 目标人数，最终总分区间 = 其余项目现值 + 项目均分区间。
      最好名次按本人总分上限与他人下限比较，最差名次按本人下限与他人上限比较（同分容差与主排名一致）。
      所有数字由当前评分、裁决与校准范围实时计算，清除结果或清空会话后本表消失。
    </p>`;
}

function runPendingCheck({ auto = false } = {}) {
  if (!analysis || analysis.counts.scores === 0) return;
  const payload = collectPendingRequest({ silent: auto });
  if (!payload) {
    if (!auto) {
      pendingData = null;
      renderPendingPanel();
    }
    return;
  }
  const myId = ++pendingReqId;
  if (!auto) setPendingMsg("计算中…");
  api(`/api/sessions/${SESSION_ID}/pending-rank`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then((data) => {
      if (myId !== pendingReqId) return;
      pendingData = data;
      pendingStale = false;
      pendingSig = pendingSignature(analysis);
      renderPendingPanel();
      setPendingMsg(
        `计算完成：${data.summary.contestants} 名选手的总分区间与名次区间已更新（只读，未写入数据）。`,
        "ok",
      );
    })
    .catch((err) => {
      if (myId !== pendingReqId) return;
      pendingData = null;
      renderPendingPanel();
      setPendingMsg(err.message, "error");
      if (!auto) toast("名次边界计算失败：" + err.message, true);
    });
}

pendingRunBtn.addEventListener("click", () => runPendingCheck());
pendingItemSel.addEventListener("change", markPendingStale);
pendingTargetInput.addEventListener("input", markPendingStale);
pendingMinInput.addEventListener("input", markPendingStale);
pendingMaxInput.addEventListener("input", markPendingStale);

pendingClearBtn.addEventListener("click", () => {
  pendingReqId += 1;
  pendingData = null;
  pendingStale = false;
  pendingSig = "";
  renderPendingPanel();
  setPendingMsg("已清除结果，数据未做任何改动。", "ok");
});

// ---------- Common-judge comparable comparison (read-only) ----------
const cjPanel = $("#cj-panel");

function setCjMsg(text, cls = "") {
  const el = $("#cj-msg");
  if (el) {
    el.textContent = text;
    el.className = "msg " + cls;
  }
}

// 结果依赖当前校准范围、逐项裁决（最终分）与评分本身（评委覆盖）。
function commonJudgeSignature(a) {
  const parts = ["scope:" + a.scope];
  for (const b of a.contestants) {
    for (const ib of b.items) {
      for (const s of ib.scores) {
        parts.push(`${b.contestant}|${ib.item}|${s.judge}=${Number(s.effective).toFixed(10)}`);
      }
    }
  }
  return parts.join("#");
}

function renderCommonJudgePanel() {
  if (!cjPanel) return;
  if (!analysis || analysis.counts.scores === 0) {
    cjPanel.innerHTML = "";
    return;
  }
  if (!commonJudgeData) {
    cjPanel.innerHTML = "";
    return;
  }
  const p = commonJudgeData;
  const s = p.summary;

  const banner = `
    <div class="cj-banner">
      共 <b>${s.contestants}</b> 名选手、<b>${s.items}</b> 个项目，选手对 <b>${s.pairs}</b> 对：
      <b class="cj-ok">可比 ${s.comparable}</b> 对、
      <span class="cj-no">不可比 ${s.incomparable}</span> 对
      <span class="cj-note">（因缺项目 ${s.incomparable_missing_item_pairs} 对、无共同评委 ${s.incomparable_no_common_pairs} 对，按主因可重复计数）</span>。
      可比对中：<b class="cj-a">A 领先 ${s.a_leads}</b>、<b class="cj-b">B 领先 ${s.b_leads}</b>、持平 ${s.ties}；
      与全量最终名次方向对照：<b class="cj-reversed">反向 ${s.reversed}</b> 对、同向 ${s.same_direction} 对、全量并列 ${s.full_tie} 对。
      <span class="cj-note">全量领先/并列以主榜最终名次为准；所有差值均为同一共同评委集合上的最终分均分差（A−B）；不可比项目不按零分。</span>
    </div>`;

  // 反向对置顶，其次可比对（按可比分差绝对值降序），最后不可比对；组内按全量名次。
  const rankOf = {};
  analysis.contestants.forEach((b) => { rankOf[b.contestant] = b.eff_rank; });
  const ordered = p.pairs.slice().sort((p1, p2) => {
    if (p1.reversed !== p2.reversed) return p1.reversed ? -1 : 1;
    if (p1.comparable !== p2.comparable) return p1.comparable ? -1 : 1;
    const d1 = p1.comparable ? Math.abs(p1.comparable_delta) : 0;
    const d2 = p2.comparable ? Math.abs(p2.comparable_delta) : 0;
    if (d1 !== d2) return d2 - d1;
    return (rankOf[p1.a] - rankOf[p2.a]) || (rankOf[p1.b] - rankOf[p2.b]) ||
      p1.a.localeCompare(p2.a) || p1.b.localeCompare(p2.b);
  });

  const leadBadge = (leader, labels) => {
    if (leader === "a") return `<span class="cj-lead-a">${esc(labels.a || "A 领先")}</span>`;
    if (leader === "b") return `<span class="cj-lead-b">${esc(labels.b || "B 领先")}</span>`;
    if (leader === "tie") return `<span class="cj-tie">${esc(labels.tie || "持平")}</span>`;
    return `<span class="cj-na">不可比</span>`;
  };
  const directionBadge = (pair) => {
    if (pair.direction === "reversed") {
      return `<span class="cj-dir-reversed">⚠ ${esc(pair.direction_label)}</span>`;
    }
    if (pair.direction === "same") return `<span class="cj-dir-same">${esc(pair.direction_label)}</span>`;
    if (pair.direction === "full_tie") return `<span class="cj-dir-tie">${esc(pair.direction_label)}</span>`;
    return `<span class="cj-dir-na">${esc(pair.direction_label)}</span>`;
  };

  const rows = ordered.map((pair, idx) => {
    const coverage = pair.items
      .filter((it) => it.comparable)
      .map((it) => `${esc(it.item)}×${it.common_count}`)
      .join("、") || '<span class="cj-no">—</span>';
    const incNames = pair.items
      .filter((it) => !it.comparable)
      .map((it) => esc(it.item))
      .join("、");
    const compCell = pair.comparable
      ? {
          a: fmt(pair.a_comparable_total),
          b: fmt(pair.b_comparable_total),
          d: totalDeltaHtml(pair.comparable_delta),
          lead: leadBadge(pair.comparable_leader, p.leader_labels),
        }
      : { a: "—", b: "—", d: '<span class="cj-na">—</span>', lead: '<span class="cj-na">不可比</span>' };

    const detailItems = pair.items.map((it) => {
      if (!it.comparable) {
        let why;
        if (it.reason === "missing_item") {
          if (it.both_missing) why = "双方在该项目都没有评分";
          else if (it.missing_side === pair.a) why = `<b>${esc(pair.a)}</b> 在该项目没有评分`;
          else why = `<b>${esc(pair.b)}</b> 在该项目没有评分`;
        } else {
          const ja = it.a_judges ? it.a_judges.map((x) => esc(x.judge)).join("、") : "";
          const jb = it.b_judges ? it.b_judges.map((x) => esc(x.judge)).join("、") : "";
          why = `双方都有该项目但<b>没有共同评委</b>` +
            (ja || jb ? `（${esc(pair.a)}：${ja || "无"}；${esc(pair.b)}：${jb || "无"}）` : "");
        }
        return `
          <div class="cj-item cj-item-na">
            <h5>${esc(it.item)} <span class="cj-item-badge">不可比 · ${esc(it.reason_label)}</span></h5>
            <p class="hint" style="margin:2px 0;">${why}，<b>不按零分</b>，不计入可比总分差。</p>
          </div>`;
      }
      const judgeRows = it.common_judges.map((j) => {
        const av = it.a_judges.find((x) => x.judge === j);
        const bv = it.b_judges.find((x) => x.judge === j);
        const d = av.effective - bv.effective;
        return `
          <tr>
            <td>${esc(j)}</td>
            <td class="num">${fmt(av.effective)}</td>
            <td class="num">${fmt(bv.effective)}</td>
            <td class="num">${totalDeltaHtml(d)}</td>
          </tr>`;
      }).join("");
      const onlyA = it.a_judges.filter((x) => !it.common_judges.includes(x.judge)).map((x) => esc(x.judge));
      const onlyB = it.b_judges.filter((x) => !it.common_judges.includes(x.judge)).map((x) => esc(x.judge));
      const coverNote = (list, who) => list.length
        ? `<span class="cj-extra-judges">${who}另评但非共同：${list.join("、")}（不参与同评委均分）</span>` : "";
      const lead = it.leader === "a"
        ? `<span class="cj-lead-a">${esc(pair.a)} 领先</span>`
        : it.leader === "b"
          ? `<span class="cj-lead-b">${esc(pair.b)} 领先</span>`
          : '<span class="cj-tie">持平</span>';
      return `
        <div class="cj-item cj-item-ok">
          <h5>${esc(it.item)}
            <span class="cj-item-badge cj-item-ok-badge">可比 · 共同评委 ${it.common_count} 人：${it.common_judges.map(esc).join("、")}</span>
          </h5>
          <div class="table-wrap cj-judge-wrap"><table>
            <tr><th>共同评委</th><th class="num">${esc(pair.a)} 最终分</th><th class="num">${esc(pair.b)} 最终分</th><th class="num">差 (A−B)</th></tr>
            ${judgeRows}
            <tr class="cj-avg-row">
              <td><b>共同评委均分</b></td>
              <td class="num"><b>${fmt(it.a_avg)}</b></td>
              <td class="num"><b>${fmt(it.b_avg)}</b></td>
              <td class="num">${totalDeltaHtml(it.delta)}　${lead}</td>
            </tr>
          </table></div>
          <p class="hint cj-cover-note" style="margin:4px 0 0;">
            全量项目均分（含非共同评委）：${esc(pair.a)} ${fmt(it.a_full_avg)}｜${esc(pair.b)} ${fmt(it.b_full_avg)}。
            ${coverNote(onlyA, esc(pair.a))}${coverNote(onlyB, esc(pair.b))}
          </p>
        </div>`;
    }).join("");

    const detailRow = `
      <tr class="cj-detail-row" data-detail="${idx}" hidden>
        <td colspan="10">
          <div class="cj-detail">
            <div class="cj-detail-summary">
              可比项目 <b>${pair.comparable_items}</b> 个、不可比项目 <b>${pair.incomparable_items}</b> 个。
              可比总分 = 各可比项目的<b>同评委均分</b>之和：
              <b>${esc(pair.a)}</b> ${pair.comparable ? fmt(pair.a_comparable_total) : "—"}
              ｜ <b>${esc(pair.b)}</b> ${pair.comparable ? fmt(pair.b_comparable_total) : "—"}。
              现有全量最终总分：<b>${esc(pair.a)}</b> ${fmt(pair.a_full_total)}（第 ${pair.a_rank} 名）
              ｜ <b>${esc(pair.b)}</b> ${fmt(pair.b_full_total)}（第 ${pair.b_rank} 名）。
            </div>
            ${detailItems}
          </div>
        </td>
      </tr>`;

    return `
      <tr class="cj-row${pair.reversed ? " cj-reversed-row" : ""}${pair.comparable ? "" : " cj-incomparable-row"}" data-row="${idx}">
        <td><span class="cj-toggle" data-toggle="${idx}">▸</span></td>
        <td class="cj-pair-name">#${pair.a_rank} <b>${esc(pair.a)}</b><span class="cj-vs"> / </span>#${pair.b_rank} <b>${esc(pair.b)}</b></td>
        <td class="num">${pair.comparable_items} / ${s.items}</td>
        <td class="cj-coverage">${coverage}${incNames ? `<div class="cj-inc-names">不可比：${incNames}</div>` : ""}</td>
        <td class="num">${fmt(pair.a_full_total)}</td>
        <td class="num">${fmt(pair.b_full_total)}</td>
        <td class="num">${totalDeltaHtml(pair.full_delta)}<div class="cj-sub">${leadBadge(pair.full_leader, { a: pair.a, b: pair.b, tie: "并列" })}</div></td>
        <td class="num">${compCell.a}<br>${compCell.b}</td>
        <td class="num">${compCell.d}<div class="cj-sub">${compCell.lead}</div></td>
        <td>${directionBadge(pair)}</td>
      </tr>
      ${detailRow}`;
  }).join("");

  cjPanel.innerHTML = banner + `
    <div class="table-wrap cj-table-wrap"><table>
      <tr>
        <th></th>
        <th>选手对（A / B）</th>
        <th class="num">可比<br>项目数</th>
        <th>共同评委覆盖<br><small>（项目×评委数）</small></th>
        <th class="num">全量最终分<br>A</th>
        <th class="num">全量最终分<br>B</th>
        <th class="num">全量总分差<br>(A−B)</th>
        <th class="num">可比总分<br>A / B</th>
        <th class="num">可比总分差<br>(A−B) / 领先</th>
        <th>方向对照</th>
      </tr>
      ${rows}
    </table></div>
    <p class="hint cj-foot" style="margin-bottom:0;">
      「全量最终分」为各项目全部参评评委均分再跨项目求和的现有最终总分；「可比总分」仅汇总逐项目<b>共同评委</b>均分。
      全量口径的领先、并列与反向以主榜<b>最终名次</b>为准（名次相同即全量并列、不构成反向；名次有先后时再与可比方向比较），不按全量总分差落入容差判定；两种口径方向相反的选手对标为<b class="cj-reversed">反向</b>并置顶；不可比选手对不按 0 分、不参与方向对照。
      点行首箭头展开可查看逐项目共同评委与每位评委的最终分明细。本面板只读，评分、裁决或校准范围变化后自动重算。
    </p>`;

  cjPanel.querySelectorAll(".cj-toggle").forEach((toggle) => {
    toggle.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const idx = toggle.dataset.toggle;
      const detail = cjPanel.querySelector(`.cj-detail-row[data-detail="${idx}"]`);
      const row = cjPanel.querySelector(`.cj-row[data-row="${idx}"]`);
      if (detail.hasAttribute("hidden")) {
        detail.removeAttribute("hidden");
        toggle.textContent = "▾";
        row.classList.add("cj-open");
      } else {
        detail.setAttribute("hidden", "");
        toggle.textContent = "▸";
        row.classList.remove("cj-open");
      }
    });
  });
  cjPanel.querySelectorAll(".cj-row").forEach((row) => {
    row.addEventListener("click", () => {
      cjPanel.querySelector(`.cj-toggle[data-toggle="${row.dataset.row}"]`)?.click();
    });
  });
  cjPanel.querySelectorAll(".cj-detail-row").forEach((detail) => {
    detail.addEventListener("click", (ev) => ev.stopPropagation());
  });
}

function runCommonJudge() {
  if (!analysis || analysis.counts.scores === 0) return;
  const myId = ++commonJudgeReqId;
  setCjMsg("同评委可比对照计算中…");
  api(`/api/sessions/${SESSION_ID}/common-judge`)
    .then((data) => {
      if (myId !== commonJudgeReqId) return;
      commonJudgeData = data;
      commonJudgeSig = commonJudgeSignature(analysis);
      setCjMsg("");
      renderCommonJudgePanel();
    })
    .catch((err) => {
      if (myId !== commonJudgeReqId) return;
      commonJudgeData = null;
      setCjMsg(err.message, "error");
      renderCommonJudgePanel();
    });
}

// ---------- Decision updates ----------
let inflight = false;
let queued = null;

function currentState(scoreId) {
  for (const b of analysis.contestants) {
    for (const ib of b.items) {
      for (const s of ib.scores) {
        if (s.id === scoreId) return s;
      }
    }
  }
  return null;
}

async function pushDecision(scoreId, useRaw, reason) {
  if (inflight) {
    queued = { scoreId, useRaw, reason };
    return;
  }
  inflight = true;
  try {
    analysis = await api(
      `/api/sessions/${SESSION_ID}/scores/${scoreId}/decision`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ use_raw: useRaw, reason }),
      }
    );
    stability = null; // 裁决变化会改变最终总分，实验结果作废
    rulePreviewData = null;
    clearMergePreview(); // 裁决变化会使合并预览中的「旧裁决」信息过期
    render();
    renderRulesPreview();
    toast("排名已实时重算");
  } catch (err) {
    toast("保存失败：" + err.message, true);
    await loadAnalysis(); // resync from server
  } finally {
    inflight = false;
    if (queued) {
      const q = queued;
      queued = null;
      pushDecision(q.scoreId, q.useRaw, q.reason);
    }
  }
}

function onChoiceChange(ev) {
  const row = ev.target.closest(".score-row");
  const scoreId = Number(row.dataset.id);
  const useRaw = ev.target.value === "raw";
  const reasonInput = row.querySelector(".reason-input");
  pushDecision(scoreId, useRaw, reasonInput.value);
}

const reasonTimers = new Map();
function onReasonInput(ev) {
  const scoreId = Number(ev.target.dataset.id);
  clearTimeout(reasonTimers.get(scoreId));
  reasonTimers.set(scoreId, setTimeout(() => {
    const s = currentState(scoreId);
    if (s) pushDecision(scoreId, s.use_raw, ev.target.value);
  }, 500));
}

// ---------- Merge import (preview then confirm) ----------
let mergeData = null; // 最近一次合并预览结果（只读，不落库）
let mergeFilter = "all";

function clearMergePreview() {
  mergeData = null;
  mergeFilter = "all";
  renderMergePreview();
}

function setMergeMsg(text, cls = "") {
  const el = $("#merge-msg");
  if (el) {
    el.textContent = text;
    el.className = "msg " + cls;
  }
}

function decisionCell(row) {
  if (row.status === "added") return `<span class="delta-flat">—（新记录，默认采用校准分）</span>`;
  if (row.decision_action === "clear") {
    const old = row.has_decision
      ? (row.old_use_raw ? "保留原分" : "采用校准分") +
        (row.old_reason ? `（${row.old_reason}）` : "")
      : "";
    return `<span class="delta-down">旧裁决将清除${old ? "：" + esc(old) : "（该记录本无裁决）"}</span>`;
  }
  if (row.decision_action === "keep") {
    return `<span class="delta-up">裁决保留${row.old_reason ? "：" + esc(row.old_reason) : ""}</span>`;
  }
  return `<span class="delta-flat">—（无裁决，不受影响）</span>`;
}

function scorePairCell(row) {
  const oldV = row.old_score === null ? '<span class="delta-flat">—</span>' : fmt(row.old_score);
  const newV = row.new_score === null ? '<span class="delta-flat">—</span>' : fmt(row.new_score);
  let arrow = "";
  if (row.status === "changed") arrow = '<span class="mg-arrow">→</span>';
  return `<td class="num">${oldV}</td><td class="num">${arrow}</td><td class="num"><b>${newV}</b></td>`;
}

function renderMergePreview() {
  const panel = $("#merge-preview");
  if (!panel) return;
  if (!mergeData) {
    panel.innerHTML = "";
    return;
  }
  const p = mergeData;
  const c = p.counts;
  const filterDefs = [
    ["all", "全部", p.rows.length],
    ["changed", p.status_labels.changed, c.changed],
    ["added", p.status_labels.added, c.added],
    ["removed", p.status_labels.removed, c.removed],
    ["unchanged", p.status_labels.unchanged, c.unchanged],
  ];
  const chips = filterDefs.map(([key, label, n]) =>
    `<button type="button" class="chip ${mergeFilter === key ? "active" : ""}" data-filter="${key}">` +
    `${esc(label)} <b>${n}</b></button>`).join("");

  const visible = mergeFilter === "all" ? p.rows : p.rows.filter((r) => r.status === mergeFilter);
  const rowsHtml = visible.map((r) => `
    <tr class="mg-row ${r.status}">
      <td><span class="mg-badge">${esc(p.status_labels[r.status])}</span></td>
      <td>${esc(r.contestant)}</td><td>${esc(r.judge)}</td><td>${esc(r.item)}</td>
      ${scorePairCell(r)}
      <td>${decisionCell(r)}</td>
    </tr>`).join("");

  panel.innerHTML = `
    <div class="merge-panel">
      <div class="merge-banner">
        合并预览（校准范围：<b>${esc(p.scope_label)}</b>，预览只读、未改任何数据）｜
        当前会话 <b>${p.total_current}</b> 条 ← 合并文件 <b>${p.total_incoming}</b> 条：
        <span class="mg-changed">分数变更 <b>${c.changed}</b></span>、
        <span class="mg-added">新增 <b>${c.added}</b></span>、
        <span class="mg-removed">仅当前会话/将删除 <b>${c.removed}</b></span>、
        <span class="mg-unchanged">未变 <b>${c.unchanged}</b></span>。
        确认后将改动 <b>${p.will_modify}</b> 条，并清除变更/删除记录上的旧裁决
        <b class="delta-down">${p.decisions_cleared}</b> 条；
        <b>未变记录及其裁决、已保存的批处理规则保持不动</b>。
      </div>
      <div class="merge-filters">${chips}</div>
      <div class="table-wrap">
        <table>
          <tr><th>状态</th><th>选手</th><th>评委</th><th>项目</th>
          <th class="num">旧分数</th><th></th><th class="num">新分数</th><th>受影响的裁决</th></tr>
          ${rowsHtml || '<tr><td colspan="8" class="no-reversal">该分类下没有记录。</td></tr>'}
        </table>
      </div>
      <div class="toolbar merge-actions">
        <button id="btn-merge-confirm" class="btn btn-primary" ${p.will_modify === 0 ? "disabled" : ""}>
          确认合并（同一事务应用 ${p.will_modify} 条变更）
        </button>
        <button id="btn-merge-cancel" class="btn btn-ghost-card">取消（不改数据）</button>
        <span id="merge-msg" class="msg"></span>
      </div>
    </div>`;

  panel.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      mergeFilter = chip.dataset.filter;
      renderMergePreview();
    });
  });
  $("#btn-merge-cancel").addEventListener("click", () => {
    clearMergePreview();
    setMsg("已取消合并，数据未做任何改动。", "ok");
  });
  $("#btn-merge-confirm").addEventListener("click", confirmMerge);
}

$("#merge-file-input").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  const scope = document.querySelector('input[name="scope"]:checked').value;
  setMsg("正在解析并比对…");
  const fd = new FormData();
  fd.append("file", file);
  try {
    mergeData = await api(
      `/api/sessions/${SESSION_ID}/merge/preview?scope=${encodeURIComponent(scope)}`,
      { method: "POST", body: fd },
    );
    mergeFilter = "all";
    renderMergePreview();
    const c = mergeData.counts;
    setMsg(
      `合并预览：新增 ${c.added}、变更 ${c.changed}、将删除 ${c.removed}、未变 ${c.unchanged}（尚未写入）`,
      "ok",
    );
  } catch (err) {
    mergeData = null;
    renderMergePreview();
    setMsg(err.message, "error");
    toast("合并预览失败：" + err.message, true);
  }
});

async function confirmMerge() {
  const p = mergeData;
  if (!p) return;
  const c = p.counts;
  if (
    !confirm(
      `确认应用合并导入？\n` +
      `新增 ${c.added} 条，分数变更 ${c.changed} 条，删除仅存在于当前会话的 ${c.removed} 条，未变 ${c.unchanged} 条。\n` +
      `将清除 ${p.decisions_cleared} 条旧裁决；未变记录与批处理规则保持不动。\n` +
      `所有变更在同一事务中提交，失败则整体回滚；点「取消」不改任何数据。`,
    )
  ) return;
  setMergeMsg("正在同一事务中应用合并…");
  try {
    const scope = document.querySelector('input[name="scope"]:checked').value;
    analysis = await api(`/api/sessions/${SESSION_ID}/merge/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records: p.records, basis: p.basis, scope }),
    });
    const applied = analysis.merge_applied.counts;
    const cleared = analysis.merge_applied.decisions_cleared;
    mergeData = null;
    stability = null;
    rulePreviewData = null;
    render();
    renderRulesPreview();
    renderMergePreview();
    setMsg(
      `合并完成：新增 ${applied.added}、变更 ${applied.changed}、删除 ${applied.removed}、未变 ${applied.unchanged}，清除旧裁决 ${cleared} 条；批处理规则未改动。`,
      "ok",
    );
    toast("合并导入完成，排名已刷新");
  } catch (err) {
    if (String(err.message).includes("重新预览")) {
      mergeData = null;
      renderMergePreview();
    }
    setMergeMsg(err.message, "error");
    setMsg(err.message, "error");
    toast("合并失败（未写入任何数据）：" + err.message, true);
  }
}

// ---------- Data actions ----------
async function loadAnalysis() {
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/analysis`);
    stability = null;
    render();
  } catch (_) {
    analysis = null;
    stability = null;
    ruleDraft = null;
    rulePreviewData = null;
    render();
  }
}

function setMsg(text, cls = "") {
  const el = $("#import-msg");
  el.textContent = text;
  el.className = "msg " + cls;
}

$("#file-input").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const scope = document.querySelector('input[name="scope"]:checked').value;
  setMsg("上传中…");
  const fd = new FormData();
  fd.append("file", file);
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/upload?scope=${encodeURIComponent(scope)}`, {
      method: "POST",
      body: fd,
    });
    stability = null;
    ruleDraft = null;
    rulePreviewData = null;
    clearMergePreview(); // 替换导入后旧预览的基线已失效
    render();
    renderRulesPreview();
    setMsg(`已导入 ${analysis.counts.scores} 条评分`, "ok");
    toast("上传成功，已完成稳健标准化");
  } catch (err) {
    setMsg(err.message, "error");
    toast("上传失败：" + err.message, true);
  } finally {
    ev.target.value = "";
  }
});

$("#btn-sample").addEventListener("click", async () => {
  const scope = document.querySelector('input[name="scope"]:checked').value;
  setMsg("载入示例中…");
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/sample?scope=${encodeURIComponent(scope)}`, {
      method: "POST",
    });
    stability = null;
    ruleDraft = null;
    rulePreviewData = null;
    clearMergePreview();
    render();
    renderRulesPreview();
    setMsg(`示例数据已载入（${analysis.counts.scores} 条评分）`, "ok");
    toast("示例数据已载入");
  } catch (err) {
    setMsg(err.message, "error");
    toast("载入失败：" + err.message, true);
  }
});

document.querySelectorAll('input[name="scope"]').forEach((radio) => {
  radio.addEventListener("change", async (ev) => {
    if (!analysis || analysis.counts.scores === 0) return;
    try {
      analysis = await api(`/api/sessions/${SESSION_ID}/scope`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: ev.target.value }),
      });
      stability = null; // 实验按校准范围重算，旧结果与新基线无关
      rulePreviewData = null; // 校准分依赖范围，区间命中结果需重新预览
      if (mergeData) { // 合并预览的分数差异不受范围影响，仅刷新范围标签
        mergeData.scope = analysis.scope;
        mergeData.scope_label = analysis.scope_label;
        renderMergePreview();
      }
      render();
      renderRulesPreview();
      toast("校准范围已切换");
    } catch (err) {
      toast("切换失败：" + err.message, true);
      loadAnalysis();
    }
  });
});

$("#btn-reset-decisions").addEventListener("click", async () => {
  if (!confirm("确定清除全部「保留原分」裁决与理由，统一恢复为采用校准分？")) return;
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/decisions/reset`, { method: "POST" });
    stability = null;
    rulePreviewData = null;
    clearMergePreview(); // 裁决被整体重置，预览中的旧裁决信息已过期
    render();
    renderRulesPreview();
    toast("已全部恢复为采用校准分");
  } catch (err) {
    toast("操作失败：" + err.message, true);
  }
});

$("#btn-export").addEventListener("click", () => {
  window.location.href = `/api/sessions/${SESSION_ID}/export`;
});

$("#btn-clear").addEventListener("click", async () => {
  if (!confirm("确定清空当前会话？\n将删除本会话上传的全部评分、裁决与理由（数据仅保存在容器卷中，不可恢复）。")) return;
  try {
    await api(`/api/sessions/${SESSION_ID}`, { method: "DELETE" });
    analysis = null;
    stability = null;
    ruleDraft = null;
    rulePreviewData = null;
    clearMergePreview();
    render();
    renderRulesPreview();
    setMsg("会话已清空。");
    toast("会话已清空");
  } catch (err) {
    toast("清空失败：" + err.message, true);
  }
});

// ---------- Ranking stability experiment actions ----------
stabilityTypeSel.addEventListener("change", syncStabilityControls);

stabilityBtnRun.addEventListener("click", async () => {
  const type = stabilityTypeSel.value;
  const name = stabilityNameSel.value;
  if (!name) {
    setStabilityMsg(`请先选择要排除的${type === "judge" ? "评委" : "项目"}。`, "error");
    return;
  }
  setStabilityMsg("计算中…");
  try {
    stability = await api(`/api/sessions/${SESSION_ID}/stability`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exclude_type: type, exclude_name: name }),
    });
    setStabilityMsg("实验完成（临时结果，未保存）", "ok");
    renderStability();
  } catch (err) {
    stability = null;
    setStabilityMsg(err.message, "error");
    renderStability();
    toast("实验失败：" + err.message, true);
  }
});

stabilityBtnClear.addEventListener("click", () => clearStability());

// ---------- Init ----------
loadAnalysis();
