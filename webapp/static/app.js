// 产线布局投影面积估算 —— 前端。只负责上传、轮询与展示；所有计算在后端。
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PARAM_KEYS = [
    "labor_area_basis", "fitter_hours_per_m2", "electrician_hours_per_m2", "packaging_factor",
    "floor_utilisation_low", "floor_utilisation_high",
    "local_charges_usd_per_box", "insurance_pct_of_freight",
    "route", "flat_rack_rate_usd",
  ];
  // 旧任务保存的参数（报告中照原样显示）：只有一个公摊工时系数；面积误差系数；框架箱运价倍数；
  // 超限设备比例（表单已去掉：框架箱 / 开顶箱由人工按线填柜数，比例取默认 0）
  const LEGACY_PARAM_KEYS = ["shared_hours_per_m2", "footprint_uncertainty_low", "footprint_uncertainty_high",
    "flat_rack_multiplier", "oversize_share"];
  // 汇总表行：[key, 格式, 是否关键行, 合计列是否显示]；结果里没有的行不显示
  const SUMMARY_ROWS = [
    ["station_count", "int", false, true],
    ["baseline_m2", "area", true, true],
    ["fine_m2", "area", true, true],
    ["fine_vs_baseline", "pct", false, true],
    ["labor_hours_baseline", "hours", false, true],
    ["labor_hours_fine", "hours", false, true],
    ["labor_hours_fitter", "hours", false, true],
    ["labor_hours_electrician", "hours", false, true],
    ["labor_hours", "hours", true, true],
    // 旧任务（公摊工时只按一个系数算）
    ["hours_baseline", "hours", false, true],
    ["hours_fine", "hours", true, true],
    ["packed_m2", "area", false, true],
    ["boxes_low", "int", false, true],
    ["boxes_mid", "int", true, true],
    ["boxes_high", "int", false, true],
    ["frame_boxes_mid", "int", false, true],
    ["cost_per_box_usd", "usd", false, false],
    ["cost_per_frame_usd", "usd", false, false],
    ["freight_low_usd", "usd", false, true],
    ["freight_mid_usd", "usd", true, true],
    ["freight_high_usd", "usd", false, true],
  ];
  const POLL_MS = 1500;

  // 工作流程图：与代码的实际执行顺序一致（jobs.run_job → estimate_footprint.measure → build_workbook → estimate）
  // where：在哪里运行；cost：决定服务器规格（费用）的一步
  const FLOW = [
    { stage: "input", steps: [
      { k: "in1", where: "browser", tools: "HTML · JavaScript · Web Crypto (SHA-256)" },
      { k: "in2", where: "you", critical: true },
      { k: "in3", where: "you" },
      { k: "in4", where: "server", tools: "Python · FastAPI · Uvicorn" },
    ] },
    { stage: "core", steps: [
      { k: "c1", where: "server", tools: "PyMuPDF" },
      { k: "c2", where: "server", tools: "PyMuPDF" },
      { k: "c3", where: "server", tools: "estimate_footprint.py" },
      { k: "c4", where: "server", tools: "NumPy · SciPy · OpenCV (Hough)" },
      { k: "c5", where: "server", tools: "NumPy · SciPy · OpenCV · Pillow", cost: true },
      { k: "c6", where: "server", tools: "NumPy" },
      { k: "c7", where: "server", tools: "Tesseract · pytesseract", ocr: true },
      { k: "c8", where: "server", tools: "PyMuPDF · Pillow" },
    ] },
    { stage: "output", steps: [
      { k: "o1", where: "server", tools: "JSON · PNG", storage: true },
      { k: "o2", where: "server", tools: "build_workbook.py · openpyxl" },
      { k: "o3", where: "browser", tools: "estimate.py · FastAPI → JavaScript" },
    ] },
  ];

  // rows: [{file: File|null, kind: "pdf"|"dxf", sha256, known, hashing, meta, filename, size, name, ref, mpp, unit}]
  // 浏览器里算指纹要把整个文件读进内存；更大的文件直接上传，服务器收到时同样按内容识别同一文件
  const HASH_MAX_BYTES = 512 * 1024 * 1024;
  const state = { lang: "zh", meta: null, system: null, rows: [], addNote: null, job: null, library: [], pollTimer: null, pollFailures: 0 };

  // ------------------------------------------------------------ 文字与格式
  function t(key, vars) {
    const dict = window.I18N[state.lang] || window.I18N.zh;
    let s = dict[key] ?? window.I18N.zh[key] ?? key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
    return s;
  }
  const say = (message) => (message ? (state.lang === "zh" ? message.zh : message.en) : "");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const locale = () => (state.lang === "zh" ? "zh-CN" : "en-US");
  function num(v, digits = 1) {
    if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "–";
    return Number(v).toLocaleString(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);
  const usd = (v) => (v === null || v === undefined ? "–" : `US$ ${num(v, 0)}`);
  const mb = (bytes) => `${(bytes / 1048576).toFixed(1)} MB`;
  function fmt(v, kind) {
    switch (kind) {
      case "int": return num(v, 0);
      case "area": return num(v, 1);
      case "hours": return num(v, 0);
      case "pct": return pct(v);
      case "usd": return usd(v);
      default: return esc(v);
    }
  }
  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(locale(), { dateStyle: "short", timeStyle: "short" });
  }
  const isBlank = (v) => v === null || v === undefined || String(v).trim() === "";
  // 文件类型：界面上按扩展名区分要填的内容（PDF 填标注尺寸，DXF 选单位）；服务器按文件内容再判断一次
  const kindOf = (f) => (/\.dxf$/i.test(f.name) ? "dxf" : f.type === "application/pdf" || /\.pdf$/i.test(f.name) ? "pdf" : null);
  const maxBytes = (kind) => (state.meta ? (kind === "dxf" ? state.meta.limits.max_dxf_mb : state.meta.limits.max_file_mb) : Infinity) * 1048576;
  // DXF 单位：每个图纸单位等于多少 mm -> 显示文字
  function unitLabel(mm) {
    const key = Object.keys((state.meta && state.meta.dxf_units) || {}).find((k) => state.meta.dxf_units[k] === Number(mm));
    return key ? t(`unit.${key}`) : `1 = ${mm} mm`;
  }
  function lengthText(mm) {
    if (mm >= 1e6) return `${num(mm / 1e6, 1)} km`;
    if (mm >= 1000) return `${num(mm / 1000, 1)} m`;
    return `${num(mm, 0)} mm`;
  }
  // 桌面版（pywebview 窗口）提供的本机接口；普通浏览器里为 null
  const desktopApi = () => (window.pywebview && window.pywebview.api) || null;

  async function desktopSave(call) {
    const out = $("reportError");
    try {
      const r = await call();
      out.classList.toggle("ok", !!(r && r.ok));
      out.textContent = r && r.ok ? t("desktop.saved", { path: r.path }) : (r && r.cancelled ? "" : t("desktop.saveFailed"));
    } catch (_) {
      out.classList.remove("ok");
      out.textContent = t("desktop.saveFailed");
    }
  }

  function storedLang() {
    try { return localStorage.getItem("layoutEstimator.lang"); } catch (_) { return null; }
  }
  function storeLang(lang) {
    try { localStorage.setItem("layoutEstimator.lang", lang); } catch (_) { /* 无痕模式等：忽略 */ }
  }

  // ------------------------------------------------------------ 语言切换
  function applyI18n() {
    document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
    document.title = t("app.title");
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-aria]").forEach((el) => el.setAttribute("aria-label", t(el.dataset.i18nAria)));
    document.querySelectorAll("[data-i18n-title]").forEach((el) => el.setAttribute("title", t(el.dataset.i18nTitle)));
    document.querySelectorAll(".langswitch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang)));
    renderOcrPill();
    if (state.meta) {
      buildParamGrid($("paramGrid"), readParams($("paramGrid")));
      if (state.job) { buildParamGrid($("recalcGrid"), readParams($("recalcGrid"))); buildFrameGrid(state.job, readFrameBoxes()); }
    }
    renderRows();
    renderLibrary();
    renderSystem();
    if (state.job) renderJob(state.job);
    loadRecent();
  }

  function setLang(lang) {
    state.lang = lang === "en" ? "en" : "zh";
    storeLang(state.lang);
    applyI18n();
  }

  // ------------------------------------------------------------ 服务器信息与参数
  function renderOcrPill() {
    const pill = $("ocrPill");
    if (!state.meta) { pill.hidden = true; return; }
    pill.hidden = false;
    pill.className = "pill " + (state.meta.ocr_available ? "on" : "off");
    pill.textContent = t(state.meta.ocr_available ? "ocr.on" : "ocr.off");
  }

  function buildParamGrid(grid, values) {
    const v = Object.assign({}, state.meta.params, values || {});
    if (values && !("flat_rack_rate_usd" in values)) {   // 旧任务存的是倍数：换算成该航线的框架箱运价
      const r = state.meta.routes.find((x) => x.name === v.route);
      if (r) v.flat_rack_rate_usd = "flat_rack_multiplier" in values ? r.usd_per_40ft * values.flat_rack_multiplier : r.flat_rack_usd;
    }
    grid.textContent = "";
    for (const key of PARAM_KEYS) {
      const label = document.createElement("label");
      const span = document.createElement("span");
      span.textContent = t(`params.${key}`);
      label.appendChild(span);
      let input;
      if (key === "route") {
        input = document.createElement("select");
        for (const r of state.meta.routes) {
          const o = document.createElement("option");
          o.value = r.name;
          o.textContent = `${r.name}（US$ ${num(r.usd_per_40ft, 0)}/40HQ）`;
          input.appendChild(o);
        }
        input.value = v.route;
      } else if (key === "labor_area_basis") {
        input = document.createElement("select");
        for (const b of state.meta.labor_area_bases) {
          const o = document.createElement("option");
          o.value = b;
          o.textContent = t(`basis.${b}`);
          input.appendChild(o);
        }
        input.value = v.labor_area_basis;
      } else {
        input = document.createElement("input");
        input.type = "number"; input.step = PARAM_STEPS[key] || "any"; input.min = "0"; input.inputMode = "decimal";
        input.value = v[key];
      }
      input.name = key;
      const range = (state.meta.param_ranges || {})[key];
      if (range) { input.min = range[0]; input.max = range[1]; }
      label.appendChild(input);
      // 每个参数下面写清楚：含义、影响什么、怎么填；再一行显示默认值 / 是否改过 / 是否超出范围
      const help = document.createElement("small");
      help.className = "param-help";
      help.textContent = t(`help.${key}`);
      const meta = document.createElement("small");
      meta.className = "param-meta";
      label.append(help, meta);
      grid.appendChild(label);
    }
    // 框架箱运价按航线取默认值：换航线时填入该航线的默认运价（仍可直接改）
    const route = grid.querySelector("select[name=route]"), flat = grid.querySelector("input[name=flat_rack_rate_usd]");
    route.addEventListener("change", () => {
      const r = state.meta.routes.find((x) => x.name === route.value);
      if (r) flat.value = r.flat_rack_usd;
    });
    grid.addEventListener("input", () => checkParams(grid));
    grid.addEventListener("change", () => checkParams(grid));
    checkParams(grid);
  }

  const SHARE_KEYS = ["floor_utilisation_low", "floor_utilisation_high", "oversize_share", "insurance_pct_of_freight"];
  // 上下箭头每次改多少：比例每次 5 个百分点（保险 0.5 个），金额每次 100——不会一下从 0 跳到 1（= 100%）
  const PARAM_STEPS = {
    fitter_hours_per_m2: 0.01, electrician_hours_per_m2: 0.01, packaging_factor: 0.05,
    floor_utilisation_low: 0.05, floor_utilisation_high: 0.05, oversize_share: 0.05, insurance_pct_of_freight: 0.005,
    local_charges_usd_per_box: 100, flat_rack_rate_usd: 100,
  };
  const USD_KEYS = ["local_charges_usd_per_box", "flat_rack_rate_usd"];

  function paramDefault(grid, key) {
    if (key !== "flat_rack_rate_usd") return state.meta.params[key];
    const r = state.meta.routes.find((x) => x.name === grid.querySelector("select[name=route]").value);
    return r ? r.flat_rack_usd : state.meta.params[key];
  }

  function paramText(key, v) {
    if (key === "route") return v;
    if (key === "labor_area_basis") return t(`basis.${v}`);
    if (USD_KEYS.includes(key)) return usd(Number(v));
    if (SHARE_KEYS.includes(key)) return `${v}（= ${num(Number(v) * 100, 0)}%）`;
    return String(v);
  }

  // 标出改过的参数（黄色）和填错的参数（红色），避免误改；最终仍以后端校验为准
  function checkParams(grid) {
    const val = (name) => grid.querySelector(`[name=${name}]`);
    const lo = val("floor_utilisation_low"), hi = val("floor_utilisation_high");
    const crossed = lo.value !== "" && hi.value !== "" && Number(lo.value) > Number(hi.value);
    for (const label of grid.querySelectorAll("label")) {
      const input = label.querySelector("input, select"), meta = label.querySelector(".param-meta");
      const key = input.name, d = paramDefault(grid, key), range = (state.meta.param_ranges || {})[key];
      const isNum = input.tagName === "INPUT";
      const value = isNum ? Number(input.value) : input.value;
      let msg = "", bad = false;
      if (isNum && input.value !== "" && (!Number.isFinite(value) || (range && (value < range[0] || value > range[1])))) {
        bad = true; msg = t("params.outOfRange", { lo: range ? range[0] : "", hi: range ? range[1] : "" });
      } else if (crossed && (input === lo || input === hi)) {
        bad = true; msg = t("params.lowAboveHigh");
      }
      const changed = !bad && input.value !== "" && (isNum ? value !== Number(d) : value !== d);
      input.classList.toggle("invalid", bad);
      label.classList.toggle("changed", changed);
      meta.classList.toggle("changed", changed);
      meta.classList.toggle("bad", bad);
      // 比例类改过后同时显示现在是百分之几，避免把 15% 填成 15
      meta.textContent = msg || (changed && SHARE_KEYS.includes(key)
        ? t("params.changedShare", { now: `${+(value * 100).toFixed(1)}%`, v: paramText(key, d) })
        : t(changed ? "params.changed" : "params.default", { v: paramText(key, d) }));
    }
  }

  // 每条线的框架箱/开顶箱柜数（可选）：填了就用所填柜数，空着按超限比例
  function buildFrameGrid(job, values) {
    const grid = $("frameGrid"), cols = (job.estimate && job.estimate.drawings) || [];
    const v = values || (job.params && job.params.frame_boxes) || {};
    $("frameBox").hidden = cols.length === 0;
    grid.textContent = "";
    for (const c of cols) {
      const share = c.frame_boxes_by_share ?? c.frame_boxes_mid;   // 旧任务没有 frame_boxes_by_share
      const label = document.createElement("label");
      const span = document.createElement("span");
      span.textContent = c.name;
      const input = document.createElement("input");
      input.type = "number"; input.min = "0"; input.step = "1"; input.inputMode = "numeric";
      input.max = state.meta.max_frame_boxes;
      input.dataset.drawing = c.id;
      input.value = v[c.id] ?? "";
      input.placeholder = t("frame.placeholder", { n: share });
      const meta = document.createElement("small");
      meta.className = "param-meta";
      const check = () => {
        const n = Number(input.value);
        const bad = input.value !== "" && !(Number.isInteger(n) && n >= 0 && n <= state.meta.max_frame_boxes);
        input.classList.toggle("invalid", bad);
        meta.classList.toggle("bad", bad);
        meta.classList.toggle("changed", !bad && input.value !== "");
        label.classList.toggle("changed", !bad && input.value !== "");
        meta.textContent = t(bad ? "frame.bad" : input.value === "" ? "frame.byShare" : "frame.byCount", { n: share });
      };
      input.addEventListener("input", check);
      check();
      label.append(span, input, meta);
      grid.appendChild(label);
    }
  }

  function readFrameBoxes() {
    const out = {};
    $("frameGrid").querySelectorAll("input").forEach((el) => { if (el.value !== "") out[el.dataset.drawing] = el.value; });
    return out;
  }

  function readParams(grid) {
    const out = {};
    grid.querySelectorAll("input, select").forEach((el) => { if (el.value !== "") out[el.name] = el.value; });
    return out;
  }

  async function loadMeta() {
    const res = await fetch("/api/meta");
    state.meta = await res.json();
    $("laborSource").textContent = state.meta.labor_source || "—";
    $("laborBasisSource").textContent = state.meta.labor_area_basis_source || "—";
    $("ratesSource").textContent = state.meta.rates_source || "—";
    buildParamGrid($("paramGrid"), null);
    renderOcrPill();
  }

  // ------------------------------------------------------------ 选择文件 / 图纸库
  const maxDrawings = () => (state.meta ? state.meta.limits.max_drawings : 30);

  // 一次可加入多个文件或整个文件夹；不是 PDF/DXF、太大、重复、超出上限的文件跳过并说明
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true });
  let hashing = Promise.resolve();                 // 逐个算 SHA-256：几十个大文件同时读进内存会很慢

  function addFiles(fileList) {
    const files = [...fileList].sort(byName);
    const skipped = { notPdf: 0, big: 0, dup: 0, limit: 0 };
    let added = 0;
    for (const f of files) {
      const kind = kindOf(f);
      if (!kind) { if (!/^\./.test(f.name)) skipped.notPdf++; continue; }
      if (f.size > maxBytes(kind)) { skipped.big++; continue; }
      if (state.rows.some((r) => r.file && r.filename === f.name && r.size === f.size && r.file.lastModified === f.lastModified)) { skipped.dup++; continue; }
      if (state.rows.length >= maxDrawings()) { skipped.limit++; continue; }
      const row = { file: f, kind, sha256: null, known: false, hashing: true, meta: null,
                    filename: f.name, size: f.size, name: "", ref: "", mpp: "", unit: "" };
      state.rows.push(row);
      added++;
      hashing = hashing.then(() => identify(row));
    }
    state.addNote = { added, ...skipped };
    $("uploadError").textContent = "";
    renderRows();
  }

  // 拖入的文件夹：逐层读出其中的文件（必须在 drop 事件中同步取得 entry）
  async function filesFromDrop(dt) {
    const entries = [...(dt.items || [])].map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
    if (!entries.length) return [...dt.files];
    const out = [];
    async function walk(entry) {
      if (entry.isFile) {
        out.push(await new Promise((res, rej) => entry.file(res, rej)));
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        for (;;) {
          const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
          if (!batch.length) break;
          for (const e of batch) await walk(e);
        }
      }
    }
    for (const e of entries) await walk(e);
    return out;
  }

  function renderAddNote() {
    const n = state.addNote, el = $("addNote");
    if (!n) { el.textContent = ""; return; }
    const parts = [t("upload.added", { n: n.added })];
    if (n.notPdf) parts.push(t("upload.skipNotPdf", { n: n.notPdf }));
    if (n.big) parts.push(t("upload.skipBig", { n: n.big, pdf: state.meta.limits.max_file_mb, dxf: state.meta.limits.max_dxf_mb }));
    if (n.dup) parts.push(t("upload.skipDup", { n: n.dup }));
    if (n.limit) parts.push(t("upload.skipLimit", { n: n.limit, max: maxDrawings() }));
    el.textContent = parts.join(" ");
  }

  // 在浏览器里算 SHA-256：图纸库里已有的同一文件不再上传，并带出上次的名称和标注尺寸
  async function identify(row) {
    try {
      if (!state.rows.includes(row)) return;       // 识别前已被移除
      if (!(window.crypto && crypto.subtle) || row.file.size > HASH_MAX_BYTES) return;
      const digest = await crypto.subtle.digest("SHA-256", await row.file.arrayBuffer());
      row.sha256 = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
      if (state.rows.some((r) => r !== row && r.sha256 === row.sha256)) {   // 内容相同（改了名）的文件只算一次
        state.rows.splice(state.rows.indexOf(row), 1);
        if (state.addNote) { state.addNote.added--; state.addNote.dup++; }
        return;
      }
      const res = await fetch(`/api/library/${row.sha256}`);
      if (res.ok) {
        const m = await res.json();
        row.known = true; row.meta = m;
        if (!row.name && m.name) row.name = m.name;
        if (isBlank(row.ref) && m.reference_length_mm) row.ref = String(m.reference_length_mm);
        if (isBlank(row.mpp) && m.mm_per_pt) row.mpp = String(m.mm_per_pt);
      }
    } catch (_) { /* 识别失败就照常上传 */ } finally {
      row.hashing = false;
      renderRows();
    }
  }

  function addLibraryEntries(list) {
    const note = { added: 0, notPdf: 0, big: 0, dup: 0, limit: 0 };
    for (const m of list) {
      if (state.rows.some((r) => r.sha256 === m.sha256)) { note.dup++; continue; }
      if (state.rows.length >= maxDrawings()) { note.limit++; continue; }
      state.rows.push({ file: null, kind: m.kind || "pdf", sha256: m.sha256, known: true, hashing: false, meta: m,
                        filename: m.filenames[m.filenames.length - 1], size: m.size_bytes, name: m.name || "",
                        ref: m.reference_length_mm ? String(m.reference_length_mm) : "", mpp: m.mm_per_pt ? String(m.mm_per_pt) : "",
                        unit: "" });
      note.added++;
    }
    state.addNote = note;
    renderRows();
    $("uploadCard").scrollIntoView({ behavior: "smooth" });
  }

  function rowStatus(row) {
    if (row.hashing) return `<span class="tag">${esc(t("row.hashing"))}</span>`;
    if (!row.known) return "";
    const m = row.meta || {};
    const detail = row.kind === "dxf"
      ? t("row.knownDetailDxf", { unit: m.dxf_unit_mm ? unitLabel(m.dxf_unit_mm) : t("unit.declared"), area: num(m.footprint_m2), when: when(m.last_used) })
      : t("row.knownDetail", { ref: num(m.reference_length_mm, 0), area: num(m.footprint_m2), when: when(m.last_used) });
    return `<span class="tag ok" title="${esc(detail)}">${esc(t("row.known"))}</span> <span class="size">${esc(m.footprint_m2 ? detail : "")}</span>`;
  }

  function renderRows() {
    const body = $("fileRows");
    body.textContent = "";
    $("fileTableWrap").hidden = state.rows.length === 0;
    $("rowCount").textContent = t("upload.count", { n: state.rows.length, max: maxDrawings() });
    renderAddNote();
    $("runBtn").disabled = state.rows.length === 0 || isRunning() || state.rows.some((r) => r.hashing);
    state.rows.forEach((row, i) => {
      const tr = document.createElement("tr");
      // PDF：标注总尺寸 + 比例；DXF：单位（文件已声明时以文件为准，未声明时才用这里的选择）
      const inputs = row.kind === "dxf"
        ? `<td><select class="unit" aria-label="${esc(t("col.unit"))}"><option value="">${esc(t("unit.auto"))}</option>
            ${Object.entries(state.meta ? state.meta.dxf_units : {}).map(([k, v]) => `<option value="${v}">${esc(t(`unit.${k}`))}</option>`).join("")}
          </select></td><td class="muted">—</td>`
        : `<td><input type="number" class="ref" min="1" step="any" inputmode="decimal" placeholder="${esc(t("ref.placeholder"))}" aria-label="${esc(t("col.ref"))}"></td>
          <td><input type="number" class="mpp" min="0" step="any" inputmode="decimal" placeholder="${esc(t("mpp.placeholder"))}" aria-label="${esc(t("col.mpp"))}"></td>`;
      tr.innerHTML = `
        <td class="filecell"><span title="${esc(row.filename)}">${esc(row.filename)}</span> <span class="tag">${row.kind === "dxf" ? "DXF" : "PDF"}</span>
          <span class="size">${mb(row.size)}</span><br>${rowStatus(row)}</td>
        <td><input type="text" class="name" maxlength="120" placeholder="${esc(t("name.placeholder"))}" aria-label="${esc(t("col.name"))}"></td>
        ${inputs}
        <td class="num"><button type="button" class="remove" aria-label="${esc(t("col.remove"))}">×</button></td>`;
      const name = tr.querySelector(".name");
      name.value = row.name;
      name.addEventListener("input", () => { row.name = name.value; });
      if (row.kind === "dxf") {
        const unit = tr.querySelector(".unit");
        unit.value = row.unit;
        unit.addEventListener("change", () => { row.unit = unit.value; });
      } else {
        const ref = tr.querySelector(".ref"), mpp = tr.querySelector(".mpp");
        ref.value = row.ref; mpp.value = row.mpp;
        ref.addEventListener("input", () => { row.ref = ref.value; ref.classList.remove("invalid"); });
        mpp.addEventListener("input", () => { row.mpp = mpp.value; });
      }
      tr.querySelector(".remove").addEventListener("click", () => { state.rows.splice(i, 1); renderRows(); });
      body.appendChild(tr);
    });
  }

  async function loadLibrary() {
    try { state.library = await (await fetch("/api/library")).json(); } catch (_) { return; }
    renderLibrary();
  }

  function renderLibrary() {
    const list = $("libraryList");
    $("libraryAllBtn").hidden = state.library.length < 2;
    if (!state.library.length) { list.innerHTML = `<li class="muted">${esc(t("library.empty"))}</li>`; return; }
    list.innerHTML = state.library.map((m, i) => `<li>
      <span class="lib-name">${esc(m.name || m.filenames[m.filenames.length - 1])}</span>
      <span class="muted">${esc(m.filenames.join("、"))} · ${mb(m.size_bytes)}</span>
      ${m.kind === "dxf"
        ? `<span class="tag">DXF</span>${m.dxf_unit_mm ? `<span>${esc(unitLabel(m.dxf_unit_mm))}</span>` : ""}`
        : m.reference_length_mm ? `<span>${num(m.reference_length_mm, 0)} mm</span>` : `<span class="tag warn">${esc(t("library.noRef"))}</span>`}
      ${m.footprint_m2 !== null ? `<span>${num(m.footprint_m2)} m²</span>` : ""}
      <span class="when">${esc(when(m.last_used || m.first_uploaded))} · ${esc(t("library.uses", { n: m.uses }))}</span>
      <button type="button" data-lib="${i}">${esc(t("library.add"))}</button></li>`).join("");
    list.querySelectorAll("button[data-lib]").forEach((b) => b.addEventListener("click", () => addLibraryEntries([state.library[Number(b.dataset.lib)]])));
  }

  const isRunning = () => !!state.job && (state.job.status === "queued" || state.job.status === "running");

  // ------------------------------------------------------------ 提交
  async function submit(rows, files, paramsJson, errorEl) {
    const fd = new FormData();
    for (const [file, name] of files) fd.append("files", file, name);
    fd.append("rows", JSON.stringify(rows));
    fd.append("params", paramsJson);
    try {
      const res = await fetch("/api/jobs", { method: "POST", body: fd });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { errorEl.textContent = say(body.error) || `HTTP ${res.status}`; return false; }
      location.hash = `job=${body.job_id}`;          // hashchange -> openJob
      return true;
    } catch (_) {
      errorEl.textContent = t("err.network");
      return false;
    }
  }

  async function run() {
    const err = $("uploadError");
    err.textContent = "";
    let ok = true;
    $("fileRows").querySelectorAll("tr").forEach((tr, i) => {
      const v = state.rows[i].ref;
      if (state.rows[i].kind !== "dxf" && !isBlank(v) && !(Number(v) > 0)) { tr.querySelector(".ref").classList.add("invalid"); ok = false; }
    });
    if (!ok) { err.textContent = t("err.refInvalid"); return; }
    const rows = [], files = [];
    for (const r of state.rows) {
      const base = r.kind === "dxf"
        ? { name: r.name, dxf_unit_mm: r.unit }
        : { name: r.name, reference_length_mm: r.ref, mm_per_pt: r.mpp };
      if (r.known && r.sha256) rows.push({ ...base, sha256: r.sha256 });         // 图纸库中已有：不再上传
      else { rows.push({ ...base, file: files.length }); files.push([r.file, r.filename]); }
    }
    const btn = $("runBtn");
    btn.disabled = true; btn.textContent = t("running");
    if (await submit(rows, files, JSON.stringify(readParams($("paramGrid"))), err)) { state.rows = []; state.addNote = null; renderRows(); }
    btn.textContent = t("run");
    btn.disabled = state.rows.length === 0 || isRunning();
  }

  // 缺少标注总尺寸（PDF）或单位（DXF）：填写后用图纸库中的同一文件重新提交（不需要再上传）
  async function continueWithDimensions() {
    const job = state.job, err = $("dimError");
    err.textContent = "";
    const values = {}, units = {};
    let ok = true;
    $("dimList").querySelectorAll("input[data-id]").forEach((input) => {
      if (!(Number(input.value) > 0)) { input.classList.add("invalid"); ok = false; } else values[input.dataset.id] = input.value;
    });
    $("dimList").querySelectorAll("select[data-unit-id]").forEach((sel) => {
      if (!sel.value) { sel.classList.add("invalid"); ok = false; } else units[sel.dataset.unitId] = sel.value;
    });
    if (!ok) { err.textContent = t("err.inputsMissing"); return; }
    const rows = job.drawings.map((d) => (d.kind === "dxf"
      ? { sha256: d.sha256, name: d.name, dxf_unit_mm: units[d.id] ?? (d.unit_source === "user" ? d.dxf_unit_mm : "") }
      : { sha256: d.sha256, name: d.name,
          reference_length_mm: values[d.id] ?? d.reference_length_mm ?? "", mm_per_pt: d.mm_per_pt ?? "" }));
    $("dimBtn").disabled = true;
    await submit(rows, [], JSON.stringify(job.params), err);
    $("dimBtn").disabled = false;
  }

  // ------------------------------------------------------------ 任务与轮询
  function jobIdFromHash() {
    const m = /job=([0-9a-f]{32})/.exec(location.hash);
    return m ? m[1] : null;
  }

  function stopPolling() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  async function openJob(id) {
    stopPolling();
    state.pollFailures = 0;
    try {
      const res = await fetch(`/api/jobs/${id}`);
      const body = await res.json();
      if (!res.ok) { state.job = null; showNoJob(say(body.error)); return; }
      state.job = body;
      buildParamGrid($("recalcGrid"), body.params);
      buildFrameGrid(body);
      $("recalcMsg").textContent = ""; $("recalcError").textContent = "";
      renderJob(body);
      loadRecent();
      if (isRunning()) state.pollTimer = setTimeout(() => poll(id), POLL_MS);
    } catch (_) {
      state.pollTimer = setTimeout(() => poll(id), POLL_MS * 2);
    }
  }

  async function poll(id) {
    if (jobIdFromHash() !== id) return;
    try {
      const res = await fetch(`/api/jobs/${id}`);
      const body = await res.json();
      if (!res.ok) { showNoJob(say(body.error)); return; }
      state.pollFailures = 0;
      state.job = body;
      renderJob(body);
      if (isRunning()) state.pollTimer = setTimeout(() => poll(id), POLL_MS);
      else { buildParamGrid($("recalcGrid"), body.params); buildFrameGrid(body); loadRecent(); loadLibrary(); loadSystem(); }
    } catch (_) {
      state.pollFailures += 1;
      if (state.pollFailures >= 3) $("progressText").textContent = t("err.network");
      state.pollTimer = setTimeout(() => poll(id), POLL_MS * 2);
    }
  }

  function showNoJob(message) {
    $("progressCard").hidden = true;
    $("resultCard").hidden = false;
    $("failedBox").hidden = false;
    $("failedMsg").textContent = message;
    $("failedDetailBox").hidden = true;
    $("dimBox").hidden = true;
    $("warnBox").hidden = true;
    $("resultBody").hidden = true;
    $("downloadBtn").hidden = true;
    $("reportBtn").hidden = true;
  }

  function renderProgress(job) {
    const { done, total } = job.progress;
    let text, width;
    switch (job.stage) {
      case "checking": text = t("progress.checking"); width = 6; break;
      case "measuring": text = t("progress.measuring", { i: Math.min(done + 1, total), n: total }); width = 10 + 78 * done / total; break;
      case "workbook": text = t("progress.workbook"); width = 92; break;
      default: text = t("progress.queued"); width = 3;
    }
    $("progressText").textContent = text;
    $("progressFill").style.width = `${width}%`;
    const secs = Math.max(0, Math.round((Date.now() - new Date(job.created_at).getTime()) / 1000));
    $("progressElapsed").textContent = t("progress.elapsed", { s: secs });
  }

  // ------------------------------------------------------------ 结果
  function renderJob(job) {
    const running = isRunning();
    $("progressCard").hidden = !running;
    $("resultCard").hidden = running;
    $("runBtn").disabled = state.rows.length === 0 || running;
    if (running) { renderProgress(job); return; }

    const failed = job.status === "failed";
    const needsDim = failed && job.error && ["dimension_needed", "dxf_units_needed", "inputs_needed"].includes(job.error.code)
      && (job.needs_dimension || []).length + (job.needs_units || []).length > 0;
    $("failedBox").hidden = !failed || needsDim;
    $("dimBox").hidden = !needsDim;
    $("resultBody").hidden = failed;
    $("downloadBtn").hidden = failed;
    $("reportBtn").hidden = failed;
    if (needsDim) renderDimensionPrompt(job);
    else if (failed) {
      $("failedMsg").textContent = say(job.error);
      $("failedDetailBox").hidden = !job.detail;
      $("failedDetail").textContent = job.detail || "";
    }
    const warnings = job.warnings || [];
    $("warnBox").hidden = failed || warnings.length === 0;
    $("warnList").innerHTML = warnings.map((w) => `<li>${esc(say(w))}</li>`).join("");
    if (failed) return;

    $("downloadBtn").href = job.workbook_url;
    renderResources(job);
    renderKpis(job);
    renderSummary(job);
    renderDrawings(job);
  }

  // DXF 未声明单位：列出各单位下文件头记录的图形范围有多大，帮助判断（不预选任何单位）
  function unitPrompt(n) {
    const ext = n.extents;
    const options = Object.entries(state.meta.dxf_units).map(([k, v]) => `<option value="${v}">${esc(t(`unit.${k}`))}${
      ext ? esc(t("unit.extentAs", { w: lengthText(ext[0] * v), h: lengthText(ext[1] * v) })) : ""}</option>`).join("");
    return `<div class="dim-item">
      <p><b>${esc(n.name)}</b> <span class="muted">${esc(n.filename)} · DXF ${esc(n.release || "")}</span></p>
      <p class="note">${esc(ext ? t("unit.extentHint", { w: num(ext[0], 0), h: num(ext[1], 0) }) : t("unit.noExtent"))}</p>
      <label><span>${esc(t("unit.question"))}</span>
        <select data-unit-id="${esc(n.id)}"><option value="">${esc(t("unit.choose"))}</option>${options}</select></label>
    </div>`;
  }

  function renderDimensionPrompt(job) {
    const dims = job.needs_dimension || [], unitNeeds = job.needs_units || [];
    $("dimTitle").textContent = t(dims.length && unitNeeds.length ? "inputs.title" : dims.length ? "dim.title" : "unit.title");
    $("dimMsg").textContent = say(job.error);
    $("dimError").textContent = "";
    // 不放示例数字：示例值很容易被照抄（例如把 101 图的 75000 填给 102 图），而面积随它的平方变化
    $("dimList").innerHTML = dims.map((n) => `<div class="dim-item">
      <figure><img src="${esc(n.image_url)}" alt="${esc(n.name)}"><figcaption>${esc(t("dim.caption"))}
        · <a href="${esc(n.image_url)}" target="_blank" rel="noopener">${esc(t("dim.fullSize"))}</a></figcaption></figure>
      <label><span>${esc(n.name)} — ${esc(t(n.orientation === "horizontal" ? "dim.horizontal" : "dim.vertical"))}</span>
        <input type="number" min="1" step="any" inputmode="decimal" placeholder="mm" data-id="${esc(n.id)}"></label>
    </div>`).join("") + (dims.length ? `<p class="note crit-note">${esc(t("dim.sensitivity"))}</p>` : "")
      + unitNeeds.map(unitPrompt).join("") + (unitNeeds.length ? `<p class="note crit-note">${esc(t("unit.sensitivity"))}</p>` : "");
    $("dimList").querySelectorAll("input").forEach((i) => i.addEventListener("input", () => i.classList.remove("invalid")));
    $("dimList").querySelectorAll("select").forEach((s) => s.addEventListener("change", () => s.classList.remove("invalid")));
    wireLightbox($("dimList"));
  }

  function kpi(label, value, sub, key) {
    return `<div class="kpi${key ? " key" : ""}"><div class="label">${esc(label)}</div>
      <div class="value">${value}</div><div class="sub">${esc(sub)}</div></div>`;
  }

  function renderKpis(job) {
    const e = job.estimate, tot = e.total, p = e.params;
    const fromFootprint = e.drawings.some((d) => d.fine_source === "footprint");
    const rate = e.drawings.length ? e.drawings[0].rate_usd_per_40ft : null;
    $("kpis").innerHTML = [
      kpi(t("kpi.fine"), `${num(tot.fine_m2, 1)} <small>m²</small>`, fromFootprint ? t("fine.fromFootprint") : t("kpi.fineNote"), true),
      kpi(t("kpi.baseline"), `${num(tot.baseline_m2, 1)} <small>m²</small>`, t("kpi.baselineNote")),
      kpi(t("kpi.ratio"), pct(tot.fine_vs_baseline), t("kpi.ratioNote")),
      tot.labor_hours === undefined
        ? kpi(t("kpi.hoursLegacy"), `${num(tot.hours_fine, 0)} <small>h</small>`, t("kpi.hoursLegacyNote", { r: p.shared_hours_per_m2 }), true)
        : kpi(t("kpi.hours", { basis: t(`basis.${p.labor_area_basis}`) }), `${num(tot.labor_hours, 0)} <small>h</small>`,
          t("kpi.hoursNote", { f: num(tot.labor_hours_fitter, 0), e: num(tot.labor_hours_electrician, 0),
            rf: p.fitter_hours_per_m2, re: p.electrician_hours_per_m2 }), true),
      kpi(t("kpi.boxes"), num(tot.boxes_mid, 0), t("kpi.boxesNote", { lo: tot.boxes_low, hi: tot.boxes_high, f: tot.frame_boxes_mid })),
      kpi(t("kpi.freight"), usd(tot.freight_mid_usd), t("kpi.freightNote", { route: e.route, rate: usd(rate) })),
    ].join("");
  }

  function renderSummary(job) {
    const cols = job.estimate.drawings, tot = job.estimate.total;
    let html = `<thead><tr><th>${esc(t("summary.item"))}</th>`;
    for (const c of cols) html += `<th class="num">${esc(c.name)}</th>`;
    if (cols.length > 1) html += `<th class="num">${esc(t("summary.total"))}</th>`;
    html += "</tr></thead><tbody>";
    for (const [key, kind, isKey, hasTotal] of SUMMARY_ROWS) {
      if (!cols.some((c) => key in c)) continue;
      const label = key === "labor_hours"
        ? t("row.labor_hours", { basis: t(`basis.${job.estimate.params.labor_area_basis}`) }) : t(`row.${key}`);
      html += `<tr class="${isKey ? "key" : ""}"><td>${esc(label)}</td>`;
      for (const c of cols) {
        const src = key === "fine_m2" && c.fine_source === "footprint" ? `<span class="src">${esc(t("fine.fromFootprint"))}</span>`
          : key === "frame_boxes_mid" && c.frame_source === "count" ? `<span class="src">${esc(t("frame.fromCount"))}</span>` : "";
        html += `<td class="num">${fmt(c[key], kind)}${src}</td>`;
      }
      if (cols.length > 1) html += `<td class="num">${hasTotal ? fmt(tot[key], kind) : ""}</td>`;
      html += "</tr>";
    }
    $("summaryTable").innerHTML = html + "</tbody>";
  }

  function figure(url, caption, kind) {
    if (!url) return "";
    return `<figure data-kind="${kind}"><img src="${esc(url)}" alt="${esc(caption)}" loading="lazy"><figcaption>${esc(caption)}</figcaption></figure>`;
  }

  function wireLightbox(host) {
    host.querySelectorAll("figure img").forEach((img) => img.addEventListener("click", () => {
      $("lightboxImg").src = img.src; $("lightboxImg").alt = img.alt; $("lightbox").showModal();
    }));
  }

  function renderDrawings(job) {
    const host = $("drawings");
    host.innerHTML = (job.results || []).map((d, i) => {
      const input = job.drawings[i] || {};
      if (d.source === "dxf") return renderDxfDrawing(d, input);
      const scaleSrc = input.mm_per_pt ? t("drawing.scaleManual") : t("drawing.scaleAuto", { ref: num(d.reference_length_mm, 0) });
      const refSrc = input.reference_source === "library" ? t("drawing.refLibrary") : t("drawing.refUser");
      const reused = input.reused ? `<span class="tag ok">${esc(t("drawing.reused", { when: when(input.reused_from) }))}</span>` : "";
      const lanes = d.envelope.lanes.map((l) => `<tr><td>${esc(l.lane)}</td><td class="num">${num(l.length_m)}</td>
        <td class="num">${num(l.width_m)}</td><td class="num">${num(l.length_m * l.width_m)}</td><td class="num">${num(l.footprint_m2)}</td></tr>`).join("");
      const stations = d.stations.length
        ? `<div class="table-wrap"><table class="stations"><thead><tr>
            <th>${esc(t("st.op"))}</th><th>${esc(t("st.name"))}</th><th class="num">${esc(t("st.area"))}</th>
            <th class="num">${esc(t("st.bbox"))}</th><th>${esc(t("st.ocr"))}</th><th>${esc(t("st.inferred"))}</th></tr></thead><tbody>
            ${d.stations.map((s) => `<tr><td>${esc(s.op)}</td><td>${esc(s.name)}</td><td class="num">${num(s.footprint_m2, 2)}</td>
              <td class="num">${num(s.bbox_length_m)} × ${num(s.bbox_width_m)}</td><td class="ocr">${esc(s.ocr_text)}</td>
              <td class="inf">${s.op_inferred ? esc(t("st.yes")) : ""}</td></tr>`).join("")}
          </tbody></table></div>`
        : `<p class="note">${esc(t("st.none"))}</p>`;
      return `<section class="drawing">
        <h3>${esc(d.name)} <span class="muted">(${esc(d.id)})</span> ${reused}</h3>
        <div class="facts">
          <span>${esc(t("drawing.footprint"))}: <b>${num(d.footprint_m2)} m²</b></span>
          <span>${esc(t("drawing.reference"))}: <b>${num(d.reference_length_mm, 0)} mm</b> (${esc(refSrc)})</span>
          <span>${esc(t("drawing.scale"))}: <b>${num(d.mm_per_pt, 2)} mm/pt</b> (${esc(scaleSrc)})</span>
          <span>${esc(t("drawing.building"))}: <b>${num(d.building_box_m[0])} × ${num(d.building_box_m[1])} m</b></span>
          <span>${esc(t("drawing.agv"))}: <b>${num(d.agv_circles_removed, 0)}</b></span>
          <span>${esc(t("drawing.groups"))}: <b>${num(d.n_groups, 0)}</b></span>
        </div>
        <div class="figs">${figure(d.overlay_url, t("drawing.overlay"), "overlay")}${figure(d.station_overlay_url, t("drawing.stationOverlay"), "stations")}</div>
        <h3>${esc(t("drawing.lanes"))}</h3>
        <div class="table-wrap"><table><thead><tr><th>${esc(t("lane.lane"))}</th><th class="num">${esc(t("lane.length"))}</th>
          <th class="num">${esc(t("lane.width"))}</th><th class="num">${esc(t("lane.area"))}</th><th class="num">${esc(t("lane.footprint"))}</th></tr></thead>
          <tbody>${lanes}</tbody></table></div>
        <h3>${esc(t("drawing.stations"))}</h3>${stations}
      </section>`;
    }).join("");
    wireLightbox(host);
  }

  // DXF：单位及其来源、单列的移动/待定面积；叠加图颜色与 PDF 不同（绿 = 计入）
  function renderDxfDrawing(d, input) {
    const reused = input.reused ? `<span class="tag ok">${esc(t("drawing.reused", { when: when(input.reused_from) }))}</span>` : "";
    const lanes = d.envelope.lanes.map((l) => `<tr><td>${esc(l.lane)}</td><td class="num">${num(l.length_m)}</td>
      <td class="num">${num(l.width_m)}</td><td class="num">${num(l.length_m * l.width_m)}</td><td class="num">${num(l.footprint_m2)}</td></tr>`).join("");
    const basis = input.unit_basis && input.unit_source !== "user" ? ` · ${input.unit_basis}` : "";
    return `<section class="drawing">
      <h3>${esc(d.name)} <span class="muted">(${esc(d.id)}) · DXF</span> ${reused}</h3>
      <div class="facts">
        <span>${esc(t("drawing.footprintFixed"))}: <b>${num(d.footprint_m2)} m²</b></span>
        <span>${esc(t("drawing.unit"))}: <b>${esc(unitLabel(d.unit_mm))}</b> (${esc(t(`unitSrc.${input.unit_source || "file"}`))}${esc(basis)})</span>
        <span>${esc(t("drawing.mobile"))}: <b>${num(d.mobile_m2, 1)} m²</b></span>
        <span>${esc(t("drawing.unresolved"))}: <b>${num(d.unresolved_m2, 1)} m²</b> (${esc(t("drawing.items", { n: d.n_unresolved }))})</span>
        <span>${esc(t("drawing.inserts"))}: <b>${num(d.n_equipment, 0)}</b> / ${esc(t("drawing.excluded", { n: d.n_excluded }))}</span>
        <span>${esc(t("drawing.groups"))}: <b>${num(d.n_groups, 0)}</b></span>
        <span>${esc(t("drawing.lineRegion"))}: ${esc(d.line_region_basis || "—")}</span>
      </div>
      <div class="figs">${figure(d.overlay_url, t("drawing.overlayDxf"), "overlay")}</div>
      <h3>${esc(t("drawing.lanes"))}</h3>
      <div class="table-wrap"><table><thead><tr><th>${esc(t("lane.lane"))}</th><th class="num">${esc(t("lane.length"))}</th>
        <th class="num">${esc(t("lane.width"))}</th><th class="num">${esc(t("lane.area"))}</th><th class="num">${esc(t("lane.footprint"))}</th></tr></thead>
        <tbody>${lanes}</tbody></table></div>
      <p class="note">${esc(t("drawing.lanesDxf", { gap: num((d.envelope.gap_mm || 0) / 1000, 0) }))}</p>
      <h3>${esc(t("drawing.stations"))}</h3><p class="note">${esc(t("st.noneDxf"))}</p>
    </section>`;
  }

  // ------------------------------------------------------------ 核对报告（单个 HTML 文件，发给同事核对）
  // 与页面同样的数值与排版；叠加图内嵌为 data URI，样式内联，不含脚本，邮件附件可直接打开。
  const REPORT_CSS = `
    body.report { background: #fff; }
    .report header.rep-head { border-bottom: 2px solid var(--ink); padding-bottom: 10px; margin-bottom: 16px; }
    .report h1 { font-size: 20px; margin: 0 0 6px; }
    .report .card { border-radius: 8px; }
    .report table.kv td:first-child { color: var(--ink-2); white-space: nowrap; width: 1%; }
    .report .sha { font: 11px var(--mono); word-break: break-all; color: var(--ink-2); }
    .report figure img { max-height: none; cursor: default; }
    .report .checklist li { margin: 6px 0; }
    .report table.signoff td { height: 36px; }
    .report footer { color: var(--ink-3); font-size: 11.5px; margin-top: 18px; }
    @media print {
      .report .page { max-width: none; padding: 0; }
      .report .card { border: 0; padding: 0; margin-bottom: 18px; }
      .report .drawing { break-before: page; }
      .report figure, .report tr { break-inside: avoid; }
    }`;

  async function toDataUrl(url) {
    const blob = await (await fetch(url)).blob();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  function reportInputs(job) {
    const rows = job.drawings.map((d, i) => {
      const r = (job.results || [])[i] || {};
      const refSrc = d.reference_source === "library" ? t("drawing.refLibrary") : t("drawing.refUser");
      const scaleSrc = d.mm_per_pt ? t("drawing.scaleManual") : t("drawing.scaleAuto", { ref: num(r.reference_length_mm, 0) });
      const measured = d.reused ? t("report.reusedFrom", { when: when(d.reused_from) }) : t("report.measuredNow");
      if (d.kind === "dxf") {
        return `<tr><td><b>${esc(d.name)}</b> · DXF<br><span class="muted">${esc(d.filename)} · ${mb(d.size_bytes)}</span>
            <div class="sha">SHA-256 ${esc(d.sha256 || "—")}</div></td>
          <td class="num">${esc(unitLabel(r.unit_mm))}<br><span class="muted">${esc(t(`unitSrc.${d.unit_source || "file"}`))}</span></td>
          <td class="num">—</td><td>${esc(measured)}</td></tr>`;
      }
      return `<tr><td><b>${esc(d.name)}</b><br><span class="muted">${esc(d.filename)} · ${mb(d.size_bytes)}</span>
          <div class="sha">SHA-256 ${esc(d.sha256 || "—")}</div></td>
        <td class="num">${num(r.reference_length_mm, 0)} mm<br><span class="muted">${esc(refSrc)}</span></td>
        <td class="num">${num(r.mm_per_pt, 3)} mm/pt<br><span class="muted">${esc(scaleSrc)}</span></td>
        <td>${esc(measured)}</td></tr>`;
    }).join("");
    return `<div class="table-wrap"><table><thead><tr><th>${esc(t("report.drawing"))}</th>
      <th class="num">${esc(t("drawing.reference"))}</th><th class="num">${esc(t("drawing.scale"))}</th>
      <th>${esc(t("report.measurement"))}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function reportParams(job) {
    const p = job.estimate.params;
    const value = (k) => (k === "labor_area_basis" ? t(`basis.${p[k]}`) : p[k]);
    return `<div class="table-wrap"><table class="kv"><tbody>${[...PARAM_KEYS, ...LEGACY_PARAM_KEYS].filter((k) => k in p).map((k) =>
      `<tr><td>${esc(t(`params.${k}`))}</td><td>${esc(value(k))}</td></tr>`).join("")}</tbody></table></div>`;
  }

  async function buildReportHtml(job) {
    const css = await (await fetch("/static/styles.css")).text();
    const drawings = $("drawings").cloneNode(true);
    for (const img of drawings.querySelectorAll("img")) {
      img.src = await toDataUrl(img.getAttribute("src"));
      img.removeAttribute("loading");
    }
    // 报告是静态文件，没有“点击放大”
    drawings.querySelectorAll("figure[data-kind]").forEach((fig) => {
      fig.querySelector("figcaption").textContent = t(fig.dataset.kind === "stations" ? "report.stationCaption" : "report.overlayCaption");
    });
    const summary = $("summaryTable").closest(".table-wrap").cloneNode(true);
    const warnings = (job.warnings || []).map((w) => `<li>${esc(say(w))}</li>`).join("");
    const meta = [
      [t("report.generated"), when(new Date().toISOString())],
      [t("report.created"), when(job.created_at)],
      [t("report.job"), job.id],
      [t("report.ocr"), t(job.ocr ? "flow.ocrOn" : "flow.ocrOff")],
    ].map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
    const checks = [1, 2, 3, 4, 5].map((n) => `<li>${esc(t(`report.check${n}`))}</li>`).join("");
    return `<!doctype html>
<html lang="${state.lang === "zh" ? "zh-CN" : "en"}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(t("report.title"))} — ${esc(job.drawings.map((d) => d.name).join(", "))}</title>
<style>${css}${REPORT_CSS}</style></head>
<body class="report"><main class="page">
<header class="rep-head"><h1>${esc(t("report.title"))}</h1>
  <p class="note">${esc(t("report.lede"))}</p>
  <table class="kv"><tbody>${meta}</tbody></table></header>
<section class="card"><h2>${esc(t("report.inputs"))}</h2>${reportInputs(job)}</section>
${warnings ? `<section class="alert alert-amber"><strong>${esc(t("warnings.title"))}</strong><ul>${warnings}</ul></section>` : ""}
<section class="card"><h2>${esc(t("report.results"))}</h2><div class="kpis">${$("kpis").innerHTML}</div>
  <h3>${esc(t("summary.title"))}</h3>${summary.outerHTML}</section>
<section class="card"><h2>${esc(t("report.params"))}</h2>${reportParams(job)}</section>
<section class="card"><h2>${esc(t("report.drawings"))}</h2>${drawings.innerHTML}</section>
<section class="card"><h2>${esc(t("report.checklist"))}</h2><ol class="checklist">${checks}</ol>
  <h3>${esc(t("report.signoff"))}</h3>
  <table class="signoff"><tbody>
    <tr><td>${esc(t("report.reviewer"))}</td><td></td><td>${esc(t("report.date"))}</td><td></td></tr>
    <tr><td>${esc(t("report.verdict"))}</td><td colspan="3"></td></tr>
    <tr><td>${esc(t("report.remarks"))}</td><td colspan="3"></td></tr>
  </tbody></table></section>
<section class="method"><h2 class="h-small">${esc(t("method.title"))}</h2><p>${esc(t("method.body"))}</p></section>
<footer>${esc(t("report.footer"))}</footer>
</main></body></html>`;
  }

  async function downloadReport() {
    const job = state.job;
    if (!job || job.status !== "done") return;
    const btn = $("reportBtn");
    $("reportError").textContent = ""; $("reportError").classList.remove("ok");
    btn.disabled = true; btn.textContent = t("report.building");
    try {
      const html = await buildReportHtml(job);
      const day = new Date(job.created_at).toISOString().slice(0, 10).replace(/-/g, "");
      const api = desktopApi();
      if (api) { await desktopSave(() => api.save_text(`${t("report.filename")}_${day}.html`, html)); return; }
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
      a.download = `${t("report.filename")}_${day}.html`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
    } catch (_) {
      $("reportError").textContent = t("report.error");
    } finally {
      btn.disabled = false; btn.textContent = t("report.button");
    }
  }

  // ------------------------------------------------------------ 重新计算参数
  async function recalc() {
    if (!state.job) return;
    const btn = $("recalcBtn");
    $("recalcMsg").textContent = ""; $("recalcError").textContent = "";
    btn.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${state.job.id}/estimate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params: { ...readParams($("recalcGrid")), frame_boxes: readFrameBoxes() } }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { $("recalcError").textContent = say(body.error) || `HTTP ${res.status}`; return; }
      state.job = body;
      renderJob(body);
      buildFrameGrid(body);
      $("recalcMsg").textContent = t("recalc.done");
    } catch (_) {
      $("recalcError").textContent = t("err.network");
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------ 工作流程与费用
  const gb = (mbValue) => `${num(mbValue / 1024, 1)} GB`;

  async function loadSystem() {
    try { state.system = await (await fetch("/api/system")).json(); } catch (_) { return; }
    renderSystem();
  }

  function renderSystem() {
    const sys = state.system;
    if (!sys) return;
    const h = sys.hosting, obs = sys.observed, st = sys.storage;
    // 保守取值：本服务器实测最大值与大图参考值（102 图）取较大者 —— 只测过小图时不能据此选小服务器
    const observedHigher = obs && obs.peak_rss_mb > h.reference_peak_mb;
    const peak = observedHigher ? obs.peak_rss_mb : h.reference_peak_mb;
    const peakSrc = observedHigher ? t("sys.peakObserved", { n: obs.runs }) : (h.reference_note[state.lang] || h.reference_note.zh);
    const need = peak + h.overhead_mb;
    const observedText = obs ? t("sys.observed", { n: obs.runs, peak: gb(obs.peak_rss_mb), s: num(obs.seconds_per_drawing, 0) })
                             : t("sys.notObserved");

    $("flow").innerHTML = FLOW.map((col, ci) => `
      <div class="flow-col ${col.stage}">
        <div class="flow-head"><b>${esc(t(`stage.${col.stage}`))}</b><span>${esc(t(`stage.${col.stage}.d`))}</span></div>
        ${col.steps.map((s, si) => {
          const where = sys.desktop && s.where === "server" ? "local" : s.where;
          const chips = [`<span class="chip where-${s.where}">${esc(t(`where.${where}`))}</span>`];
          if (s.critical) chips.push(`<span class="chip crit">${esc(t("flow.critical"))}</span>`);
          if (s.cost) chips.push(`<span class="chip cost">${esc(t("flow.costDriver"))}</span>`);
          if (s.ocr) chips.push(`<span class="chip ${sys.ocr_available ? "on" : "off"}">${esc(t(sys.ocr_available ? "flow.ocrOn" : "flow.ocrOff"))}</span>`);
          let live = "";
          if (s.cost) {
            live = `<div class="live">${esc(t("flow.peak", { peak: gb(peak), src: peakSrc }))}</div><div class="live sub">${esc(observedText)}</div>`;
          }
          if (s.storage) {
            live = `<div class="live">${esc(t("flow.storage", { lib: num(st.library), n: st.library_files, cache: num(st.cache), jobs: num(st.jobs) }))}</div>`;
          }
          return `<div class="step${s.cost ? " cost" : ""}${s.critical ? " crit" : ""}">
            <div class="step-n">${ci + 1}.${si + 1}</div>
            <div class="step-body"><b>${esc(t(`flow.${s.k}.t`))}</b><p>${esc(t(`flow.${s.k}.d`))}</p>
              <div class="chips">${chips.join("")}${s.tools ? `<code>${esc(s.tools)}</code>` : ""}</div>${live}</div></div>`;
        }).join("")}
      </div>${ci < FLOW.length - 1 ? '<div class="flow-arrow" aria-hidden="true"></div>' : ""}`).join("");

    $("sysNeed").textContent = `${t("sys.need", { peak: gb(peak), src: peakSrc, need: gb(need) })} ${observedText}`;
    let html = `<thead><tr><th>${esc(t("host.option"))}</th><th class="num">${esc(t("host.ram"))}</th><th>${esc(t("host.fits"))}</th>
      <th class="num">${esc(t("host.compute"))}</th><th class="num">${esc(t("host.disk"))}</th><th class="num">${esc(t("host.total"))}</th>
      <th>${esc(t("host.note"))}</th></tr></thead><tbody>`;
    for (const o of h.options) {
      const fits = o.ram_gb === null ? null : o.ram_gb * 1024 >= need;
      const disk = o.disk === true ? h.disk_gb * h.disk_usd_per_gb_month : (o.disk === null ? null : 0);
      const total = o.compute_usd + (disk || 0);
      const fitsCell = fits === null ? `<span class="tag">${esc(t("host.fitsIf", { need: gb(need) }))}</span>`
        : `<span class="tag ${fits ? "ok" : "bad"}">${esc(t(fits ? "host.yes" : "host.no"))}</span>`;
      const current = sys.desktop ? o.key === "desktop" : false;
      html += `<tr class="${fits === false ? "unfit" : ""}"><td><b>${esc(o.name[state.lang] || o.name.zh)}</b>${current ? ` <span class="tag ok">${esc(t("host.current"))}</span>` : ""}</td>
        <td class="num">${o.ram_gb === null ? esc(t("host.ownRam")) : `${num(o.ram_gb, o.ram_gb < 1 ? 1 : 0)} GB`}</td>
        <td>${fitsCell}</td>
        <td class="num">US$ ${num(o.compute_usd, 0)}</td>
        <td class="num">${disk === null ? esc(t("host.noDisk")) : `US$ ${num(disk, 2)}`}</td>
        <td class="num"><b>US$ ${num(total, 2)}</b>${esc(t("host.perMonth"))}</td>
        <td class="muted">${esc(o.note[state.lang] || o.note.zh || "")}</td></tr>`;
    }
    $("hostingTable").innerHTML = html + "</tbody>";
    const notes = [
      t("sys.noteSoftware"),
      t("sys.noteGithub"),
      t("sys.noteBandwidth", { gb: h.bandwidth.included_gb, usd: h.bandwidth.usd_per_gb_after }),
      t("sys.noteSource", { src: h.source, date: h.checked }),
    ];
    if (!sys.password_protected && !sys.desktop) notes.push(t("sys.notePassword"));
    let items = notes.map((n) => `<li>${esc(n)}</li>`);
    if (sys.desktop) {
      items = [`<li>${esc(t("sys.noteDesktop", { dir: sys.data_dir }))}${desktopApi() ? ` <button type="button" class="link" id="openDataBtn">${esc(t("desktop.openData"))}</button>` : ""}</li>`, ...items];
    }
    $("sysNotes").innerHTML = items.join("");
    const open = $("openDataBtn");
    if (open) open.addEventListener("click", () => desktopApi() && desktopApi().open_data_dir());
  }

  function renderResources(job) {
    const r = job.resources, line = $("resourceLine");
    line.hidden = !r;
    if (!r) return;
    const parts = [];
    if (r.measure) {
      parts.push(r.measure.peak_rss_mb
        ? t("res.measured", { n: r.measure.drawings, s: num(r.measure.seconds, 1), peak: gb(r.measure.peak_rss_mb) })
        : t("res.measuredNoPeak", { n: r.measure.drawings, s: num(r.measure.seconds, 1) }));
    }
    if (r.reused) parts.push(t("res.reused", { n: r.reused }));
    if (r.workbook) parts.push(t("res.workbook", { s: num(r.workbook.seconds, 1) }));
    line.textContent = t("res.title") + parts.join(t("res.sep"));
  }

  // ------------------------------------------------------------ 最近的估算
  async function loadRecent() {
    let jobs;
    try { jobs = await (await fetch("/api/jobs")).json(); } catch (_) { return; }
    const list = $("recentList");
    if (!jobs.length) { list.innerHTML = `<li class="muted">${esc(t("recent.empty"))}</li>`; return; }
    list.innerHTML = jobs.map((j) => `<li>
      <a href="#job=${esc(j.id)}">${esc(j.drawings.join("、"))}</a>
      <span class="badge ${esc(j.status)}">${esc(t(`status.${j.status}`))}</span>
      ${j.fine_m2 !== null ? `<span>${num(j.fine_m2)} m²</span>` : ""}
      <span class="when">${esc(when(j.created_at))}</span></li>`).join("");
  }

  // ------------------------------------------------------------ 启动
  function route() {
    const id = jobIdFromHash();
    if (id) { openJob(id); return; }
    stopPolling();
    state.job = null;
    $("progressCard").hidden = true;
    $("resultCard").hidden = true;
    renderRows();
  }

  function wire() {
    document.querySelectorAll(".langswitch button").forEach((b) => b.addEventListener("click", () => setLang(b.dataset.lang)));
    const drop = $("drop"), input = $("fileInput");
    input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
    const folder = $("folderInput");
    $("folderBtn").addEventListener("click", () => folder.click());
    folder.addEventListener("change", () => { addFiles(folder.files); folder.value = ""; });
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", async (e) => addFiles(await filesFromDrop(e.dataTransfer)));
    $("clearBtn").addEventListener("click", () => { state.rows = []; state.addNote = null; renderRows(); });
    $("libraryAllBtn").addEventListener("click", () => addLibraryEntries(state.library));
    $("runBtn").addEventListener("click", run);
    $("dimBtn").addEventListener("click", continueWithDimensions);
    $("reportBtn").addEventListener("click", downloadReport);
    $("downloadBtn").addEventListener("click", (e) => {
      const api = desktopApi();
      if (!api) return;                                      // 浏览器：照常下载
      e.preventDefault();
      desktopSave(() => api.save_url($("downloadBtn").getAttribute("href")));
    });
    $("recalcBtn").addEventListener("click", recalc);
    $("newBtn").addEventListener("click", () => { history.pushState(null, "", location.pathname); route(); $("uploadCard").scrollIntoView({ behavior: "smooth" }); });
    $("lightbox").addEventListener("click", (e) => { if (e.target === $("lightbox")) $("lightbox").close(); });
    window.addEventListener("hashchange", route);
  }

  async function start() {
    state.lang = storedLang() === "en" ? "en" : "zh";
    wire();
    try { await loadMeta(); } catch (_) { $("uploadError").textContent = t("err.network"); }
    applyI18n();
    loadLibrary();
    loadSystem();
    route();
  }

  start();
})();
