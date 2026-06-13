const $ = s => document.querySelector(s);
const fmt = n => (n ?? 0).toLocaleString();
// ISO alpha-2 -> full country name via the browser's built-in region names (falls back to the code)
const _region = (() => { try { return new Intl.DisplayNames(["en"], { type: "region" }); } catch { return null; } })();
const countryName = cc => { if (!cc) return ""; try { return (_region && _region.of(cc)) || cc; } catch { return cc; } };
let state = { dim: "year", offset: 0, limit: 50, total: 0 };
let chart;

function params() {
  const p = new URLSearchParams();
  const field = $("#f-field").value, country = $("#f-country").value,
        q = $("#f-company").value.trim(), y0 = $("#f-y0").value, y1 = $("#f-y1").value;
  if (field && field !== "0") p.set("field", field);
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
  await Promise.all([drawChart(), loadPatents(), loadCompanies()]);
  $("#csv").href = "/api/export.csv?" + params().toString();
}

const kfmt = v => Math.abs(v) >= 1e6 ? (v/1e6).toFixed(1)+"M" : Math.abs(v) >= 1e3 ? Math.round(v/1e3)+"k" : v;

async function drawChart() {
  const p = params(); p.set("dim", state.dim);
  const { data } = await (await fetch("/api/trend?" + p)).json();
  $("#chart-title").textContent =
    { year: "Patent families by year", field: "Top WIPO technology fields", country: "Top applicant countries" }[state.dim];
  const horizontal = state.dim !== "year";
  const labels = data.map(d => state.dim === "country" ? countryName(d.label) : d.label);
  const valueTicks = { font: { size: 11 }, callback: kfmt };
  const catTicks = { font: { size: 11 }, autoSkip: false };
  const cfg = {
    type: "bar",
    data: { labels,
            datasets: [{ data: data.map(d => d.n), backgroundColor: "#3257d0", borderRadius: 4,
                         maxBarThickness: horizontal ? 22 : 26 }] },
    options: {
      indexAxis: horizontal ? "y" : "x",
      plugins: { legend: { display: false },
                 tooltip: { callbacks: { label: c => fmt(horizontal ? c.parsed.x : c.parsed.y) + " families" } } },
      scales: horizontal
        ? { x: { grid: { color: "#eef0f6" }, ticks: valueTicks }, y: { grid: { display: false }, ticks: catTicks } }
        : { x: { grid: { display: false }, ticks: catTicks }, y: { grid: { color: "#eef0f6" }, ticks: valueTicks } },
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
    const coName = co ? co.name + (co.country ? `<span class="cc">${countryName(co.country)}</span>` : "") : "—";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="pub">${row.rep_publication ?? "—"}</td>
      <td>${row.filing_year}</td>
      <td class="field-tag">${row.primary_field_name ?? "—"}</td>
      <td>${coName}</td>
      <td class="num">${fmt(row.n_bwd_citations)}</td>`;
    tb.appendChild(tr);
  }
  const from = r.total ? state.offset + 1 : 0;
  $("#page").textContent = `${fmt(from)}–${fmt(Math.min(state.offset + state.limit, r.total))} of ${fmt(r.total)}`;
  $("#prev").disabled = state.offset === 0;
  $("#next").disabled = state.offset + state.limit >= r.total;
}

async function loadCompanies() {
  const p = params(); p.set("limit", 25);
  const r = await (await fetch("/api/companies?" + p)).json();
  const tb = $("#companies tbody"); tb.innerHTML = "";
  for (const c of r.rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${c.name}</td><td>${countryName(c.country_code) || "—"}</td><td class="num">${fmt(c.families)}</td>`;
    tb.appendChild(tr);
  }
}

// events
$("#apply").onclick = refresh;
$("#reset").onclick = () => {
  $("#f-field").value = "0"; $("#f-country").value = ""; $("#f-company").value = "";
  $("#f-y0").value = ""; $("#f-y1").value = ""; refresh();
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

boot();
