// 产线布局投影面积估算 —— 前端。只负责上传、轮询与展示；所有计算在后端。
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PARAM_KEYS = [
    "shared_hours_per_m2", "packaging_factor",
    "floor_utilisation_low", "floor_utilisation_high",
    "footprint_uncertainty_low", "footprint_uncertainty_high",
    "oversize_share", "flat_rack_multiplier", "local_charges_usd_per_box", "insurance_pct_of_freight",
    "route",
  ];
  // 汇总表行：[key, 格式, 是否关键行, 合计列是否显示]
  const SUMMARY_ROWS = [
    ["station_count", "int", false, true],
    ["baseline_m2", "area", true, true],
    ["fine_m2", "area", true, true],
    ["fine_vs_baseline", "pct", false, true],
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

  // rows: [{file: File|null, sha256, known, hashing, meta, filename, size, name, ref, mpp}]
  const state = { lang: "zh", meta: null, system: null, rows: [], job: null, library: [], pollTimer: null, pollFailures: 0 };

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
    document.querySelectorAll(".langswitch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang)));
    renderOcrPill();
    if (state.meta) {
      buildParamGrid($("paramGrid"), readParams($("paramGrid")));
      if (state.job) buildParamGrid($("recalcGrid"), readParams($("recalcGrid")));
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
          o.textContent = `${r.name}（US$ ${num(r.usd_per_40ft, 0)}/40ft）`;
          input.appendChild(o);
        }
        input.value = v.route;
      } else {
        input = document.createElement("input");
        input.type = "number"; input.step = "any"; input.min = "0"; input.inputMode = "decimal";
        input.value = v[key];
      }
      input.name = key;
      label.appendChild(input);
      grid.appendChild(label);
    }
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
    $("ratesSource").textContent = state.meta.rates_source || "—";
    buildParamGrid($("paramGrid"), null);
    renderOcrPill();
  }

  // ------------------------------------------------------------ 选择文件 / 图纸库
  const maxDrawings = () => (state.meta ? state.meta.limits.max_drawings : 10);

  function addFiles(fileList) {
    for (const f of fileList) {
      const isPdf = f.type === "application/pdf" || /\.pdf$/i.test(f.name);
      if (!isPdf || state.rows.length >= maxDrawings()) continue;
      const row = { file: f, sha256: null, known: false, hashing: true, meta: null,
                    filename: f.name, size: f.size, name: "", ref: "", mpp: "" };
      state.rows.push(row);
      identify(row);
    }
    $("uploadError").textContent = "";
    renderRows();
  }

  // 在浏览器里算 SHA-256：图纸库里已有的同一文件不再上传，并带出上次的名称和标注尺寸
  async function identify(row) {
    try {
      if (!(window.crypto && crypto.subtle)) return;
      const digest = await crypto.subtle.digest("SHA-256", await row.file.arrayBuffer());
      row.sha256 = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
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

  function addFromLibrary(m) {
    if (state.rows.length >= maxDrawings()) return;
    state.rows.push({ file: null, sha256: m.sha256, known: true, hashing: false, meta: m,
                      filename: m.filenames[m.filenames.length - 1], size: m.size_bytes, name: m.name || "",
                      ref: m.reference_length_mm ? String(m.reference_length_mm) : "", mpp: m.mm_per_pt ? String(m.mm_per_pt) : "" });
    renderRows();
    $("uploadCard").scrollIntoView({ behavior: "smooth" });
  }

  function rowStatus(row) {
    if (row.hashing) return `<span class="tag">${esc(t("row.hashing"))}</span>`;
    if (!row.known) return "";
    const m = row.meta || {};
    const detail = t("row.knownDetail", { ref: num(m.reference_length_mm, 0), area: num(m.footprint_m2), when: when(m.last_used) });
    return `<span class="tag ok" title="${esc(detail)}">${esc(t("row.known"))}</span> <span class="size">${esc(m.footprint_m2 ? detail : "")}</span>`;
  }

  function renderRows() {
    const body = $("fileRows");
    body.textContent = "";
    $("fileTableWrap").hidden = state.rows.length === 0;
    $("runBtn").disabled = state.rows.length === 0 || isRunning() || state.rows.some((r) => r.hashing);
    state.rows.forEach((row, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="filecell"><span title="${esc(row.filename)}">${esc(row.filename)}</span> <span class="size">${mb(row.size)}</span><br>${rowStatus(row)}</td>
        <td><input type="text" class="name" maxlength="120" placeholder="${esc(t("name.placeholder"))}" aria-label="${esc(t("col.name"))}"></td>
        <td><input type="number" class="ref" min="1" step="any" inputmode="decimal" placeholder="${esc(t("ref.placeholder"))}" aria-label="${esc(t("col.ref"))}"></td>
        <td><input type="number" class="mpp" min="0" step="any" inputmode="decimal" placeholder="${esc(t("mpp.placeholder"))}" aria-label="${esc(t("col.mpp"))}"></td>
        <td class="num"><button type="button" class="remove" aria-label="${esc(t("col.remove"))}">×</button></td>`;
      const name = tr.querySelector(".name"), ref = tr.querySelector(".ref"), mpp = tr.querySelector(".mpp");
      name.value = row.name; ref.value = row.ref; mpp.value = row.mpp;
      name.addEventListener("input", () => { row.name = name.value; });
      ref.addEventListener("input", () => { row.ref = ref.value; ref.classList.remove("invalid"); });
      mpp.addEventListener("input", () => { row.mpp = mpp.value; });
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
    if (!state.library.length) { list.innerHTML = `<li class="muted">${esc(t("library.empty"))}</li>`; return; }
    list.innerHTML = state.library.map((m, i) => `<li>
      <span class="lib-name">${esc(m.name || m.filenames[m.filenames.length - 1])}</span>
      <span class="muted">${esc(m.filenames.join("、"))} · ${mb(m.size_bytes)}</span>
      ${m.reference_length_mm ? `<span>${num(m.reference_length_mm, 0)} mm</span>` : `<span class="tag warn">${esc(t("library.noRef"))}</span>`}
      ${m.footprint_m2 !== null ? `<span>${num(m.footprint_m2)} m²</span>` : ""}
      <span class="when">${esc(when(m.last_used || m.first_uploaded))} · ${esc(t("library.uses", { n: m.uses }))}</span>
      <button type="button" data-lib="${i}">${esc(t("library.add"))}</button></li>`).join("");
    list.querySelectorAll("button[data-lib]").forEach((b) => b.addEventListener("click", () => addFromLibrary(state.library[Number(b.dataset.lib)])));
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
      if (!isBlank(v) && !(Number(v) > 0)) { tr.querySelector(".ref").classList.add("invalid"); ok = false; }
    });
    if (!ok) { err.textContent = t("err.refInvalid"); return; }
    const rows = [], files = [];
    for (const r of state.rows) {
      const base = { name: r.name, reference_length_mm: r.ref, mm_per_pt: r.mpp };
      if (r.known && r.sha256) rows.push({ ...base, sha256: r.sha256 });         // 图纸库中已有：不再上传
      else { rows.push({ ...base, file: files.length }); files.push([r.file, r.filename]); }
    }
    const btn = $("runBtn");
    btn.disabled = true; btn.textContent = t("running");
    if (await submit(rows, files, JSON.stringify(readParams($("paramGrid"))), err)) { state.rows = []; renderRows(); }
    btn.textContent = t("run");
    btn.disabled = state.rows.length === 0 || isRunning();
  }

  // 缺少标注总尺寸：填写后用图纸库中的同一文件重新提交（不需要再上传）
  async function continueWithDimensions() {
    const job = state.job, err = $("dimError");
    err.textContent = "";
    const values = {};
    let ok = true;
    $("dimList").querySelectorAll("input[data-id]").forEach((input) => {
      if (!(Number(input.value) > 0)) { input.classList.add("invalid"); ok = false; } else values[input.dataset.id] = input.value;
    });
    if (!ok) { err.textContent = t("err.refMissing"); return; }
    const rows = job.drawings.map((d) => ({
      sha256: d.sha256, name: d.name,
      reference_length_mm: values[d.id] ?? d.reference_length_mm ?? "", mm_per_pt: d.mm_per_pt ?? "",
    }));
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
      else { buildParamGrid($("recalcGrid"), body.params); loadRecent(); loadLibrary(); loadSystem(); }
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
    const needsDim = failed && job.error && job.error.code === "dimension_needed" && (job.needs_dimension || []).length > 0;
    $("failedBox").hidden = !failed || needsDim;
    $("dimBox").hidden = !needsDim;
    $("resultBody").hidden = failed;
    $("downloadBtn").hidden = failed;
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

  function renderDimensionPrompt(job) {
    $("dimMsg").textContent = say(job.error);
    $("dimError").textContent = "";
    // 不放示例数字：示例值很容易被照抄（例如把 101 图的 75000 填给 102 图），而面积随它的平方变化
    $("dimList").innerHTML = job.needs_dimension.map((n) => `<div class="dim-item">
      <figure><img src="${esc(n.image_url)}" alt="${esc(n.name)}"><figcaption>${esc(t("dim.caption"))}
        · <a href="${esc(n.image_url)}" target="_blank" rel="noopener">${esc(t("dim.fullSize"))}</a></figcaption></figure>
      <label><span>${esc(n.name)} — ${esc(t(n.orientation === "horizontal" ? "dim.horizontal" : "dim.vertical"))}</span>
        <input type="number" min="1" step="any" inputmode="decimal" placeholder="mm" data-id="${esc(n.id)}"></label>
    </div>`).join("") + `<p class="note crit-note">${esc(t("dim.sensitivity"))}</p>`;
    $("dimList").querySelectorAll("input").forEach((i) => i.addEventListener("input", () => i.classList.remove("invalid")));
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
      kpi(t("kpi.hours"), `${num(tot.hours_fine, 0)} <small>h</small>`, t("kpi.hoursNote", { r: p.shared_hours_per_m2 }), true),
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
      html += `<tr class="${isKey ? "key" : ""}"><td>${esc(t(`row.${key}`))}</td>`;
      for (const c of cols) {
        const src = key === "fine_m2" && c.fine_source === "footprint" ? `<span class="src">${esc(t("fine.fromFootprint"))}</span>` : "";
        html += `<td class="num">${fmt(c[key], kind)}${src}</td>`;
      }
      if (cols.length > 1) html += `<td class="num">${hasTotal ? fmt(tot[key], kind) : ""}</td>`;
      html += "</tr>";
    }
    $("summaryTable").innerHTML = html + "</tbody>";
  }

  function figure(url, caption) {
    if (!url) return "";
    return `<figure><img src="${esc(url)}" alt="${esc(caption)}" loading="lazy"><figcaption>${esc(caption)}</figcaption></figure>`;
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
        <div class="figs">${figure(d.overlay_url, t("drawing.overlay"))}${figure(d.station_overlay_url, t("drawing.stationOverlay"))}</div>
        <h3>${esc(t("drawing.lanes"))}</h3>
        <div class="table-wrap"><table><thead><tr><th>${esc(t("lane.lane"))}</th><th class="num">${esc(t("lane.length"))}</th>
          <th class="num">${esc(t("lane.width"))}</th><th class="num">${esc(t("lane.area"))}</th><th class="num">${esc(t("lane.footprint"))}</th></tr></thead>
          <tbody>${lanes}</tbody></table></div>
        <h3>${esc(t("drawing.stations"))}</h3>${stations}
      </section>`;
    }).join("");
    wireLightbox(host);
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
        body: JSON.stringify({ params: readParams($("recalcGrid")) }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { $("recalcError").textContent = say(body.error) || `HTTP ${res.status}`; return; }
      state.job = body;
      renderJob(body);
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
          const chips = [`<span class="chip where-${s.where}">${esc(t(`where.${s.where}`))}</span>`];
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
      html += `<tr class="${fits === false ? "unfit" : ""}"><td><b>${esc(o.name[state.lang] || o.name.zh)}</b></td>
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
    if (!sys.password_protected) notes.push(t("sys.notePassword"));
    $("sysNotes").innerHTML = notes.map((n) => `<li>${esc(n)}</li>`).join("");
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
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
    $("runBtn").addEventListener("click", run);
    $("dimBtn").addEventListener("click", continueWithDimensions);
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
