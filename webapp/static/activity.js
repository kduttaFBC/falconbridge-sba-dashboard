/* Lending Activity tab (Phase 1). Renders payload.activity (webapp/lending_activity.py) and the existing bank
   industry rows. Display only: every figure comes from the server; the browser formats and arranges it. */
(function () {
  "use strict";
  const BLUE = "#2a78d6", NAVY = "#1f4e79", ORANGE = "#eb6834", GREY = "#8a8984", MUTED = "#5d6b7a", GRID = "#e9edf1";
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const money = (v) => !isNum(v) ? "–" : Math.abs(v) >= 1e9 ? "$" + (v / 1e9).toFixed(2) + "B" : Math.abs(v) >= 1e6
    ? "$" + (v / 1e6).toFixed(1) + "M" : Math.abs(v) >= 1e3 ? "$" + (v / 1e3).toFixed(0) + "K" : "$" + v.toFixed(0);
  const exact = (v) => isNum(v) ? "$" + v.toLocaleString("en-US", {maximumFractionDigits: 0}) : "–";
  const pct = (v) => isNum(v) ? (v * 100).toFixed(2) + "%" : "–";
  const chg = (v) => isNum(v) ? (v >= 0 ? "+" : "−") + Math.abs(v * 100).toFixed(1) + "%" : "–";
  const int = (v) => isNum(v) ? v.toLocaleString("en-US") : "–";
  const SMALL = 10;
  let P, root, charts = {}, state = {period: "annual", measure: "amount", heat: "gross", geo: "project", ch: "processing"};
  let lastDrill = null;

  function chart(id, cfg) {
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(document.getElementById(id), cfg);
  }
  const base = (fmt, extra) => Object.assign({
    responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: "index", intersect: false},
    plugins: {legend: {position: "top", align: "start", labels: {boxWidth: 10, boxHeight: 10}},
      tooltip: {callbacks: {label: (c) => `${c.dataset.label}: ${fmt(c.raw)}`}}},
    scales: {x: {grid: {display: false}, ticks: {color: MUTED, maxRotation: 0, autoSkip: true}},
             y: {grid: {color: GRID}, border: {display: false}, ticks: {color: MUTED, callback: (v) => fmt(v)}, beginAtZero: true}}
  }, extra || {});
  function seg(id, key, onchange) {
    root.querySelectorAll(`#${id} button`).forEach((b) => b.addEventListener("click", () => {
      state[key] = b.dataset.v;
      root.querySelectorAll(`#${id} button`).forEach((x) => x.classList.toggle("on", x === b));
      onchange();
    }));
  }
  function table(head, rows, cls) {
    return `<table class="data ${cls || ""}"><thead><tr>${head.map((h, i) => `<th class="${i ? "num" : ""}">${h}</th>`).join("")}</tr></thead><tbody>` +
      rows.map((r) => `<tr class="${r.cls || ""}"${r.drill ? ` data-dim="${esc(r.drill[0])}" data-val="${esc(r.drill[1])}" tabindex="0" title="Show the loans behind this row"` : ""}>` +
        r.cells.map((x, i) => `<td class="${i ? "num" : ""}">${x}</td>`).join("") + "</tr>").join("") + "</tbody></table>";
  }
  function bindDrill(el) {
    el.querySelectorAll("[data-dim]").forEach((r) => {
      const go = () => drill(r.dataset.dim, r.dataset.val);
      r.addEventListener("click", go);
      r.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    });
  }
  const small = (n) => n > 0 && n < SMALL ? ' <span class="tag-s">small sample</span>' : "";

  // ------------------------------------------------------------------------------------------- summary tiles
  function summary() {
    const A = P.activity, T = A.totals, C = P.criteria;
    const R = A.recent, L4 = R.last4;
    const peak = A.annual.reduce((a, m) => (m.gross > (a ? a.gross : -1) ? m : a), null);
    const tiles = [
      ["Loans originated", int(T.loans), `first disbursed ${A.annual[0].period}–${A.annual[A.annual.length - 1].period}`],
      ["Gross original amount", money(T.gross), exact(T.gross)],
      ["Original unguaranteed", money(T.unguar), `${pct(T.unguar_share)} of gross &middot; not a current balance`],
      ["Last four quarters", L4 ? money(L4.gross) : "–", L4 ? `${esc(L4.from)} – ${esc(L4.to)} &middot; ${money(L4.unguar)} unguaranteed &middot; ${R.prior4 ? chg(R.gross_change) + " gross vs the prior four" : "no prior period"}` : ""],
      ["Largest origination year", peak ? String(peak.period) : "–", peak ? `${money(peak.gross)} gross &middot; ${int(peak.loans)} loans` : ""],
      ["Loans now in default status", int(T.default_loans), `${pct(T.cum_def)} of gross (% Cum. Defaulted, full history)`],
    ];
    root.querySelector("#act-kpis").innerHTML = tiles.map(([l, v, s]) =>
      `<div class="kpi"><div class="k-label">${l}</div><div class="k-val">${v}</div><div class="k-sub">${s}</div></div>`).join("");
    root.querySelector("#act-pop").textContent = `${C.bank_name}, ${C.bank_state} · SBA FOIA data as of ${A.as_of}`;
    root.querySelector("#act-basis").innerHTML = "Population: the lender's eligible SBA 7(a) loans in the release (all approval years), as in the Performance Summary. " +
      "Original unguaranteed = gross approval − SBA-guaranteed approval at origination; SBA FOIA data contains no current or outstanding balances. " +
      "Default status = LoanStatus CHGOFF or CLSLN." + (A.partial_year ? ` ${A.as_of.slice(0, 4)} is a partial year.` : "") +
      " Growth figures compare with the immediately preceding period; a partial year (*) has none.";
  }

  // ------------------------------------------------------------------------------------------- originations
  function originations() {
    const A = P.activity, rows = state.period === "annual" ? A.annual : A.quarterly;
    const label = (m) => (state.period === "annual" ? String(m.period) : m.label) + (m.partial ? "*" : "");
    const dim = state.period === "annual" ? "year" : "quarter";
    const cfg = state.measure === "amount" ? {
      type: "bar",
      data: {labels: rows.map(label), datasets: [
        {label: "SBA-guaranteed", data: rows.map((m) => m.guar), backgroundColor: "#9fbfe0", stack: "g", borderRadius: 2},
        {label: "Original unguaranteed", data: rows.map((m) => m.unguar), backgroundColor: NAVY, stack: "g", borderRadius: 2}]},
      options: base(money, {scales: {x: {stacked: true, grid: {display: false}, ticks: {color: MUTED, autoSkip: true, maxRotation: 0}},
        y: {stacked: true, grid: {color: GRID}, border: {display: false}, ticks: {color: MUTED, callback: (v) => money(v)}}}})
    } : {
      type: "bar",
      data: {labels: rows.map(label), datasets: [{label: "Loans originated", data: rows.map((m) => m.loans), backgroundColor: BLUE, borderRadius: 2}]},
      options: base(int)
    };
    cfg.options.onClick = (e, els) => { if (els.length) drill(dim, String(rows[els[0].index].period)); };
    cfg.options.onHover = (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; };
    chart("act-chart", cfg);
    const shown = state.period === "quarterly" ? rows.filter((m) => m.loans || m.default_loans) : rows;
    root.querySelector("#act-table").innerHTML = table(
      [state.period === "annual" ? "Year" : "Quarter", "Loans", "Gross", "SBA-guaranteed", "Original unguaranteed", "Unguaranteed %",
       "Average loan", "Gross vs prior", "Now in default status", "% Cum. Defaulted"],
      shown.slice().reverse().map((m) => ({drill: [dim, String(m.period)], cells: [esc(label(m)), int(m.loans), money(m.gross), money(m.guar),
        money(m.unguar), pct(m.unguar_share), money(m.avg), chg(m.gross_change), int(m.default_loans), pct(m.cum_def) + small(m.loans)]})), "act-click");
    bindDrill(root.querySelector("#act-table"));
  }

  // ------------------------------------------------------------------------------------------- cohorts vs events
  function cohortsEvents() {
    const A = P.activity, years = A.annual.filter((m) => m.loans);
    const cohortOpts = base(pct);
    cohortOpts.plugins.legend = {display: false};
    cohortOpts.onClick = (e, els) => { if (els.length) drill("year", String(years[els[0].index].period)); };
    chart("act-cohort", {type: "bar", data: {labels: years.map((m) => String(m.period) + (m.partial ? "*" : "")), datasets: [
      {label: "% Cum. Defaulted", data: years.map((m) => m.cum_def), backgroundColor: years.map((m) => m.loans < SMALL ? "#c9d6e3" : BLUE), borderRadius: 2}]},
      options: cohortOpts});
    const ev = A.chargeoff_events.annual;
    chart("act-events", {type: "bar", data: {labels: ev.map((e) => String(e.period)), datasets: [
      {label: "Gross charge-off amount", data: ev.map((e) => e.gross_chargeoff), backgroundColor: ORANGE, borderRadius: 2, yAxisID: "y"},
      {label: "Loans charged off", data: ev.map((e) => e.loans), type: "line", borderColor: NAVY, backgroundColor: NAVY, yAxisID: "y1", pointRadius: 3}]},
      options: Object.assign(base(money), {
        plugins: {legend: {position: "top", align: "start", labels: {boxWidth: 10, boxHeight: 10}},
          tooltip: {callbacks: {label: (c) => c.dataset.yAxisID === "y1" ? `${c.dataset.label}: ${int(c.raw)}` : `${c.dataset.label}: ${money(c.raw)}`}}},
        scales: {x: {grid: {display: false}, ticks: {color: MUTED}},
          y: {grid: {color: GRID}, ticks: {color: MUTED, callback: (v) => money(v)}, beginAtZero: true},
          y1: {position: "right", grid: {display: false}, ticks: {color: MUTED, precision: 0}, beginAtZero: true}},
        onClick: (e, els) => { if (els.length) drill("chargeoff_year", String(ev[els[0].index].period)); }})});
    const E = A.chargeoff_events;
    root.querySelector("#act-events-note").innerHTML = "Left: the share of each year's originations (by original amount) that is now in default status; light bars have fewer than 10 loans. " +
      "Right: when charge-offs happened, by charge-off date, whatever the loan's origination year. The two are different populations and are not a rate of each other. " +
      (E.undated.loans ? `${int(E.undated.loans)} loan(s) in default status have no charge-off date and are not shown by year. ` : "") +
      (E.dated_not_in_default_status ? `${int(E.dated_not_in_default_status)} loan(s) carry a charge-off date but are not in default status and are excluded. ` : "") +
      "No unguaranteed share of losses is calculated (FalconBridge has not confirmed a method).";
  }

  // ------------------------------------------------------------------------------------------- industry
  function industry() {
    const rows = P.bank.full.total.industry.filter((r) => r.loans > 0).sort((a, b) => b.orig - a.orig);
    const share = Object.fromEntries(P.activity.industry_year.industries.map((r) => [r.name, r.unguar_share]));
    const ind = (n) => n === "N/A" ? "Not classified (no NAICS code)" : n;
    root.querySelector("#act-ind").innerHTML = table(
      ["Industry", "Loans", "Gross", "Original unguaranteed", "Unguaranteed %", "Share of gross", "% Cum. Defaulted"],
      rows.slice(0, 15).map((r) => ({drill: ["industry", r.name], cells: [esc(ind(r.name)) + small(r.loans), int(r.loans), money(r.orig), money(r.unguar),
        pct(share[r.name]), pct(r.share), pct(r.cum_def)]})), "act-click");
    bindDrill(root.querySelector("#act-ind"));
    heatmap();
    const C = P.activity.concentration_by_year.filter((c) => c.gross > 0);
    chart("act-conc", {type: "line", data: {labels: C.map((c) => String(c.year)), datasets: [
      {label: "Largest industry", data: C.map((c) => c.top1), borderColor: NAVY, backgroundColor: NAVY, pointRadius: 2},
      {label: "Largest five industries", data: C.map((c) => c.top5), borderColor: BLUE, backgroundColor: BLUE, borderDash: [5, 4], pointRadius: 2}]},
      options: Object.assign(base(pct), {scales: {x: {grid: {display: false}, ticks: {color: MUTED}}, y: {min: 0, max: 1, grid: {color: GRID}, ticks: {color: MUTED, callback: (v) => pct(v)}}}})});
    const last = C[C.length - 1];
    root.querySelector("#act-conc-note").innerHTML = last ? `<p><strong>${esc(String(last.year))}:</strong> the largest industry holds ${pct(last.top1)} of that year's gross originations and the largest five ${pct(last.top5)}, across ${int(last.industries)} industries.</p><p>Years with few loans swing widely; read the trend together with the originations chart.</p>` : "";
  }
  function heatmap() {
    const IY = P.activity.industry_year, years = IY.years.filter((y) => P.activity.annual.some((m) => m.period === y && m.loans));
    const top = IY.industries.slice(0, 12), key = state.heat;
    const max = Math.max(1, ...top.flatMap((r) => years.map((y) => (r.cells[String(y)] || {})[key] || 0)));
    const fmt = key === "loans" ? int : money;
    const head = `<tr><th>Industry</th>${years.map((y) => `<th class="num">${y}</th>`).join("")}<th class="num">Total</th></tr>`;
    const body = top.map((r) => `<tr><td class="hm-name" title="${esc(r.name)}">${esc(r.name)}</td>` + years.map((y) => {
      const c = r.cells[String(y)], v = c ? c[key] : 0, a = v ? 0.08 + 0.72 * (v / max) : 0;
      return c ? `<td class="num hm" style="background: rgba(31,78,121,${a.toFixed(3)}); color: ${a > 0.45 ? "#fff" : "inherit"}" data-dim="industry_year" data-val="${esc(r.name + "|" + y)}" tabindex="0" title="${esc(r.name)}, ${y}: ${fmt(v)}">${fmt(v)}</td>` : `<td class="num hm-empty"></td>`;
    }).join("") + `<td class="num"><strong>${fmt(r[key])}</strong></td></tr>`).join("");
    const el = root.querySelector("#act-heat");
    el.innerHTML = `<table class="data act-heat"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    bindDrill(el);
  }

  // ------------------------------------------------------------------------------------------- geography
  function geography() {
    const G = P.activity.geography[state.geo], dim = state.geo === "project" ? "project_state" : "borrower_state";
    const top = G.slice(0, 12);
    chart("act-geo-chart", {type: "bar", data: {labels: top.map((s) => s.state), datasets: [
      {label: "SBA-guaranteed", data: top.map((s) => s.guar), backgroundColor: "#9fbfe0", stack: "g"},
      {label: "Original unguaranteed", data: top.map((s) => s.unguar), backgroundColor: NAVY, stack: "g"}]},
      options: Object.assign(base(money), {indexAxis: "y", scales: {x: {stacked: true, grid: {color: GRID}, ticks: {color: MUTED, callback: (v) => money(v)}},
        y: {stacked: true, grid: {display: false}, ticks: {color: MUTED}}}, onClick: (e, els) => { if (els.length) drill(dim, top[els[0].index].state); }})});
    root.querySelector("#act-geo-table").innerHTML = table(["State", "Loans", "Gross", "Original unguaranteed", "Share of gross", "In default status", "% Cum. Defaulted"],
      G.map((s) => ({drill: [dim, s.state], cells: [esc(s.state) + small(s.loans), int(s.loans), money(s.gross), money(s.unguar), pct(s.share), int(s.default_loans), pct(s.cum_def)]})), "act-click");
    bindDrill(root.querySelector("#act-geo-table"));
    const home = P.criteria.bank_state, inHome = G.find((s) => s.state === home);
    root.querySelector("#act-geo-note").innerHTML = (state.geo === "project" ? "Project state: where the financed business is located (FOIA ProjectState)." :
      "Borrower state: the borrower's address state (FOIA BorrState).") + " The two are separate populations and are never combined. " +
      (inHome ? `The bank's own state (${esc(home)}) holds ${pct(inHome.share)} of gross originations on this basis.` : `No loans in the bank's own state (${esc(home)}) on this basis.`);
  }

  // ------------------------------------------------------------------------------------------- characteristics
  function characteristics() {
    const CH = P.activity.characteristics, keys = Object.keys(CH);
    const nav = root.querySelector("#act-char-nav");
    nav.innerHTML = keys.map((k) => `<button type="button" data-k="${k}" class="${k === state.ch ? "on" : ""}">${esc(CH[k].label)}</button>`).join("");
    nav.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { state.ch = b.dataset.k; characteristics(); }));
    const c = CH[state.ch];
    root.querySelector("#act-char").innerHTML = table([esc(c.label), "Loans", "Gross", "Original unguaranteed", "Unguaranteed %", "Share of gross", "% Cum. Defaulted"],
      c.items.map((i) => ({drill: [state.ch, i.value], cells: [esc(i.value) + small(i.loans), int(i.loans), money(i.gross), money(i.unguar), pct(i.unguar_share), pct(i.share), pct(i.cum_def)]})), "act-click");
    bindDrill(root.querySelector("#act-char"));
    root.querySelector("#act-char-note").textContent = state.ch === "size_band" ?
      "Loan size bands are an application grouping of the original gross amount (not a FalconBridge rule); the $350K benchmark threshold remains on the Benchmark Analysis tab." :
      `Values exactly as published in the FOIA field ${c.field}.`;
  }

  // ------------------------------------------------------------------------------------------- drill-down
  function drill(dim, value) {
    const url = root.dataset.drill + new URLSearchParams({dim: dim, value: value}).toString();
    const m = root.querySelector("#act-modal");
    root.querySelector("#act-modal-title").textContent = "Loading…";
    root.querySelector("#act-modal-table").innerHTML = "";
    root.querySelector("#act-modal-totals").innerHTML = "";
    root.querySelector("#act-modal-rec").textContent = "";
    m.hidden = false;
    fetch(url, {credentials: "same-origin"}).then((r) => r.ok ? r.json() : r.text().then((t) => Promise.reject(t))).then((j) => {
      lastDrill = j;
      root.querySelector("#act-modal-title").textContent = `${j.bank}: loans ${j.selection}`;
      root.querySelector("#act-modal-pop").textContent = `Population: ${j.population}. SBA FOIA data as of ${j.as_of || P.criteria.as_of}. Amounts: ${j.amounts}.`;
      const rec = root.querySelector("#act-modal-rec");
      rec.className = "act-badge " + (j.reconciles ? "ok" : j.displayed ? "bad" : "na");
      rec.textContent = j.reconciles ? "Reconciles to the displayed figure" : j.displayed ? "Does not reconcile: review" : "No stored figure to reconcile";
      const T = j.totals;
      root.querySelector("#act-modal-totals").innerHTML = `<strong>${int(T.loans)}</strong> loans &middot; gross <strong>${exact(T.gross)}</strong> &middot; original unguaranteed <strong>${exact(T.unguar)}</strong> &middot; in default status <strong>${int(T.default_loans)}</strong> &middot; gross charge-offs <strong>${exact(T.gross_chargeoff)}</strong>`;
      const money0 = new Set(["GrossApproval", "SBAGuaranteedApproval", "unguar", "GrossChargeOffAmount"]);
      root.querySelector("#act-modal-table").innerHTML = `<table class="data act-loan-table"><thead><tr>${j.columns.map(([k, l]) => `<th class="${money0.has(k) ? "num" : ""}">${esc(l)}</th>`).join("")}</tr></thead><tbody>` +
        j.loans.map((r) => `<tr class="${r.default_status ? "act-def" : ""}">` + j.columns.map(([k]) => {
          const v = r[k];
          return money0.has(k) ? `<td class="num">${exact(typeof v === "number" ? v : parseFloat(v || 0))}</td>` : `<td>${esc(v == null ? "" : v)}</td>`;
        }).join("") + "</tr>").join("") + "</tbody></table>";
    }).catch((e) => { root.querySelector("#act-modal-title").textContent = "The loans could not be loaded."; root.querySelector("#act-modal-pop").textContent = String(e).slice(0, 200); });
  }
  function csv() {
    if (!lastDrill) return;
    const q = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
    const lines = [lastDrill.columns.map(([, l]) => q(l)).join(",")].concat(lastDrill.loans.map((r) => lastDrill.columns.map(([k]) => q(r[k])).join(",")));
    const blob = new Blob([lines.join("\n")], {type: "text/csv"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${lastDrill.bank.replace(/[^A-Za-z0-9]+/g, "_")}_${lastDrill.dimension}_${String(lastDrill.value).replace(/[^A-Za-z0-9]+/g, "_")}_loans.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // ------------------------------------------------------------------------------------------- entry point
  let wired = false;
  function render(payload) {
    P = payload;
    root = document.getElementById("tab-activity");
    const has = P.activity && !P.activity.empty;
    root.querySelector("#act-missing").hidden = !!has;
    root.querySelector("#act-body").hidden = !has;
    if (!has) { root.querySelector("#act-live-link").href = root.dataset.live; return; }
    if (!wired) {
      seg("act-period", "period", originations);
      seg("act-measure", "measure", originations);
      seg("act-heat-m", "heat", heatmap);
      seg("act-geo", "geo", geography);
      root.querySelector("#act-modal-close").addEventListener("click", () => { root.querySelector("#act-modal").hidden = true; });
      root.querySelector("#act-modal").addEventListener("click", (e) => { if (e.target.id === "act-modal") e.target.hidden = true; });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") root.querySelector("#act-modal").hidden = true; });
      root.querySelector("#act-modal-csv").addEventListener("click", csv);
      wired = true;
    }
    summary(); originations(); cohortsEvents(); industry(); geography(); characteristics();
  }
  window.FBXActivity = {render: render};
})();
