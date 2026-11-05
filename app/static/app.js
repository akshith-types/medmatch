const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const GROUPS = ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"];

const state = { hospitals: [], types: [], me: null, graph: null };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.message || body.detail || `Request failed (${res.status})`);
  return body;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), 4200);
}

const hName = (id) => state.hospitals.find((h) => h.hospital_id === id)?.hospital_name ?? `#${id}`;

/* ---------- boot ---------- */
async function boot() {
  [state.hospitals, state.types] = await Promise.all([api("/api/hospitals"), api("/api/asset-types")]);
  const opts = state.hospitals.map((h) => `<option value="${h.hospital_id}">${esc(h.hospital_name)}</option>`).join("");
  $("#actingAs").innerHTML = opts;
  $("#rFrom").innerHTML = opts;
  $("#rTo").innerHTML = opts;
  const saved = Number(localStorageSafe("get", "mm-me"));
  state.me = state.hospitals.some((h) => h.hospital_id === saved) ? saved
           : state.hospitals.find((h) => h.hospital_code === "KPD01")?.hospital_id ?? state.hospitals[0].hospital_id;
  $("#actingAs").value = state.me;
  $("#rTo").value = state.me;
  $("#rFrom").value = state.hospitals.find((h) => h.hospital_code === "BLR01")?.hospital_id ?? state.hospitals[1].hospital_id;

  const typeOpts = state.types.map((t) => `<option value="${t.asset_type_id}" data-cat="${t.category}">${esc(t.asset_type_name)}</option>`).join("");
  $("#fType").innerHTML = typeOpts;
  $("#pType").innerHTML = typeOpts;
  $("#fType").value = state.types.find((t) => t.asset_type_name === "Packed Red Cells")?.asset_type_id;
  $("#pType").value = $("#fType").value;
  $("#fBlood").innerHTML += GROUPS.map((g) => `<option>${g}</option>`).join("");
  $("#fBlood").value = "O-";
  $("#pBlood").innerHTML = GROUPS.map((g) => `<option>${g}</option>`).join("");
  $("#fHla").innerHTML += (await api("/api/hla-types")).map((h) => `<option>${esc(h)}</option>`).join("");

  refreshPulse();
  refreshHealth();
  search();
}

function localStorageSafe(op, k, v) {
  try { return op === "get" ? localStorage.getItem(k) : localStorage.setItem(k, v); } catch { return null; }
}

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    $("#health").innerHTML = `Database <b>${h.database}</b><br>Routing via <b>${h.graph_backend === "neo4j" ? "Neo4j" : "built-in fallback"}</b>`;
  } catch { $("#health").textContent = "Server unreachable"; }
}

async function refreshPulse() {
  const s = await api("/api/stats");
  $("#pulse").innerHTML = `
    <div><b>${s.available_units.toLocaleString()}</b><span>units available</span></div>
    <div class="rare"><b>${s.o_negative}</b><span>O-negative units</span></div>
    <div><b>${s.active_transfers}</b><span>transfers under way</span></div>
    <div><b>${s.completed_transfers}</b><span>delivered</span></div>
    <div><b>${s.hospitals}</b><span>hospitals linked</span></div>`;
}

/* ---------- navigation ---------- */
$$(".nav").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
function show(view) {
  $$(".nav").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === "view-" + view));
  ({ transfers: loadTransfers, network: drawMap, audit: loadAudit }[view] || (() => {}))();
}
$("#actingAs").addEventListener("change", (e) => {
  state.me = Number(e.target.value);
  localStorageSafe("set", "mm-me", state.me);
  $("#rTo").value = state.me;
  search();
  if ($("#view-transfers").classList.contains("on")) loadTransfers();
});

/* ---------- search ---------- */
$("#fType").addEventListener("change", () => {
  const cat = $("#fType").selectedOptions[0].dataset.cat;
  $("#bgWrap").hidden = cat === "ANTIVENOM";
  $("#hlaWrap").hidden = cat !== "ORGAN";
});
$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); search(); });

async function search() {
  const cat = $("#fType").selectedOptions[0]?.dataset.cat;
  const q = new URLSearchParams({ asset_type_id: $("#fType").value, requester_id: state.me, limit: 24 });
  if (cat !== "ANTIVENOM" && $("#fBlood").value) q.set("blood_group", $("#fBlood").value);
  if (cat === "ORGAN" && $("#fHla").value) q.set("hla_type", $("#fHla").value);
  if ($("#fDist").value) q.set("max_distance_km", $("#fDist").value);
  try {
    const r = await api("/api/search?" + q);
    $("#searchMeta").textContent = r.count
      ? `${r.count} compatible unit${r.count > 1 ? "s" : ""}, nearest first. Query took ${r.query_ms} ms.`
      : "";
    $("#results").innerHTML = r.count ? r.results.map(labelCard).join("")
      : `<div class="empty">No compatible unit is available in the network right now. Widen the distance, or try a compatible blood group.</div>`;
  } catch (err) { toast(err.message, true); }
}

function labelCard(a) {
  const type = $("#fType").selectedOptions[0].textContent;
  const hrs = Number(a.hours_to_expiry);
  const pct = Math.max(3, Math.min(100, (hrs / (24 * 14)) * 100));
  const cls = hrs < 24 ? "now" : hrs < 72 ? "soon" : "";
  const left = hrs < 48 ? `${hrs.toFixed(1)} h left` : `${Math.round(hrs / 24)} days left`;
  const g = a.blood_group || "—";
  const neg = g.endsWith("-");
  return `<article class="label">
    <div class="group ${neg ? "neg" : ""}">${esc(g)}<small>${a.blood_group ? (neg ? "Rh negative" : "Rh positive") : "No ABO"}</small></div>
    <div class="prod"><strong>${esc(type)}</strong><code>${esc(a.asset_code)}</code>${a.hla_type ? `<br><code>${esc(a.hla_type)}</code>` : ""}</div>
    <div class="where">${esc(a.hospital_name)}<br><em>${esc(a.city)}, ${Number(a.distance_km).toFixed(1)} km away</em></div>
    <div class="expiry">
      <div class="bar"><i class="${cls}" style="width:${pct}%"></i></div>
      <div class="row"><span>${left}</span>
        <button class="primary" data-reserve="${a.asset_id}" data-code="${esc(a.asset_code)}" data-hosp="${esc(a.hospital_name)}">Reserve</button></div>
    </div></article>`;
}

$("#results").addEventListener("click", (e) => {
  const b = e.target.closest("[data-reserve]");
  if (!b) return;
  const dlg = $("#confirm");
  $("#confirmBody").innerHTML = `<p><b>${b.dataset.code}</b> from ${b.dataset.hosp}, for ${esc(hName(state.me))}.</p>`;
  dlg.returnValue = "";
  dlg.showModal();
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    try {
      const r = await api("/api/reserve", { method: "POST", body: JSON.stringify({
        asset_id: Number(b.dataset.reserve), requester_id: state.me,
        department: $("#cDept").value, contact_person: $("#cPerson").value, urgency_level: $("#cUrg").value }) });
      const eta = r.route ? `, ETA ${r.route.total_minutes} min via ${r.route.hospital_ids.length - 1} leg(s)` : "";
      toast(`Reserved ${r.asset_code} as transfer #${r.transaction_id}${eta}`);
      refreshPulse();
      search();
    } catch (err) { toast(err.message, true); search(); }
  };
});

/* ---------- transfers ---------- */
$("#allTransfers").addEventListener("change", loadTransfers);
async function loadTransfers() {
  const all = $("#allTransfers").checked;
  const rows = await api("/api/transactions" + (all ? "" : `?hospital_id=${state.me}`));
  $("#txTable tbody").innerHTML = rows.length ? rows.map((t) => `<tr>
      <td>${t.transaction_id}</td>
      <td><b>${esc(t.asset_type_name)}</b> ${esc(t.blood_group || "")}<br><span class="hops">${esc(t.asset_code)}</span></td>
      <td>${esc(t.supplying_hospital)}</td>
      <td>${esc(t.requesting_hospital)}<br><span class="hops">${esc(t.department)}, ${esc(t.urgency_level.toLowerCase())}</span></td>
      <td>${t.eta_minutes != null ? t.eta_minutes + " min" : "—"}<br><span class="hops">${(t.route_path || []).map(hName).join(" → ")}</span></td>
      <td><span class="pill ${t.status}">${t.status.replace("_", " ").toLowerCase()}</span></td>
      <td><div class="acts">${actions(t)}</div></td></tr>`).join("")
    : `<tr><td colspan="7">No transfers yet. Reserve a unit from Find and reserve to start one.</td></tr>`;
}
function actions(t) {
  const b = (a, l) => `<button data-act="${a}" data-id="${t.transaction_id}">${l}</button>`;
  if (t.status === "ALLOCATED") return b("dispatch", "Dispatch") + b("complete", "Mark delivered") + b("cancel", "Cancel");
  if (t.status === "IN_TRANSIT") return b("complete", "Mark delivered");
  return "";
}
$("#txTable").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  try {
    await api(`/api/transactions/${b.dataset.id}/${b.dataset.act}`, { method: "POST" });
    toast({ dispatch: "Dispatched", complete: "Marked delivered", cancel: "Cancelled; unit returned to the pool" }[b.dataset.act] + ` (#${b.dataset.id})`);
    loadTransfers(); refreshPulse();
  } catch (err) { toast(err.message, true); }
});

/* ---------- network map ---------- */
async function drawMap(hot) {
  if (!state.graph) state.graph = await api("/api/graph");
  const { nodes, edges } = state.graph;
  const W = 980, H = 560, pad = 60;
  // Schematic positions (west to east, like a transit map) so dense city clusters stay readable.
  const LAYOUT = { BLR01: [70, 250], BLR02: [190, 180], HSR01: [160, 380], KRG01: [310, 450], AMB01: [400, 300],
    KPD01: [500, 150], VLR02: [560, 220], VLR01: [520, 300], RNP01: [680, 200], ARC01: [660, 320],
    TVM01: [560, 490], KNC01: [770, 420], TPT01: [650, 60], CHN03: [880, 110], CHN01: [900, 230], CHN02: [880, 360] };
  const lats = nodes.map((n) => n.lat), lons = nodes.map((n) => n.lon);
  const [minLa, maxLa, minLo, maxLo] = [Math.min(...lats), Math.max(...lats), Math.min(...lons), Math.max(...lons)];
  const x = (lo) => pad + ((lo - minLo) / (maxLo - minLo)) * (W - pad * 2);
  const y = (la) => H - pad - ((la - minLa) / (maxLa - minLa)) * (H - pad * 2);
  const pos = Object.fromEntries(nodes.map((n) => [n.id, LAYOUT[n.code] || [x(n.lon), y(n.lat)]]));
  const hotPairs = new Set();
  if (hot) for (let i = 0; i < hot.length - 1; i++) hotPairs.add(`${hot[i]}-${hot[i + 1]}`);
  const seen = new Set();
  let svg = "";
  for (const e of edges) {
    const k = [Math.min(e.source, e.target), Math.max(e.source, e.target), e.mode].join("-");
    if (seen.has(k)) continue;
    seen.add(k);
    const isHot = hotPairs.has(`${e.source}-${e.target}`) || hotPairs.has(`${e.target}-${e.source}`);
    const [x1, y1] = pos[e.source], [x2, y2] = pos[e.target];
    svg += `<line class="edge ${e.mode === "AIR" ? "air" : ""} ${isHot ? "hot" : ""}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"><title>${e.mode} ${e.eta_minutes} min</title></line>`;
  }
  // draw hot edges last so they sit on top
  svg = svg.replace(/(<line class="edge[^"]*hot[^>]*>.*?<\/line>)/g, "") + (svg.match(/<line class="edge[^"]*hot[^>]*>.*?<\/line>/g) || []).join("");
  const hotSet = new Set(hot || []);
  for (const n of nodes) {
    const [cx, cy] = pos[n.id];
    const right = cx < W - 160;
    svg += `<g class="node ${hotSet.has(n.id) ? "hot" : ""}"><circle cx="${cx}" cy="${cy}" r="${n.facility_type === "BLOOD_BANK" || n.facility_type === "TRANSPLANT_CENTER" ? 8 : 6}"/>
      <text x="${cx + (right ? 12 : -12)}" y="${cy - 2}" text-anchor="${right ? "start" : "end"}">${esc(n.code)}</text>
      <text class="city" x="${cx + (right ? 12 : -12)}" y="${cy + 12}" text-anchor="${right ? "start" : "end"}">${esc(n.city)}</text>
      <title>${esc(n.name)}</title></g>`;
  }
  $("#map").setAttribute("viewBox", `0 0 ${W} ${H}`);
  $("#map").innerHTML = svg;
}
$("#routeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api(`/api/route?src=${$("#rFrom").value}&dst=${$("#rTo").value}&cold_chain=${$("#rCold").checked}`);
    const legs = r.hospital_names.map((n, i) => i === 0 ? n : `${r.modes[i - 1].toLowerCase()} ${r.leg_minutes[i - 1]} min → ${n}`).join(", ");
    $("#routeMeta").innerHTML = `<b>${r.total_minutes} min</b> door to door${r.cold_chain_required && !r.cold_chain_satisfied ? " (no fully refrigerated corridor exists; showing fastest standard route)" : ""}. ${esc(legs)}. Solved by ${r.source === "neo4j" ? "Neo4j Dijkstra" : "the built-in Dijkstra fallback"}.`;
    drawMap(r.hospital_ids);
  } catch (err) { toast(err.message, true); }
});

/* ---------- concurrency lab ---------- */
$$("[data-demo]").forEach((b) => b.addEventListener("click", async () => {
  $$("[data-demo]").forEach((x) => (x.disabled = true));
  $("#labOut").innerHTML = `<div class="verdict">Running both transactions…</div>`;
  try {
    const kind = b.dataset.demo, flag = b.dataset.flag;
    const r = await api(`/api/demo/${kind}?${kind === "race" ? "safe" : "wait_die"}=${flag}`, { method: "POST" });
    $("#labOut").innerHTML = (kind === "race" ? raceVerdict(r) : deadlockVerdict(r)) + timeline(r.trace);
    refreshPulse();
  } catch (err) { $("#labOut").innerHTML = `<div class="verdict bad">${esc(err.message)}</div>`; }
  $$("[data-demo]").forEach((x) => (x.disabled = false));
}));

function raceVerdict(r) {
  if (r.lost_update)
    return `<div class="verdict bad">Lost update. Both hospitals read the unit as available and both overwrote it. Only the database's one-active-claim constraint stopped a double allocation; one hospital was told it had the unit when it did not.</div>`;
  return `<div class="verdict good">Exactly ${r.winners} winner. The second hospital re-read the row under lock, saw it reserved, and was turned away cleanly. Final state: ${esc(r.final_asset_state.status)}, ${r.final_asset_state.active_claims} active claim.</div>`;
}
function deadlockVerdict(r) {
  if (r.deadlock_occurred)
    return `<div class="verdict bad">Deadlock. Each transaction held one unit and waited for the other. PostgreSQL detected the cycle only after its timeout and killed a victim; total ${r.total_ms} ms.</div>`;
  return `<div class="verdict good">No deadlock. The younger transaction was aborted the moment it would have waited on an older one, so no cycle could form. Total ${r.total_ms} ms.</div>`;
}
function timeline(trace) {
  const lanes = {};
  for (const e of trace) (lanes[e.who] ||= []).push(e);
  return `<div class="timeline">${Object.entries(lanes).map(([who, evs]) => {
    const [name, ...rest] = who.split(" (");
    return `<div class="lane"><h3>${esc(name)}<small>${rest.length ? esc("(" + rest.join(" (")) : ""}</small></h3><div>${evs.map((e) => {
      const m = e.msg;
      const cls = /DIE/.test(m) ? "die" : /DEADLOCK/.test(m) ? "dead" : /^WAIT/.test(m) ? "wait" : /COMMIT/.test(m) ? "commit" : "";
      return `<div class="ev ${cls}"><b>${e.ms} ms</b><span>${esc(m)}</span></div>`;
    }).join("")}</div></div>`;
  }).join("")}</div>`;
}

/* ---------- plans ---------- */
$("#planForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api(`/api/explain?asset_type_id=${$("#pType").value}&blood_group=${encodeURIComponent($("#pBlood").value)}`);
  $("#planOut").innerHTML = r.plan.map((l) => esc(l).replace(/(idx_[a-z_]+)/g, '<span class="idx">$1</span>')).join("\n");
});

/* ---------- audit ---------- */
async function loadAudit() {
  const rows = await api("/api/audit?limit=150");
  $("#auditTable tbody").innerHTML = rows.map((a) => `<tr>
    <td>${new Date(a.log_time).toLocaleString()}</td><td>${esc(a.action.replace("_", " ").toLowerCase())}</td>
    <td>${a.transaction_id ?? "—"}</td>
    <td>${a.old_status || a.new_status ? `${esc(a.old_status || "new")} → ${esc(a.new_status || "")}` : "—"}</td>
    <td class="hops">${esc(a.details ? JSON.stringify(a.details) : "")}</td></tr>`).join("")
    || `<tr><td colspan="5">Nothing logged yet.</td></tr>`;
}

boot().catch((err) => toast("Could not load: " + err.message, true));
