"use strict";

/* ---------------------------------------------------------------------------
 * State
 * ------------------------------------------------------------------------- */
const state = {
  meta: null,
  rules: {},
  sides: { A: null, B: null },
  keyColumns: { A: 0, B: 0 },
  secondaryFields: [],
  decisions: {},
  probes: [],
  analysis: null,
  activeTab: "matches",
  activeVersion: null,
  versions: [],
  baselineId: "",
  busy: 0,
  sampleProbes: [],
  encodings: { A: "gb18030", B: "cp1254" },
  sources: { A: null, B: null },
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = "toast" + (isError ? " error" : "");
  toast.hidden = false;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { toast.hidden = true; }, isError ? 6000 : 3000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data && data.error) message = data.error;
    } catch (_) { /* not JSON */ }
    throw new Error(message);
  }
  return response.json();
}

async function download(path, body, fallbackName) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data && data.error) message = data.error;
    } catch (_) { /* ignore */ }
    throw new Error(message);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const name = match ? match[1] : fallbackName;
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: name });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* Display helpers --------------------------------------------------------- */

const VISIBLE = {
  invisible: (ch) => /\p{Cf}/u.test(ch),
  space: (ch) => ch === "　" ? "␣" : ch === " " ? "⍽" : ch === " " ? "␠" : null,
  combining: (ch) => /\p{M}/u.test(ch),
};

function glyph(ch) {
  if (ch === "　") return "␣";
  if (ch === " ") return "⍽";
  if (ch === "​") return "∅";
  if (ch === "‌" || ch === "‍") return "∅";
  if (ch === "﻿") return "∅";
  if (ch === "­") return "∅";
  if (ch === "\t") return "⇥";
  if (ch === "\n") return "⏎";
  if (/\p{M}/u.test(ch)) return "◌" + ch;
  if (ch === " ") return "␠";
  return ch;
}

function visibleText(value) {
  if (value === null || value === undefined) return "";
  return Array.from(String(value)).map(glyph).join("");
}

function cpLabel(ch) {
  const cp = ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0");
  return `U+${cp}`;
}

function isCf(ch) {
  return /\p{Cf}/u.test(ch);
}

function shortKey(value, max = 40) {
  const text = visibleText(value);
  return text.length > max ? text.slice(0, max) + "…" : text;
}

function sideName(side) {
  return side === "A" ? "A 侧" : "B 侧";
}

/* ---------------------------------------------------------------------------
 * Bootstrap
 * ------------------------------------------------------------------------- */
async function bootstrap() {
  try {
    state.meta = await api("/api/meta");
    state.rules = { ...state.meta.defaults };
    renderRules();
    renderSideCards();
    renderVersionSelect();
    await refreshVersions();
    await previewRules();
    await refreshAnalysis({ quiet: true });
  } catch (error) {
    showToast("初始化失败：" + error.message, true);
  }
}

/* ---------------------------------------------------------------------------
 * Side cards
 * ------------------------------------------------------------------------- */
function renderSideCards() {
  const container = $("#sides");
  container.innerHTML = "";
  for (const side of ["A", "B"]) {
    container.appendChild(renderSideCard(side));
  }
}

function renderSideCard(side) {
  const data = state.sides[side];
  const sample = state.meta.samples.find((item) => item.id === side);
  const card = el("div", { class: "side-card" });
  const badge = el("span", { class: "side-badge " + side.toLowerCase(), text: side });
  const head = el("div", { class: "side-head" }, [
    el("div", { class: "side-tag" }, [
      badge,
      el("strong", { text: data ? data.label : (side === "A" ? "中国侧数据" : "土耳其侧数据") }),
      data ? el("span", { class: "tag " + (side === "A" ? "info" : "warn"),
        text: data.encoding }) : null,
    ]),
    el("div", { class: "muted small", text: data ? `${data.rows.length} 行 · ${data.columns.length} 列` : "未载入" }),
  ]);

  const body = el("div", { class: "side-body" });

  const fileInput = el("input", {
    type: "file",
    accept: ".csv,.json,.txt",
    onchange: (event) => handleFile(side, event.target.files[0]),
  });
  const uploadLabel = el("label", { class: "upload-btn", for: `file-${side}`,
    text: "选择 CSV / JSON 文件" });
  fileInput.id = `file-${side}`;

  const encodingSelect = el("select", {
    onchange: (event) => {
      const value = event.target.value;
      state.encodings[side] = value;
      const source = state.sources[side];
      if (!source) return;
      if (source.type === "sample") loadSample(side, value, { quiet: true });
      else if (source.type === "file") importBase64(side, source.filename,
        source.base64, value, { quiet: true });
    },
  });
  const encodings = state.meta.encodings.map((item) => item.value);
  if (sample && !encodings.includes(sample.encoding)) encodings.push(sample.encoding);
  for (const value of encodings) {
    encodingSelect.appendChild(el("option", { value, text: value }));
  }
  encodingSelect.value = state.encodings[side] ||
    (data ? data.encoding : (sample ? sample.encoding : "utf-8"));

  const sampleButton = el("button", {
    class: "ghost",
    text: "载入本地样本",
    onclick: () => loadSample(side, encodingSelect.value),
  });

  body.appendChild(el("div", { class: "file-row" }, [
    fileInput, uploadLabel,
    el("label", { class: "inline-field", text: "编码" }, [encodingSelect]),
    sampleButton,
  ]));

  if (data) {
    const columnSelect = el("select", {
      onchange: (event) => {
        state.keyColumns[side] = Number(event.target.value);
        scheduleAnalysis();
      },
    });
    data.columns.forEach((column) => {
      columnSelect.appendChild(el("option", { value: column.index, text: column.name }));
    });
    columnSelect.value = String(state.keyColumns[side] ?? 0);
    body.appendChild(el("div", { class: "file-row" }, [
      el("label", { class: "key-select", text: "关联编码列" }, [columnSelect]),
      el("span", { class: "side-meta",
        text: `文件：${data.filename} · ${data.encoding}` }),
    ]));

    const preview = el("div", { class: "scroll-x" });
    const table = el("table", { class: "grid" });
    const header = el("tr", {}, data.columns.map((column) =>
      el("th", { text: column.name + (column.index === state.keyColumns[side] ? " ·关联" : "") })));
    table.appendChild(el("thead", {}, [header]));
    const tbody = el("tbody");
    for (const row of data.rows.slice(0, 6)) {
      tbody.appendChild(el("tr", {}, data.columns.map((column) =>
        el("td", { class: "raw-cell", title: row.cells[column.index] ?? "",
          text: visibleText(row.cells[column.index] ?? "") }))));
    }
    table.appendChild(tbody);
    preview.appendChild(table);
    body.appendChild(preview);
    if (data.rows.length > 6) {
      body.appendChild(el("div", { class: "muted small",
        text: `仅预览前 6 行，共 ${data.rows.length} 行` }));
    }
    body.appendChild(el("div", { class: "file-row" }, [
      el("button", { class: "ghost", text: "清空该侧",
        onclick: () => { state.sides[side] = null; state.analysis = null;
          state.sources[side] = null; renderSideCards();
          refreshAnalysis({ quiet: true }); } }),
    ]));
  } else {
    body.appendChild(el("div", { class: "side-meta", text: sample ? sample.notes : "" }));
  }

  card.appendChild(head);
  card.appendChild(body);
  return card;
}

async function handleFile(side, file) {
  if (!file) return;
  try {
    const buffer = await file.arrayBuffer();
    const base64 = arrayBufferToBase64(buffer);
    state.sources[side] = { type: "file", filename: file.name, base64 };
    await importBase64(side, file.name, base64,
      state.encodings[side] || "utf-8");
  } catch (error) {
    showToast("导入失败：" + error.message, true);
  }
}

async function importBase64(side, filename, base64, encoding, options = {}) {
  try {
    const result = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ filename, encoding, content_base64: base64 }),
    });
    state.encodings[side] = encoding;
    adoptSide(side, result);
    if (result.replacement_chars > 0) {
      showToast(`按 ${encoding} 解码出现 ${result.replacement_chars} 个替换符，请检查编码选择`, true);
    } else if (!options.quiet) {
      showToast(`已导入 ${filename}（${encoding}，${result.row_count} 行）`);
    }
  } catch (error) {
    showToast("导入失败：" + error.message, true);
  }
}

function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function loadSample(side, encoding, options = {}) {
  try {
    const result = await api("/api/sample/load", {
      method: "POST",
      body: JSON.stringify({ side, encoding }),
    });
    state.sources[side] = { type: "sample" };
    state.encodings[side] = encoding;
    adoptSide(side, result);
    if (result.replacement_chars > 0) {
      showToast(`样本以 ${encoding} 解码出现 ${result.replacement_chars} 个替换符`, true);
    } else if (!options.quiet) {
      showToast(`已载入 ${side} 侧样本（${encoding}）`);
    }
  } catch (error) {
    showToast("载入样本失败：" + error.message, true);
  }
}

function adoptSide(side, result) {
  state.sides[side] = result.side_data;
  state.keyColumns[side] = 0;
  // Suggested secondary fields must exist on both sides.
  if (state.sides.A && state.sides.B) {
    const namesA = state.sides.A.columns.map((column) => column.name);
    const namesB = new Set(state.sides.B.columns.map((column) => column.name));
    state.secondaryFields = state.secondaryFields.filter((name) =>
      namesA.includes(name) && namesB.has(name));
  }
  state.decisions = {};
  state.analysis = null;
  renderSideCards();
  refreshAnalysis();
}

/* ---------------------------------------------------------------------------
 * Rules
 * ------------------------------------------------------------------------- */
function renderRules() {
  const list = $("#rule-list");
  list.innerHTML = "";
  const impact = state.analysis && state.analysis.rule_impact
    ? state.analysis.rule_impact : null;

  for (const rule of state.meta.rules) {
    const selected = state.rules[rule.key];
    const optionMeta = rule.options.find((option) => option.value === selected);
    const isDestructive = Boolean(optionMeta && optionMeta.warning);
    const isDirty = selected !== rule.default;

    const card = el("div", {
      class: "rule-item" + (isDestructive ? " destructive" : "") + (isDirty ? " dirty" : ""),
    }, [
      el("div", { class: "rule-top" }, [
        el("div", {}, [
          el("div", { class: "rule-name", text: rule.label }),
          el("div", { class: "rule-desc", text: rule.description }),
        ]),
        el("div", { class: "rule-impact" }),
      ]),
      el("div", { class: "rule-options" }, rule.options.map((option) => {
        const button = el("button", {
          class: "rule-option"
            + (option.value === selected ? " selected" : "")
            + (option.warning ? " warn" : ""),
          title: option.warning || option.label,
          onclick: () => {
            state.rules[rule.key] = option.value;
            renderRules();
            previewRules();
            scheduleAnalysis();
          },
        }, [
          option.label,
          option.warning ? el("span", { class: "opt-warn", text: " !" }) : null,
        ]);
        return button;
      })),
    ]);

    const impactCell = card.querySelector(".rule-impact");
    if (impact) {
      const counts = ["A", "B"].map((side) => (impact[side] || {})[rule.key] || 0);
      const total = counts[0] + counts[1];
      if (total > 0) {
        impactCell.textContent = `影响 ${total} 个键（A ${counts[0]} / B ${counts[1]}）`;
      }
    } else if (state.preview) {
      const probes = (state.preview.probe_impact || {})[rule.key] || 0;
      if (probes > 0) {
        impactCell.textContent = `预览中改变 ${probes} 个探针`;
      }
    }
    list.appendChild(card);
  }

  const destructive = state.meta.rules.filter((rule) => {
    const option = rule.options.find((item) => item.value === state.rules[rule.key]);
    return option && option.warning;
  });
  const count = $("#destructive-count");
  if (destructive.length) {
    count.innerHTML = `<span class="warn-mark">!</span> ${destructive.length} 项已选用会改变业务含义的规则`;
  } else {
    count.textContent = "当前全部为保守规则";
  }
  updateFingerprint();
}

function updateFingerprint() {
  const pill = $("#version-pill");
  const fp = $("#rule-fingerprint");
  const live = state.preview && state.preview.rules_fingerprint;
  if (state.activeVersion) {
    const synced = live === state.activeVersion.rules_fingerprint;
    pill.textContent = synced
      ? `版本：${state.activeVersion.label}`
      : `版本：${state.activeVersion.label}（有未保存改动）`;
    pill.className = "pill " + (synced ? "accent" : "warn");
    fp.textContent = live ? `当前 ${live.slice(0, 12)}` : "—";
  } else {
    pill.textContent = "未保存版本";
    pill.className = "pill";
    fp.textContent = live ? `当前规则 ${live.slice(0, 12)}` : "规则尚未固化";
  }
  fp.classList.add("mono");
}

async function previewRules() {
  try {
    const payload = {
      rules: state.rules,
      samples: state.sampleProbes,
    };
    const result = await api("/api/rules/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.preview = result;
    renderProbes(result);
    renderRules();
  } catch (error) {
    showToast("规则预览失败：" + error.message, true);
  }
}

function renderProbes(result) {
  const body = $("#probe-body");
  body.innerHTML = "";
  for (const probe of result.probes) {
    const row = el("tr", {}, [
      el("td", {}, [
        el("div", { class: "probe-raw", title: probe.raw, text: shortKey(probe.raw, 22) }),
        el("div", { class: "preview-chips" },
          Array.from(probe.raw).slice(0, 6).map((ch) =>
            el("span", { class: "cp-chip" + (isCf(ch) ? " invisible" : ""),
              title: ch + " · " + cpLabel(ch), text: cpLabel(ch) }))),
      ]),
      el("td", { class: "probe-clean" + (probe.changed ? " changed" : ""),
        title: probe.clean, text: shortKey(probe.clean, 22) || "（空）" }),
      el("td", {}, probe.changed
        ? el("span", { class: "tag ok", text: probe.steps.map((step) => shortRule(step.rule)).join(" → ") })
        : el("span", { class: "muted small", text: "无变化" })),
      el("td", {}, probe.collides_with.length
        ? el("span", { class: "probe-collide", title: probe.collides_with.join("\n"),
          text: "与 " + probe.collides_with.length + " 种写法相同" })
        : el("span", { class: "muted small", text: "—" })),
    ]);
    body.appendChild(row);
  }
  if (!result.probes.length) {
    body.appendChild(el("tr", {}, [el("td", { colspan: 4, class: "empty",
      text: "没有可预览的样本" })]));
  }
}

function shortRule(rule) {
  const names = {
    encoding_invalid: "编码", invisible: "不可见", whitespace: "空白", width: "全半角",
    normalization: "规范化", diacritics: "重音", case: "大小写",
    punctuation: "标点", leading_zeros: "前导零",
  };
  return names[rule] || rule;
}

/* ---------------------------------------------------------------------------
 * Analysis
 * ------------------------------------------------------------------------- */
let analysisTimer = null;
function scheduleAnalysis() {
  clearTimeout(analysisTimer);
  analysisTimer = setTimeout(() => refreshAnalysis(), 260);
}

function buildPayload(extra = {}) {
  return {
    sides: state.sides,
    rules: state.rules,
    key_columns: state.keyColumns,
    secondary_fields: state.secondaryFields,
    decisions: state.decisions,
    ...extra,
  };
}

function bothLoaded() {
  return Boolean(state.sides.A && state.sides.B);
}

async function refreshAnalysis(options = {}) {
  if (!bothLoaded()) {
    state.analysis = null;
    renderResults();
    return;
  }
  state.busy += 1;
  if (!options.quiet) renderResults();
  try {
    const extra = {};
    if (state.baselineId) extra.baseline_version_id = state.baselineId;
    const result = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify(buildPayload(extra)),
    });
    state.analysis = result.analysis;
    pruneDecisions();
    renderRules();
    renderResults();
  } catch (error) {
    showToast("关联计算失败：" + error.message, true);
  } finally {
    state.busy -= 1;
    if (state.busy === 0) renderResults();
  }
}

function pruneDecisions() {
  if (!state.analysis) return;
  const valid = new Set(state.analysis.collisions.map((group) => group.id));
  for (const key of Object.keys(state.decisions)) {
    if (!valid.has(key)) delete state.decisions[key];
  }
}

/* ---------------------------------------------------------------------------
 * Results rendering
 * ------------------------------------------------------------------------- */
function renderResults() {
  renderStats();
  renderTabs();
  renderTabBody();
  const subtitle = $("#results-subtitle");
  if (!bothLoaded()) {
    subtitle.textContent = "载入两侧数据后自动计算。";
  } else if (!state.analysis) {
    subtitle.textContent = "计算中…";
  } else {
    subtitle.textContent =
      `规则指纹 ${state.analysis.rules_fingerprint.slice(0, 12)} · `
      + `A ${state.analysis.summary.rows_a} 行 / B ${state.analysis.summary.rows_b} 行`;
  }
}

function renderStats() {
  const box = $("#stats");
  box.innerHTML = "";
  if (!state.analysis) {
    box.appendChild(el("div", { class: "empty", text: "尚未计算" }));
    return;
  }
  const s = state.analysis.summary;
  const stats = [
    { n: s.confirmed_pairs, l: "确认匹配对数", cls: "ok" },
    { n: s.matched_secondary, l: "经次级字段确认", cls: "ok" },
    { n: s.collisions_pending, l: "碰撞待确认", cls: s.collisions_pending ? "warn" : "" },
    { n: s.ambiguous_groups, l: "重复键待选", cls: s.ambiguous_groups ? "warn" : "" },
    { n: s.ambiguous_pairs, l: "可能匹配组合", cls: "info" },
    { n: s.unmatched_a + s.unmatched_b, l: "未匹配行", cls: "" },
    { n: s.excluded_a + s.excluded_b, l: "排除行", cls: "" },
  ];
  if (state.analysis.diff) {
    const d = state.analysis.diff;
    stats.push({ n: d.added_count, l: "对比新增", cls: "ok" });
    stats.push({ n: d.removed_count, l: "对比失效", cls: "danger" });
    stats.push({ n: d.conflict_count, l: "对比冲突", cls: "warn" });
  }
  for (const stat of stats) {
    box.appendChild(el("div", { class: "stat " + (stat.cls || "") }, [
      el("div", { class: "n", text: String(stat.n) }),
      el("div", { class: "l", text: stat.l }),
    ]));
  }
}

function renderTabs() {
  const counts = state.analysis ? {
    collisions: state.analysis.summary.collisions_pending,
    duplicates: state.analysis.duplicate_groups.length,
    unmatched: state.analysis.summary.unmatched_a + state.analysis.summary.unmatched_b,
    excluded: state.analysis.summary.excluded_a + state.analysis.summary.excluded_b,
    diff: state.analysis.diff
      ? state.analysis.diff.added_count + state.analysis.diff.removed_count
        + state.analysis.diff.conflict_count
      : 0,
  } : {};
  for (const tab of $$(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === state.activeTab);
    const count = counts[tab.dataset.tab];
    let badge = tab.querySelector(".tab-badge");
    if (count && count > 0) {
      if (!badge) {
        badge = el("span", { class: "tab-badge" });
        tab.appendChild(document.createTextNode(" "));
        tab.appendChild(badge);
      }
      badge.textContent = String(count);
    } else if (badge) {
      badge.remove();
    }
  }
}

function renderTabBody() {
  const body = $("#tab-body");
  body.innerHTML = "";
  if (!state.analysis) {
    body.appendChild(el("div", { class: "empty" }, [
      el("b", { text: "载入 A / B 两侧数据后自动关联" }),
      "可以先调整上方规则，预览会实时显示原始键与整理后的键。",
    ]));
    return;
  }
  const renderers = {
    matches: renderMatches,
    collisions: renderCollisions,
    duplicates: renderDuplicates,
    unmatched: renderUnmatched,
    excluded: renderExcluded,
    diff: renderDiff,
  };
  (renderers[state.activeTab] || renderMatches)(body);
}

function rowButton(side, rowNo, label) {
  return el("button", { class: "row-link", text: label || `${side}${rowNo}`,
    onclick: () => openRow(side, rowNo) });
}

function keyCode(value) {
  return el("span", { class: "keycell", title: value, text: shortKey(value, 30) || "（空）" });
}

/* --- matches --- */
function renderMatches(body) {
  const matches = state.analysis.matches;
  if (!matches.length) {
    body.appendChild(el("div", { class: "empty" }, [
      el("b", { text: "暂无可确认的匹配" }),
      "可能存在待确认的碰撞，或两侧键尚未被规则整理成相同形式。",
    ]));
    return;
  }
  const table = el("table", { class: "data" });
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "整理后键" }), el("th", { text: "A 行" }),
    el("th", { text: "A 原始键" }), el("th", { text: "B 行" }),
    el("th", { text: "B 原始键" }), el("th", { text: "确认方式" }),
  ])]));
  const tbody = el("tbody");
  for (const match of matches) {
    tbody.appendChild(el("tr", {}, [
      el("td", {}, [keyCode(match.key)]),
      el("td", {}, [rowButton("A", match.a)]),
      el("td", {}, [keyCode(rawKeyOf("A", match.a))]),
      el("td", {}, [rowButton("B", match.b)]),
      el("td", {}, [keyCode(rawKeyOf("B", match.b))]),
      el("td", {}, [el("span", {
        class: "tag " + (match.mode === "unique" ? "ok" : "info"),
        text: match.mode === "unique" ? "唯一键" : "次级字段加唯一",
      })]),
    ]));
  }
  table.appendChild(tbody);
  body.appendChild(table);
  body.appendChild(el("p", { class: "muted small",
    text: "点击行号可查看原始行、每个清洗步骤和字符码位。" }));
}

/* --- collisions --- */
function renderCollisions(body) {
  const collisions = state.analysis.collisions;
  if (!collisions.length) {
    body.appendChild(el("div", { class: "empty" }, [
      el("b", { text: "没有碰撞" }),
      "没有出现多个不同原始键被整理成同一个键的情况。",
    ]));
    return;
  }
  if (collisions.some((group) => group.decision === "pending")) {
    body.appendChild(el("div", { class: "hint-box warn" }, [
      el("b", { text: "确认处理方式前不会自动一对一关联。 " }),
      "待确认组中涉及的 " + state.analysis.summary.blocked_rows + " 行被阻断，"
      + "显示的是可能匹配数量，不是已匹配结果。",
    ]));
  }
  for (const group of collisions) {
    body.appendChild(renderCollisionCard(group));
  }
}

function renderCollisionCard(group) {
  const resolved = group.decision !== "pending";
  const card = el("div", { class: "collision-card" + (resolved ? " resolved" : "") });

  card.appendChild(el("div", { class: "collision-head" }, [
    el("div", {}, [
      el("div", {}, [
        el("span", { class: "tag " + (group.side === "A" ? "info" : "warn"),
          text: sideName(group.side) }),
        el("span", { class: "muted small", text: "  整理后为 " }),
        el("span", { class: "collision-key", text: visibleText(group.clean_key) }),
      ]),
      el("div", { class: "collision-meta",
        text: `${group.variants.length} 种原始写法 · 涉及本侧 ${group.row_nos.length} 行`
          + (group.counterpart_row_nos.length
            ? ` · 对侧同键 ${group.counterpart_row_nos.length} 行`
            : " · 对侧无同键") }),
    ]),
    el("div", {}, [
      el("span", { class: "tag " + (group.decision === "pending" ? "warn"
        : group.decision === "merge" ? "ok" : "danger"),
        text: group.decision === "pending" ? "待确认"
          : group.decision === "merge" ? "已合并"
          : group.decision === "separate" ? "按原始键区分" : "已排除" }),
    ]),
  ]));

  const variants = el("div", { class: "collision-variants" });
  for (const variant of group.variants) {
    variants.appendChild(el("div", { class: "variant-line" }, [
      el("span", { class: "variant-raw", text: shortKey(variant.raw, 30) }),
      el("span", { class: "tag", text: `${variant.row_count} 行` }),
      el("span", { class: "variant-cp" },
        Array.from(variant.raw).slice(0, 14).map((ch) =>
          el("span", { class: "cp-chip" + (isCf(ch) ? " invisible" : ""),
            title: ch, text: cpLabel(ch) }))),
      el("span", { class: "muted small",
        text: "行号 " + variant.row_nos.join("、") }),
    ]));
  }
  card.appendChild(variants);

  if (group.decision === "pending") {
    card.appendChild(el("div", { class: "hint-box tight" }, [
      "若确认合并，本组 " + group.row_nos.length + " 行 × 对侧 "
      + group.counterpart_row_nos.length + " 行 = ",
      el("b", { text: String(group.possible_matches) }),
      " 种可能配对；需用次级字段进一步缩小，或确认后接受重复。",
    ]));
  } else if (group.decision === "merge") {
    card.appendChild(el("div", { class: "collision-meta",
      text: `合并后与对侧 ${group.counterpart_row_nos.length} 行同键，`
        + `可能配对 ${group.possible_matches} 种；已在下方“重复键”中列明。` }));
  }

  const actions = el("div", { class: "collision-actions" });
  const options = [
    { value: "merge", label: "视为同一键（合并）", cls: "" },
    { value: "separate", label: "按原始键区分", cls: "" },
    { value: "skip", label: "排除这些行", cls: "danger" },
  ];
  for (const option of options) {
    actions.appendChild(el("button", {
      class: "ghost " + option.cls + (group.decision === option.value ? " selected" : ""),
      text: option.label,
      onclick: () => setDecision(group.id, option.value),
    }));
  }
  if (resolved) {
    actions.appendChild(el("button", {
      class: "ghost", text: "改回待确认",
      onclick: () => setDecision(group.id, null),
    }));
  }
  card.appendChild(actions);
  return card;
}

function setDecision(collisionId, decision) {
  if (decision === null) delete state.decisions[collisionId];
  else state.decisions[collisionId] = decision;
  if (state.analysis) {
    // optimistic local update so buttons feel immediate
    const group = state.analysis.collisions.find((item) => item.id === collisionId);
    if (group && decision) group.decision = decision;
  }
  renderResults();
  refreshAnalysis({ quiet: true });
}

/* --- duplicates --- */
function renderDuplicates(body) {
  const groups = state.analysis.duplicate_groups;
  const controls = el("div", { class: "hint-box" });
  controls.appendChild(el("b", {
    text: "两侧都存在重复键：先选业务上的次级字段，再看可能匹配数量。" }));
  const controlRow = el("div", {
    class: "control-row" });
  controlRow.appendChild(renderSecondaryControl());
  controls.appendChild(controlRow);
  body.appendChild(controls);

  if (!groups.length) {
    body.appendChild(el("div", { class: "empty" }, [
      el("b", { text: "没有双侧重复的键" }),
      "每个键在两侧最多只有一行，或差异仅来自待确认的碰撞。",
    ]));
    return;
  }

  for (const group of groups) {
    body.appendChild(renderDuplicateCard(group));
  }
}

function renderSecondaryControl() {
  const available = commonColumns();
  const wrap = el("div", { class: "file-row" });
  for (const name of available) {
    const active = state.secondaryFields.includes(name);
    wrap.appendChild(el("button", {
      class: "rule-option" + (active ? " selected" : ""),
      text: name,
      onclick: () => {
        if (active) {
          state.secondaryFields = state.secondaryFields.filter((item) => item !== name);
        } else {
          if (state.secondaryFields.length >= 2) {
            showToast("最多选择 2 个次级字段", true);
            return;
          }
          state.secondaryFields = [...state.secondaryFields, name];
        }
        refreshAnalysis();
      },
    }));
  }
  if (state.secondaryFields.length) {
    wrap.appendChild(el("span", { class: "muted small",
      text: "已选：" + state.secondaryFields.join(" + ") + "（规则同样应用于次级字段）" }));
  } else {
    wrap.appendChild(el("span", { class: "muted small",
      text: "未选次级字段时只按键匹配；重复键会保持待定。" }));
  }
  return wrap;
}

function commonColumns() {
  if (!state.sides.A || !state.sides.B) return [];
  const namesA = state.sides.A.columns.map((column) => column.name);
  const namesB = new Set(state.sides.B.columns.map((column) => column.name));
  return namesA.filter((name) => namesB.has(name));
}

function renderDuplicateCard(group) {
  const card = el("div", { class: "dup-card" });
  const head = el("div", { class: "dup-head" }, [
    el("div", {}, [
      el("div", {}, [
        el("span", { class: "muted small", text: "键 " }),
        el("span", { class: "collision-key", text: visibleText(group.key) }),
        el("span", { class: "tag " + (group.key_kind === "raw" ? "warn" : "info"),
          text: group.key_kind === "raw" ? "按原始键区分" : "整理后的键" }),
      ]),
      el("div", { class: "collision-meta",
        text: `A 侧 ${group.a_count} 行 · B 侧 ${group.b_count} 行`
          + (group.secondary_fields.length
            ? ` · 次级字段 ${group.secondary_fields.join(" + ")}`
            : " · 未选次级字段") }),
    ]),
    el("div", { class: "dup-count",
      text: `${group.possible} 种可能配对` }),
  ]);
  card.appendChild(head);

  if (group.status === "matched") {
    card.appendChild(el("div", { class: "hint-box mt8" }, [
      el("span", { class: "tag ok", text: "已确定" }),
      ` 通过${group.mode === "secondary" ? "次级字段" : "唯一键"}确认 1 对；`
      + `其余 ${group.possible - 1} 种组合已被排除。`,
    ]));
  } else {
    card.appendChild(el("div", { class: "hint-box warn mt8" }, [
      el("span", { class: "tag warn", text: "待定" }),
      group.reason === "secondary_no_match"
        ? " 所选次级字段的整理值没有任何一对相等，请调整规则或字段。"
        : " 存在多对次级字段相等的组合，不能自动一一关联；请改选次级字段或调整规则。",
      el("div", { class: "muted small mt4",
        text: `完全相同的组合有 ${group.secondary_matches ?? "—"} 对。` }),
    ]));
  }

  const fields = group.secondary_fields;
  if (!group.candidates || !group.candidates.length) {
    if (group.status === "matched" && group.chosen) {
      card.appendChild(el("div", { class: "file-row mt6" }, [
        el("span", { class: "muted small", text: "确认配对：" }),
        rowButton("A", group.chosen.a), el("span", { class: "muted small", text: "↔" }),
        rowButton("B", group.chosen.b),
      ]));
    }
    return card;
  }
  const table = el("table", { class: "data pair-table" });
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "A 行" }), el("th", { text: "A 原始键" }),
    ...fields.map((field) => el("th", { text: field + "（A）" })),
    el("th", { text: "B 行" }), el("th", { text: "B 原始键" }),
    ...fields.map((field) => el("th", { text: field + "（B）" })),
    el("th", { text: "次级字段" }),
  ])]));
  const tbody = el("tbody");
  for (const pair of group.candidates) {
    const same = fields.length > 0 && fields.every((field) =>
      pair.secondary[field].a === pair.secondary[field].b);
    const row = el("tr", {}, [
      el("td", {}, [rowButton("A", pair.a)]),
      el("td", { class: "keycell", text: shortKey(rawKeyOf("A", pair.a), 22) }),
      ...fields.map((field) => el("td", { class: "keycell",
        text: visibleText(pair.secondary[field].a) })),
      el("td", {}, [rowButton("B", pair.b)]),
      el("td", { class: "keycell", text: shortKey(rawKeyOf("B", pair.b), 22) }),
      ...fields.map((field) => el("td", { class: "keycell",
        text: visibleText(pair.secondary[field].b) })),
      el("td", {}, fields.length
        ? el("span", { class: "tag " + (same ? "ok" : "danger"),
          text: same ? "全部相等" : "不相等" })
        : el("span", { class: "muted small", text: "未选" })),
    ]);
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  card.appendChild(table);
  if (group.candidates_truncated) {
    card.appendChild(el("p", { class: "muted small",
      text: `仅显示前 ${group.candidates.length} 种组合，完整列表见导出的 CSV。` }));
  }
  return card;
}

function rawKeyOf(side, rowNo) {
  const data = state.analysis && state.analysis.sides[side];
  if (!data) return "";
  const row = data.rows.find((item) => item.row_no === rowNo);
  return row ? row.raw_key : "";
}

/* --- unmatched / excluded --- */
function renderUnmatched(body) {
  renderRowList(body, ["unmatched"], "未匹配", "这些行在另一侧没有相同整理键。");
}

function renderExcluded(body) {
  renderRowList(body, ["excluded"], "已排除的行", "空白键、编码损坏或按碰撞决定排除的行。");
}

function renderRowList(body, kinds, emptyTitle, emptyText) {
  const groups = kinds.map((kind) => [kind, state.analysis[kind]]);
  let total = 0;
  for (const [, data] of groups) total += data.A.length + data.B.length;
  if (!total) {
    body.appendChild(el("div", { class: "empty" }, [
      el("b", { text: emptyTitle + "：无" }), emptyText,
    ]));
    return;
  }
  const table = el("table", { class: "data" });
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "侧" }), el("th", { text: "行号" }),
    el("th", { text: "原始键" }), el("th", { text: "整理后键" }),
    el("th", { text: "说明" }),
  ])]));
  const tbody = el("tbody");
  for (const [kind, data] of groups) {
    for (const side of ["A", "B"]) {
      for (const item of data[side]) {
        tbody.appendChild(el("tr", {}, [
          el("td", {}, [el("span", { class: "tag " + (side === "A" ? "info" : "warn"),
            text: sideName(side) })]),
          el("td", {}, [rowButton(side, item.row_no)]),
          el("td", { class: "keycell", title: item.raw_key,
            text: shortKey(item.raw_key, 26) || "（空）" }),
          el("td", { class: "keycell", title: item.clean_key,
            text: shortKey(item.clean_key, 26) || "（空）" }),
          el("td", { class: "muted small", text: describeReason(item) }),
        ]));
      }
    }
  }
  table.appendChild(tbody);
  body.appendChild(table);
}

function describeReason(item) {
  const reasons = {
    blank: "整理后为空键",
    encoding_invalid: "编码解码出现替换符",
    collision_skip: "所属碰撞组被排除",
    other_side_missing: "对侧没有该键",
    collision_pending: "碰撞待确认，已阻断",
    counterpart_collision_pending: "对侧碰撞待确认，已阻断",
    no_counterpart: "没有对应行",
    excluded: "已排除",
  };
  return reasons[item.exclude_reason] || reasons[item.reason] || item.reason
    || item.exclude_reason || "—";
}

/* --- diff --- */
function renderDiff(body) {
  const diff = state.analysis.diff;
  if (!diff) {
    body.appendChild(el("div", { class: "empty" }, [
      el("b", { text: "未选择对比基线" }),
      "在上方“对比基线”选择已保存的版本，即可看到新增、失效和冲突。",
    ]));
    return;
  }
  if (diff.data_changed) {
    body.appendChild(el("div", { class: "hint-box warn",
      text: "两侧源数据与基线版本不同，差异中可能混入数据本身的变化。" }));
  }
  const blocks = [
    ["added", "新增匹配", "ok"],
    ["removed", "失效匹配", "danger"],
    ["conflict", "变为冲突（基线匹配，现被阻断或歧义）", "warn"],
  ];
  for (const [kind, title, cls] of blocks) {
    const records = diff[kind];
    const group = el("div", { class: "diff-group" }, [
      el("h3", {}, [
        el("span", { class: "tag " + cls, text: String(records.length) }),
        title,
      ]),
    ]);
    if (!records.length) {
      group.appendChild(el("p", { class: "muted small", text: "无" }));
    } else {
      const table = el("table", { class: "data" });
      table.appendChild(el("thead", {}, [el("tr", {}, [
        el("th", { text: "A 行" }), el("th", { text: "B 行" }),
        el("th", { text: "键" }), el("th", { text: "说明" }),
      ])]));
      const tbody = el("tbody");
      for (const record of records) {
        tbody.appendChild(el("tr", {}, [
          el("td", {}, [rowButton("A", record.a)]),
          el("td", {}, [rowButton("B", record.b)]),
          el("td", { class: "keycell", text: shortKey(record.key, 26) }),
          el("td", { class: "muted small",
            text: record.status === "conflict" ? "现处于碰撞或重复歧义"
              : record.status === "removed" ? "基线匹配已不再成立"
              : record.mode === "secondary" ? "经次级字段确认" : "唯一键匹配" }),
        ]));
      }
      table.appendChild(tbody);
      group.appendChild(table);
    }
    body.appendChild(group);
  }
  body.appendChild(el("p", { class: "muted small",
    text: `基线：${diff.baseline_label}（${diff.baseline_created_at}）`
      + ` · 规则指纹 ${String(diff.baseline_fingerprint).slice(0, 12)}`
      + ` · 未变 ${diff.unchanged_count} 对` }));
}

/* ---------------------------------------------------------------------------
 * Drawer: row provenance
 * ------------------------------------------------------------------------- */
function openRow(side, rowNo) {
  if (!state.analysis || !side) return;
  const data = state.analysis.sides[side];
  const row = data.rows.find((item) => item.row_no === rowNo);
  if (!row) return;
  $("#drawer-title").textContent = `${sideName(side)} 第 ${rowNo} 行 · ${data.filename}`;
  const body = $("#drawer-body");
  body.innerHTML = "";

  const cellGrid = el("div", { class: "kv" });
  data.columns.forEach((column) => {
    const value = row.cells[column.index] ?? "";
    const isKey = column.index === state.analysis.config.key_columns[side];
    cellGrid.appendChild(el("div", { class: "k",
      text: column.name + (isKey ? "（关联键）" : "") }));
    cellGrid.appendChild(el("div", { class: "v", text: visibleText(value) || "（空）" }));
  });
  body.appendChild(el("div", { class: "drawer-section" }, [
    el("h4", { text: "原始行" }), cellGrid,
  ]));

  body.appendChild(el("div", { class: "drawer-section" }, [
    el("h4", { text: "关联键" }),
    el("div", { class: "kv" }, [
      el("div", { class: "k", text: "原始键" }),
      el("div", { class: "v", text: visibleText(row.raw_key) || "（空）" }),
      el("div", { class: "k", text: "整理后键" }),
      el("div", { class: "v", text: visibleText(row.clean_key) || "（空）" }),
      el("div", { class: "k", text: "状态" }),
      el("div", { class: "v", text: statusLabel(row) }),
    ]),
  ]));

  body.appendChild(el("div", { class: "drawer-section" }, [
    el("h4", { text: "字符码位（不可见字符以虚线高亮）" }),
    el("div", { class: "cp-list" },
      Array.from(row.raw_key).map((ch) => el("span", {
        class: "cp-item" + (isCf(ch) ? " invisible" : ""),
        title: ch, text: cpLabel(ch),
      })).concat(row.raw_key ? [] : [el("span", { class: "cp-item missing",
        text: "（空）" })])),
  ]));

  const steps = el("div", { class: "steps" });
  if (!row.steps.length) {
    steps.appendChild(el("p", { class: "muted small", text: "没有任何规则改动此键。" }));
  }
  for (const step of row.steps) {
    steps.appendChild(el("div", { class: "step" }, [
      el("div", { class: "rule-name", text: shortRule(step.rule) }),
      el("div", {}, [
        el("span", { class: "muted small", text: "前：" }),
        el("span", { class: "mono", text: visibleText(step.before) || "（空）" }),
      ]),
      el("div", {}, [
        el("span", { class: "muted small", text: "后：" }),
        el("span", { class: "mono", text: visibleText(step.after) || "（空）" }),
      ]),
      step.detail ? el("div", { class: "muted small", text: step.detail }) : null,
    ]));
  }
  body.appendChild(el("div", { class: "drawer-section" }, [
    el("h4", { text: "应用的规则" }), steps,
  ]));

  const secondaryKeys = Object.keys(row.secondary || {});
  if (secondaryKeys.length) {
    const sec = el("div", { class: "kv" });
    for (const field of secondaryKeys) {
      const value = row.secondary[field];
      sec.appendChild(el("div", { class: "k", text: field }));
      sec.appendChild(el("div", { class: "v",
        text: `${visibleText(value.raw) || "（空）"} → ${visibleText(value.clean) || "（空）"}` }));
    }
    body.appendChild(el("div", { class: "drawer-section" }, [
      el("h4", { text: "次级字段" }), sec,
    ]));
  }

  if (row.partners && row.partners.length) {
    const partners = el("div", { class: "file-row" });
    for (const partnerNo of row.partners.slice(0, 12)) {
      const other = side === "A" ? "B" : "A";
      partners.appendChild(rowButton(other, partnerNo,
        `${other}${partnerNo}`));
    }
    body.appendChild(el("div", { class: "drawer-section" }, [
      el("h4", { text: `可能 / 已确认的对侧行（${row.partner_count}）` }),
      partners,
      row.possible > 1
        ? el("p", { class: "muted small mt6",
          text: `该键可能产生 ${row.possible} 种配对，尚未自动确定。` })
        : null,
    ]));
  }

  body.appendChild(el("div", { class: "drawer-section" }, [
    el("h4", { text: "规则指纹" }),
    el("div", { class: "mono small", text: state.analysis.rules_fingerprint }),
  ]));

  $("#drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
}

function statusLabel(row) {
  const labels = {
    matched_unique: "唯一匹配",
    matched_secondary: "次级字段确认",
    ambiguous: "重复键待选",
    blocked: "碰撞待确认（已阻断）",
    unmatched: "未匹配",
    excluded: "已排除",
  };
  return labels[row.status] || row.status;
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
}

/* ---------------------------------------------------------------------------
 * Versions
 * ------------------------------------------------------------------------- */
async function refreshVersions() {
  try {
    const result = await api("/api/versions");
    state.versions = result.versions;
    renderVersionList();
    renderVersionSelect();
  } catch (error) {
    showToast("版本列表加载失败：" + error.message, true);
  }
}

function renderVersionList() {
  const list = $("#version-list");
  list.innerHTML = "";
  if (!state.versions.length) {
    list.appendChild(el("div", { class: "empty" }, [
      el("b", { text: "还没有保存的规则版本" }),
      "填写名称后点击“保存为版本”；之后修改规则即可对比新增、失效与冲突。",
    ]));
    return;
  }
  for (const version of state.versions) {
    const current = state.activeVersion && state.activeVersion.id === version.id;
    const item = el("div", { class: "version-item" + (current ? " current" : "") }, [
      el("div", {}, [
        el("div", { class: "vtitle", text: version.label }),
        el("div", { class: "vmeta",
          text: `${version.created_at} · 规则指纹 ${version.rules_fingerprint.slice(0, 10)}`
            + ` · 运行快照 ${version.run_count}` }),
        version.note ? el("div", { class: "vmeta", text: version.note }) : null,
      ]),
      el("div", { class: "version-actions" }, [
        el("button", { class: "ghost", text: "载入规则",
          onclick: () => applyVersion(version) }),
        el("button", { class: "ghost", text: "设为对比基线",
          onclick: () => {
            state.baselineId = version.id;
            $("#baseline-select").value = version.id;
            refreshAnalysis();
          } }),
        el("button", { class: "ghost", text: "删除",
          onclick: () => deleteVersion(version) }),
      ]),
    ]);
    list.appendChild(item);
  }
}

function renderVersionSelect() {
  const select = $("#baseline-select");
  const previous = state.baselineId;
  select.innerHTML = "";
  select.appendChild(el("option", { value: "", text: "（不对比）" }));
  for (const version of state.versions) {
    select.appendChild(el("option", { value: version.id, text: version.label }));
  }
  select.value = previous || "";
}

function applyVersion(version) {
  state.rules = { ...version.rules };
  state.keyColumns = { ...state.keyColumns, ...version.config.key_columns };
  state.secondaryFields = [...(version.config.secondary_fields || [])];
  state.decisions = { ...(version.decisions || {}) };
  state.activeVersion = version;
  renderRules();
  renderSideCards();
  previewRules();
  refreshAnalysis();
  showToast(`已载入版本「${version.label}」的规则`);
}

async function deleteVersion(version) {
  if (!window.confirm(`删除版本「${version.label}」及其运行快照？`)) return;
  try {
    await fetch(`/api/versions/${version.id}`, { method: "DELETE" });
    if (state.baselineId === version.id) state.baselineId = "";
    if (state.activeVersion && state.activeVersion.id === version.id) {
      state.activeVersion = null;
    }
    await refreshVersions();
    refreshAnalysis();
  } catch (error) {
    showToast("删除失败：" + error.message, true);
  }
}

async function saveVersion() {
  if (!bothLoaded()) {
    showToast("请先载入两侧数据", true);
    return;
  }
  const label = $("#version-label").value.trim();
  if (!label) {
    showToast("请填写版本名称", true);
    return;
  }
  try {
    const result = await api("/api/versions", {
      method: "POST",
      body: JSON.stringify(buildPayload({ label, note: $("#version-note").value.trim() })),
    });
    state.activeVersion = result.version;
    $("#version-label").value = "";
    $("#version-note").value = "";
    await refreshVersions();
    renderRules();
    showToast(`已保存版本「${result.version.label}」`);
  } catch (error) {
    showToast("保存失败：" + error.message, true);
  }
}

async function saveRun() {
  if (!bothLoaded()) {
    showToast("请先载入两侧数据", true);
    return;
  }
  try {
    const extra = {};
    if (state.activeVersion) extra.version_id = state.activeVersion.id;
    else extra.label = $("#version-label").value.trim() || "运行快照";
    const result = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify(buildPayload(extra)),
    });
    showToast(`已保存运行快照 ${result.run_id}（版本 ${result.version_id}）`);
    await refreshVersions();
  } catch (error) {
    showToast("保存运行失败：" + error.message, true);
  }
}

/* ---------------------------------------------------------------------------
 * Events
 * ------------------------------------------------------------------------- */
function bindEvents() {
  $("#load-samples").addEventListener("click", async () => {
    await loadSample("A", "gb18030");
    await loadSample("B", "cp1254");
  });

  $("#reset-rules").addEventListener("click", () => {
    state.rules = { ...state.meta.defaults };
    state.decisions = {};
    renderRules();
    previewRules();
    refreshAnalysis();
  });

  $("#probe-add").addEventListener("click", addProbe);
  $("#probe-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addProbe(); }
  });

  $("#tabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (!tab) return;
    state.activeTab = tab.dataset.tab;
    renderTabs();
    renderTabBody();
  });

  $("#baseline-select").addEventListener("change", (event) => {
    state.baselineId = event.target.value;
    if (state.baselineId) state.activeTab = "diff";
    refreshAnalysis();
  });

  $("#export-csv").addEventListener("click", async () => {
    if (!bothLoaded()) { showToast("请先载入两侧数据", true); return; }
    try {
      const extra = {};
      if (state.activeVersion) extra.version_id = state.activeVersion.id;
      if (state.baselineId) extra.baseline_version_id = state.baselineId;
      await download("/api/export/csv", buildPayload(extra), "linkage.csv");
    } catch (error) {
      showToast("导出失败：" + error.message, true);
    }
  });

  $("#export-rules").addEventListener("click", async () => {
    if (!bothLoaded()) { showToast("请先载入两侧数据", true); return; }
    try {
      const extra = {};
      if (state.activeVersion) extra.version_id = state.activeVersion.id;
      await download("/api/export/rules", buildPayload(extra), "linkage_rules.json");
    } catch (error) {
      showToast("导出失败：" + error.message, true);
    }
  });

  $("#save-version").addEventListener("click", saveVersion);
  $("#save-run").addEventListener("click", saveRun);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });
}

function addProbe() {
  const input = $("#probe-input");
  const value = input.value;
  if (!value) return;
  state.sampleProbes.push(value);
  if (state.sampleProbes.length > 20) state.sampleProbes.shift();
  input.value = "";
  previewRules();
}

bindEvents();
bootstrap();
