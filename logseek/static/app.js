// logseek web page: file strip with binary-search animation, day histograms, search.

const $ = (id) => document.getElementById(id);
const fmt = (n) => n.toLocaleString("en-US");
const mb = (b) => `${(b / 1024 ** 2).toFixed(1)} MB`;
const TIME_RE = /^(\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?|\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?)?)$/;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => null);
  if (!r.ok) {
    const d = body && body.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `HTTP ${r.status}`);
  }
  return body;
}

let info = null;
let hourOffsets = []; // [{label, first}] from the day histogram, for strip ticks

// ---- file strip ------------------------------------------------------------------

const S = { left: 20, right: 980, top: 26, height: 44 };
const strip = d3.select("#strip");
const xb = () => d3.scaleLinear().domain([0, info.size]).range([S.left, S.right]);
let animation = 0; // bumps on every new search, so an old animation stops

function drawStripBase() {
  const x = xb();
  strip.selectAll("*").remove();
  strip.append("rect").attr("class", "strip-bg").attr("x", S.left).attr("y", S.top)
    .attr("width", S.right - S.left).attr("height", S.height).attr("rx", 4);
  strip.append("rect").attr("class", "strip-range").attr("id", "range")
    .attr("y", S.top).attr("height", S.height).attr("x", S.left).attr("width", 0);
  strip.append("g").attr("id", "jumps-g");
  strip.append("rect").attr("class", "strip-window").attr("id", "window")
    .attr("y", S.top).attr("height", S.height).attr("width", 0);
  const ticks = strip.append("g");
  hourOffsets.forEach((h, i) => {
    const px = x(h.first);
    ticks.append("line").attr("class", "strip-tick").attr("x1", px).attr("x2", px)
      .attr("y1", S.top).attr("y2", S.top + S.height);
    if (i % 3 === 0) {
      ticks.append("text").attr("class", "strip-label").attr("x", px).attr("y", S.top + S.height + 16).text(h.label);
    }
  });
  strip.append("text").attr("class", "strip-end").attr("x", S.left).attr("y", 16).text("byte 0");
  strip.append("text").attr("class", "strip-end").attr("x", S.right).attr("y", 16)
    .attr("text-anchor", "end").text(`byte ${fmt(info.size)}`);
}

function animateSearch(res, target) {
  const run = ++animation;
  const x = xb();
  const probes = res.probes;
  const g = strip.select("#jumps-g");
  g.selectAll("*").remove();
  strip.select("#window").attr("width", 0);
  $("jumps").innerHTML = probes.map((p, i) =>
    `<li>byte ${fmt(p.pos)} → ${esc(p.timestamp || "end of file")} → ${p.went === "right" ? "too early, go right" : "late enough, go left"}</li>`
  ).join("");

  if (!probes.length) {
    $("jump-text").textContent = "No start time: the window starts at byte 0.";
    showWindow(res, x);
    return;
  }
  const step = Math.max(60, Math.min(180, 3200 / probes.length));
  probes.forEach((p, i) => {
    setTimeout(() => {
      if (run !== animation) return;
      strip.select("#range").transition().duration(step * 0.8)
        .attr("x", x(p.lo)).attr("width", Math.max(1, x(p.hi) - x(p.lo)));
      g.selectAll("line").attr("class", "strip-jump");
      g.append("line").attr("class", "strip-jump now")
        .attr("x1", x(p.pos)).attr("x2", x(p.pos)).attr("y1", S.top - 6).attr("y2", S.top + S.height + 6);
      $("jump-text").innerHTML =
        `Jump <b>${i + 1}</b> of ${probes.length}: byte ${fmt(p.pos)} → <b>${esc(p.timestamp || "end of file")}</b> → ` +
        (p.went === "right" ? `earlier than ${esc(target)}, keep the right half` : `not earlier than ${esc(target)}, keep the left half`);
      if (i === probes.length - 1) setTimeout(() => run === animation && showWindow(res, x), step);
    }, i * step);
  });
}

function showWindow(res, x) {
  const s = res.stats;
  strip.select("#range").transition().duration(300).attr("width", 0);
  strip.select("#window").attr("x", x(s.first)).attr("width", Math.max(2, x(s.last) - x(s.first)));
  $("jump-text").innerHTML =
    `Found byte <b>${fmt(s.first)}</b> after <b>${res.probes.length}</b> jumps. ` +
    `Window: ${mb(s.window_bytes)} of ${mb(info.size)} (blue). Search took ${s.search_ms} ms.`;
}

// ---- histograms ------------------------------------------------------------------

const H = { left: 44, right: 990, top: 8 };
let histStack = []; // zoom history: [{start, end}]

async function loadHistogram(start, end, buckets) {
  const q = new URLSearchParams({ buckets });
  if (start) q.set("start", start);
  if (end) q.set("end", end);
  const data = await api(`/api/histogram?${q}`);
  drawHistograms(data);
  return data;
}

function shortTime(stamp, spanMs) {
  const t = stamp.slice(11);
  // spanMs = one bucket: label with as much detail as the bucket size needs
  return spanMs >= 60e3 ? t.slice(0, 5) : spanMs >= 1e3 ? t.slice(0, 8) : t;
}

function drawHistograms(data) {
  const rows = data.buckets;
  const spanMs = new Date(rows[0].end.replace(" ", "T")) - new Date(rows[0].start.replace(" ", "T"));
  $("hist-title").textContent = histStack.length
    ? `${data.start.slice(11, 19)} – ${data.end.slice(11, 19)} (${rows.length} buckets)`
    : `Whole day ${info.day}`;
  $("reset").hidden = !histStack.length;

  const x = d3.scaleBand().domain(d3.range(rows.length)).range([H.left, H.right]).paddingInner(0.12);
  const every = Math.ceil(rows.length / 12);
  const xAxis = (svg, h) => svg.append("g").attr("class", "axis").attr("transform", `translate(0,${h})`)
    .call(d3.axisBottom(x).tickValues(d3.range(0, rows.length, every))
      .tickFormat((i) => shortTime(rows[i].start, spanMs)).tickSizeOuter(0));

  // MB for big buckets, KB for small ones (avoids "200m" = 0.2 MB on the axis)
  const useMB = d3.max(rows, (r) => r.bytes) >= 1024 ** 2;
  $("vol-label").textContent = `Log volume (${useMB ? "MB" : "KB"})`;
  drawBars("#vol", 150, rows, x, (r) => r.bytes / (useMB ? 1024 ** 2 : 1024), "bar-vol", xAxis);
  const hasErrors = rows.every((r) => r.errors !== null);
  if (hasErrors) {
    drawBars("#err", 120, rows, x, (r) => r.errors, "bar-err", xAxis);
  } else {
    const svg = d3.select("#err");
    svg.selectAll("*").remove();
    svg.append("text").attr("class", "no-data").attr("x", 500).attr("y", 60).text("Window too big to count errors: zoom in");
  }
}

function drawBars(sel, height, rows, x, value, cls, xAxis) {
  const svg = d3.select(sel);
  svg.selectAll("*").remove();
  const bottom = height - 22;
  const y = d3.scaleLinear().domain([0, d3.max(rows, value) || 1]).nice().range([bottom, H.top]);
  svg.append("g").attr("class", "axis").attr("transform", `translate(${H.left},0)`)
    .call(d3.axisLeft(y).ticks(3).tickSize(-(H.right - H.left)).tickFormat(d3.format(",~r")))
    .call((g) => g.select(".domain").remove());
  xAxis(svg, bottom);
  svg.append("g").selectAll("rect").data(rows).join("rect")
    .attr("class", cls)
    .attr("x", (_, i) => x(i)).attr("width", x.bandwidth())
    .attr("y", (r) => y(value(r))).attr("height", (r) => Math.max(0, bottom - y(value(r))))
    .attr("rx", Math.min(2, x.bandwidth() / 2));
  // full-height hit areas: easier to hover and click than thin bars
  svg.append("g").selectAll("rect").data(rows).join("rect")
    .attr("class", "bar-hit")
    .attr("x", (_, i) => x(i) - (x.step() - x.bandwidth()) / 2).attr("width", x.step())
    .attr("y", H.top).attr("height", bottom - H.top)
    .on("mousemove", (ev, r) => showTip(ev, r))
    .on("mouseleave", () => { $("tip").hidden = true; })
    .on("click", (_, r) => zoomInto(r));
}

function showTip(ev, r) {
  const tip = $("tip");
  const box = tip.parentElement.getBoundingClientRect();
  tip.innerHTML = `<b>${esc(r.start.slice(11))} – ${esc(r.end.slice(11))}</b><br>` +
    `${mb(r.bytes)} of logs` + (r.errors !== null ? ` · ${fmt(r.errors)} errors` : "");
  tip.hidden = false;
  const left = Math.min(ev.clientX - box.left + 12, box.width - tip.offsetWidth - 4);
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${ev.clientY - box.top + 12}px`;
}

async function zoomInto(r) {
  $("tip").hidden = true;
  const spanMs = new Date(r.end.replace(" ", "T")) - new Date(r.start.replace(" ", "T"));
  $("from").value = r.start.slice(11);
  $("to").value = r.end.slice(11);
  if (spanMs >= 2000) {
    histStack.push({ start: r.start, end: r.end });
    await loadHistogram(r.start, r.end, 60).catch((e) => { $("err-msg").textContent = e.message; });
  }
  doSearch();
}

$("reset").addEventListener("click", async () => {
  histStack = [];
  await loadHistogram(null, null, 96);
});

// ---- search ----------------------------------------------------------------------

function levelBadge(level) {
  return `<span class="lvl lvl-${esc(level)}">${esc(level)}</span>`;
}

async function doSearch() {
  const from = $("from").value.trim();
  const to = $("to").value.trim();
  $("err-msg").textContent = "";
  for (const [name, v] of [["From", from], ["To", to]]) {
    if (v && !TIME_RE.test(v)) {
      $("err-msg").textContent = `${name}: use 14:05, 14:05:30 or 2026-11-19 14:05`;
      return;
    }
  }
  const q = new URLSearchParams();
  if (from) q.set("start", from);
  if (to) q.set("end", to);
  if ($("level").value) q.set("level", $("level").value);
  if ($("service").value) q.set("service", $("service").value);
  if ($("grep").value.trim()) q.set("grep", $("grep").value.trim());
  q.set("limit", "200");
  let res;
  try {
    res = await api(`/api/search?${q}`);
  } catch (e) {
    $("err-msg").textContent = e.message;
    return;
  }
  const s = res.stats;
  $("stats").textContent =
    `${fmt(s.matched)}${s.limited ? "+" : ""} matches` +
    (s.limited ? " (showing the first 200)" : "") +
    ` · ${fmt(s.scanned)} entries read from a ${mb(s.window_bytes)} window of ${mb(info.size)}` +
    ` · binary search ${s.jumps} jumps in ${s.search_ms} ms · scan ${s.scan_ms} ms` +
    (s.truncated ? " · window cut to 128 MB" : "");
  $("rows").innerHTML = res.entries.map((e) => `
    <tr>
      <td class="time">${esc(e.timestamp.slice(11))}</td>
      <td>${levelBadge(e.level)}</td>
      <td>${esc(e.service)}</td>
      <td>${esc(e.message)}${e.extra.length ? `<pre>${esc(e.extra.join("\n"))}</pre>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="hint">No matches.</td></tr>`;
  animateSearch(res, from || "the start");
}

$("search").addEventListener("submit", (e) => { e.preventDefault(); doSearch(); });
["from", "to"].forEach((id) => $(id).addEventListener("input", () => { $("err-msg").textContent = ""; }));

// ---- start -------------------------------------------------------------------------

async function start() {
  info = await api("/api/info");
  $("file").textContent = `${info.file} · ${mb(info.size)} · ${info.day}`;
  const day = await api("/api/histogram?buckets=24&errors=false");
  hourOffsets = day.buckets.map((b) => ({ label: b.start.slice(11, 16), first: b.first }));
  drawStripBase();
  await loadHistogram(null, null, 96);
  $("level").value = "ERROR";
  doSearch();
}

start().catch((e) => { $("err-msg").textContent = `Could not load: ${e.message}`; });
