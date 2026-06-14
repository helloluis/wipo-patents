const $ = s => document.querySelector(s);
const fmt = n => (n ?? 0).toLocaleString();
// ISO alpha-2 -> full country name via the browser's built-in region names (falls back to the code)
const _region = (() => { try { return new Intl.DisplayNames(["en"], { type: "region" }); } catch { return null; } })();
const countryName = cc => { if (!cc) return ""; try { return (_region && _region.of(cc)) || cc; } catch { return cc; } };
let state = { dim: "year", offset: 0, limit: 50, total: 0, applied: {} };
let chart;
const FIELDS = {};  // field_number -> field_name
const esc = s => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function summaryHTML() {
  const a = state.applied || {};
  const st = a.status === "granted" ? "<b>granted</b> " : a.status === "application" ? "<b>pending</b> " : "";
  let s = `Showing all ${st}${a.field ? `<b>${FIELDS[a.field]}</b> ` : ""}patents`;
  if (a.country) s += ` in <b>${countryName(a.country)}</b>`;
  if (a.q) s += ` matching “<b>${esc(a.q)}</b>”`;
  if (a.y0 && a.y1) s += ` from <b>${a.y0}–${a.y1}</b>`;
  else if (a.y0) s += ` from <b>${a.y0}</b> onward`;
  else if (a.y1) s += ` up to <b>${a.y1}</b>`;
  return s + ".";
}

function params() {
  const p = new URLSearchParams();
  const field = $("#f-field").value, country = $("#f-country").value,
        q = $("#f-company").value.trim(), y0 = $("#f-y0").value, y1 = $("#f-y1").value,
        status = $("#f-status").value;
  if (field && field !== "0") p.set("field", field);
  if (status) p.set("status", status);
  if (country) p.set("country", country);
  if (q) p.set("q", q);
  if (y0) p.set("y0", y0);
  if (y1) p.set("y1", y1);
  return p;
}

async function boot() {
  const m = await (await fetch("/api/meta")).json();
  $("#stats").innerHTML =
    `<div class="s"><b>${fmt(m.total_families)}</b><span>inventions</span></div>
     <div class="s"><b>${fmt(m.total_companies)}</b><span>companies</span></div>`;
  for (const f of m.fields) {
    FIELDS[f.field_number] = f.field_name;
    const o = document.createElement("option");
    o.value = f.field_number; o.textContent = `${f.field_number}. ${f.field_name}`;
    $("#f-field").appendChild(o);
  }
  m.countries
    .map(c => ({ code: c, name: countryName(c) }))
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach(c => {
      const o = document.createElement("option"); o.value = c.code; o.textContent = c.name;
      $("#f-country").appendChild(o);
    });
  $("#f-y0").placeholder = m.year_min; $("#f-y1").placeholder = m.year_max;
  refresh();
}

async function refresh() {
  state.offset = 0;
  state.applied = {
    field: $("#f-field").value !== "0" ? +$("#f-field").value : 0,
    status: $("#f-status").value,
    country: $("#f-country").value,
    q: $("#f-company").value.trim(),
    y0: +$("#f-y0").value || 0,
    y1: +$("#f-y1").value || 0,
  };
  $("#filters-summary").innerHTML = summaryHTML();
  await Promise.all([drawChart(), loadPatents()]);
  $("#csv").href = "/api/export.csv?" + params().toString();
}

function toggleFilters() {
  const collapsed = $("#filters").classList.toggle("collapsed");
  $("#caret").setAttribute("aria-expanded", String(!collapsed));
}

const kfmt = v => Math.abs(v) >= 1e6 ? (v/1e6).toFixed(1)+"M" : Math.abs(v) >= 1e3 ? Math.round(v/1e3)+"k" : v;

async function drawChart() {
  const p = params(); p.set("dim", state.dim);
  const { data } = await (await fetch("/api/trend?" + p)).json();
  $("#chart-title").textContent =
    { year: "By filing year — granted vs. pending", field: "Top WIPO technology fields", country: "Top applicant countries" }[state.dim];
  const isYear = state.dim === "year";
  const horizontal = !isYear;
  const labels = data.map(d => state.dim === "country" ? countryName(d.label) : d.label);
  const valueTicks = { font: { size: 11 }, callback: kfmt };
  const catTicks = { font: { size: 11 }, autoSkip: false };

  const datasets = isYear
    ? [{ label: "Granted", data: data.map(d => d.granted), backgroundColor: "#3257d0", borderRadius: 3, maxBarThickness: 26 },
       { label: "Pending", data: data.map(d => d.pending), backgroundColor: "#e8a13c", borderRadius: 3, maxBarThickness: 26 }]
    : [{ data: data.map(d => d.n), backgroundColor: "#3257d0", borderRadius: 4, maxBarThickness: 22 }];

  const tooltip = isYear
    ? { callbacks: { label: c => `${c.dataset.label}: ${fmt(c.parsed.y)}`,
                     footer: items => "Total filed: " + fmt(items.reduce((s, i) => s + i.parsed.y, 0)) } }
    : { callbacks: { label: c => fmt(horizontal ? c.parsed.x : c.parsed.y) + " families" } };

  const cfg = {
    type: "bar",
    data: { labels, datasets },
    options: {
      indexAxis: horizontal ? "y" : "x",
      plugins: {
        legend: { display: isYear, position: "top", align: "end", labels: { boxWidth: 12, font: { size: 12 } } },
        tooltip,
      },
      scales: horizontal
        ? { x: { grid: { color: "#eef0f6" }, ticks: valueTicks }, y: { grid: { display: false }, ticks: catTicks } }
        : { x: { stacked: isYear, grid: { display: false }, ticks: catTicks },
            y: { stacked: isYear, grid: { color: "#eef0f6" }, ticks: valueTicks } },
      responsive: true, maintainAspectRatio: false
    }
  };
  if (chart) chart.destroy();
  chart = new Chart($("#chart"), cfg);
}

async function loadPatents() {
  const p = params(); p.set("limit", state.limit); p.set("offset", state.offset);
  const r = await (await fetch("/api/patents?" + p)).json();
  state.total = r.total;
  $("#pat-total").textContent = `· ${fmt(r.total)} matches`;
  const tb = $("#patents tbody"); tb.innerHTML = "";
  for (const row of r.rows) {
    const co = row.assignees[0];
    const coName = co ? esc(co.name) + (co.country ? `<span class="cc">${countryName(co.country)}</span>` : "") : "—";
    const cpc = (row.cpc_codes || "").split("; ")[0] || "—";
    const badge = row.granted ? '<span class="badge granted">Granted</span>' : '<span class="badge pending">Pending</span>';
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><a class="pub" data-id="${esc(row.family_id)}">${row.rep_publication ?? "—"}</a></td>
      <td>${row.filing_year}</td>
      <td>${badge}</td>
      <td class="field-tag">${row.primary_field_name ?? "—"}</td>
      <td class="code">${row.ipc_main ?? "—"}</td>
      <td class="code">${esc(cpc)}</td>
      <td>${coName}</td>
      <td class="num">${fmt(row.n_bwd_citations)}</td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll("a.pub").forEach(a => a.onclick = () => openPatent(a.dataset.id));
  const from = r.total ? state.offset + 1 : 0;
  $("#page").textContent = `${fmt(from)}–${fmt(Math.min(state.offset + state.limit, r.total))} of ${fmt(r.total)}`;
  $("#prev").disabled = state.offset === 0;
  $("#next").disabled = state.offset + state.limit >= r.total;
}

async function openPatent(id) {
  const body = $("#modal-body");
  body.innerHTML = "<p style='color:#6b7488'>Loading…</p>";
  $("#modal").hidden = false;
  const d = await (await fetch("/api/patent?id=" + encodeURIComponent(id))).json();
  if (d.error) { body.innerHTML = "<p>Not found.</p>"; return; }
  const iso = v => { v = +v; return v ? `${Math.floor(v / 10000)}-${String(Math.floor(v / 100) % 100).padStart(2, "0")}-${String(v % 100).padStart(2, "0")}` : "—"; };
  const chips = s => s ? s.split("; ").filter(Boolean).map(c => `<span class="chip">${esc(c)}</span>`).join("") : "—";
  const assignees = (d.assignees || []).map(a => `${esc(a.name)} <span class="cc">${countryName(a.country_code) || "?"}</span>`).join("<br>") || "—";
  const fields = (d.fields || []).map(f => `${f.field_number}. ${esc(f.field_name)} <span class="field-tag">· ${esc(f.sector_name)}</span>`).join("<br>") || "—";
  const inv = (d.inventor_countries || []).map(countryName).join(", ") || "—";
  const badge = d.granted ? '<span class="badge granted">Granted</span>' : '<span class="badge pending">Pending</span>';
  body.innerHTML = `<div class="det">
    <h2>${d.rep_publication ?? "—"} ${badge}</h2>
    <div class="pubn">Application ${d.rep_application ?? "—"} · DOCDB family ${d.family_id}</div>
    <dl>
      <dt>Priority date</dt><dd>${iso(d.priority_date)}</dd>
      <dt>Filing date</dt><dd>${iso(d.filing_date)}</dd>
      <dt>Publication date</dt><dd>${iso(d.publication_date)}</dd>
      <dt>Grant date</dt><dd>${iso(d.grant_date)}</dd>
      <dt>WIPO field(s)</dt><dd>${fields}</dd>
      <dt>IPC codes</dt><dd>${chips(d.ipc_codes)}</dd>
      <dt>CPC codes</dt><dd>${chips(d.cpc_codes)}</dd>
      <dt>Applicant(s)</dt><dd>${assignees}</dd>
      <dt>Inventor countries</dt><dd>${inv}</dd>
      <dt>Backward citations</dt><dd>${fmt(d.n_bwd_citations)}</dd>
      <dt>Family members (${d.n_publications})</dt><dd>${chips(d.member_publications)}</dd>
    </dl>
  </div>`;
}
function closeModal() { $("#modal").hidden = true; }

// events
$("#apply").onclick = refresh;
$("#reset").onclick = () => {
  $("#f-field").value = "0"; $("#f-status").value = ""; $("#f-country").value = "";
  $("#f-company").value = ""; $("#f-y0").value = ""; $("#f-y1").value = ""; refresh();
};
$("#f-company").addEventListener("keydown", e => { if (e.key === "Enter") refresh(); });
$("#dim").addEventListener("click", e => {
  if (e.target.tagName !== "BUTTON") return;
  state.dim = e.target.dataset.dim;
  [...$("#dim").children].forEach(b => b.classList.toggle("on", b === e.target));
  drawChart();
});
$("#prev").onclick = () => { if (state.offset > 0) { state.offset -= state.limit; loadPatents(); } };
$("#next").onclick = () => { if (state.offset + state.limit < state.total) { state.offset += state.limit; loadPatents(); } };
$("#filters-head").onclick = toggleFilters;
$("#filters-summary").onclick = toggleFilters;
$("#modal-close").onclick = closeModal;
$("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

// curated comparable studies (deep-research, verified)
const RESEARCH = [
  { theme: "Patent economics & economic history", papers: [
    { cite: "Moser, P. (2012). Innovation without Patents: Evidence from World's Fairs. Journal of Law and Economics, 55(1), 43–74.",
      url: "https://www.journals.uchicago.edu/doi/abs/10.1086/663631" },
    { cite: "Bottomley, S. (2014). Patenting in England, Scotland and Ireland during the Industrial Revolution, 1700–1852. Explorations in Economic History, 54, 48–63.",
      url: "https://www.sciencedirect.com/science/article/abs/pii/S0014498314000321" },
  ]},
  { theme: "Schumpeterian growth & patent policy", papers: [
    { cite: "Lu, Lai & Yu (2024). Effects of patent policy on growth and inequality: exogenous versus endogenous quality improvements. Journal of Economics, 141(1).",
      url: "https://link.springer.com/article/10.1007/s00712-023-00843-w" },
    { cite: "Yu & Lai (2024/25). Endogenous Innovation Scale and Patent Policy in a Monetary Schumpeterian Growth Model. Macroeconomic Dynamics, 29.",
      url: "https://www.cambridge.org/core/journals/macroeconomic-dynamics/article/endogenous-innovation-scale-and-patent-policy-in-a-monetary-schumpeterian-growth-model/5473582887203BDB376858704F88413D" },
    { cite: "Li, C.-W. (2008). Promoting innovation and competition with patent policy. Journal of Evolutionary Economics, 18.",
      url: "https://link.springer.com/article/10.1007/s00191-008-0089-5" },
  ]},
  { theme: "ML / data-mining of patents for emerging-tech detection", papers: [
    { cite: "Lee, Kwon, Kim & Kwon (2018). Early identification of emerging technologies: A machine learning approach using multiple patent indicators. Technological Forecasting & Social Change, 127, 291–303.",
      url: "https://www.sciencedirect.com/science/article/abs/pii/S0040162517304778" },
    { cite: "Kyebambe, Cheng, Lin, He & Zhang (2017). Forecasting emerging technologies: A supervised learning approach through patent analysis. Technological Forecasting & Social Change, 125, 236–244.",
      url: "https://www.sciencedirect.com/science/article/abs/pii/S0040162516307065" },
    { cite: "Choi & Yoon et al. (2022). Technology identification from patent texts: A novel named entity recognition method. Technological Forecasting & Social Change.",
      url: "https://www.sciencedirect.com/science/article/abs/pii/S0040162522006813" },
    { cite: "Li et al. (2019). Identifying the Development Trends of Emerging Technologies Using Patent Analysis and Web News Data Mining: The Case of Perovskite Solar Cell Technology. IEEE Access.",
      url: "https://ieeexplore.ieee.org/document/8897119/" },
    { cite: "Lee, Park & Lee (2025). A study on the monitoring of technology innovation through patent analysis. Humanities and Social Sciences Communications.",
      url: "https://www.nature.com/articles/s41599-025-06363-w" },
    { cite: "Liu, Shapira, Yue & Guan (2022). Mapping technological innovation dynamics in artificial intelligence domains: Evidence from a global patent analysis. PLOS ONE, 17(2), e0262050.",
      url: "https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0262050" },
  ]},
];

function renderResearch() {
  $("#research").innerHTML = RESEARCH.map(g => `
    <div class="research-group">
      <h4>${g.theme}</h4>
      ${g.papers.map(p => `<a class="research-item" href="${p.url}" target="_blank" rel="noopener">${esc(p.cite)} <span class="ext">↗</span></a>`).join("")}
    </div>`).join("");
}

renderResearch();
boot();
