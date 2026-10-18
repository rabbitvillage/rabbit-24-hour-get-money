// btc5m dashboard: a read-only page fed by the racknerd simulator.
//   GET  /            -> HTML rendered from the last pushed snapshot (no query params, no client JS)
//   POST /api/push    -> store a snapshot; needs "Authorization: Bearer <PUSH_TOKEN>" (secret binding)
// Everything else -> 404/405. All rendered text is HTML-escaped; numbers are coerced with Number().

const KEY = "latest";
const MAX_BODY = 65536;
const STALE_AFTER_S = 15 * 60;

const SECURITY_HEADERS = {
  "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "cache-control": "no-store",
  "permissions-policy": "geolocation=(), microphone=(), camera=()",
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/") {
      if (request.method !== "GET" && request.method !== "HEAD") return text(405, "method not allowed", { allow: "GET, HEAD" });
      const raw = await env.DASH.get(KEY);
      return html(render(raw ? safeParse(raw) : null));
    }
    if (url.pathname === "/api/push") {
      if (request.method !== "POST") return text(405, "method not allowed", { allow: "POST" });
      if (!(await authorized(request, env.PUSH_TOKEN))) return text(401, "unauthorized");
      const len = Number(request.headers.get("content-length") || 0);
      if (len > MAX_BODY) return text(413, "too large");
      const body = await request.text();
      if (body.length > MAX_BODY) return text(413, "too large");
      const parsed = safeParse(body);
      if (!parsed) return text(400, "bad json");
      const snap = sanitize(parsed);
      snap.received_at = new Date().toISOString();
      await env.DASH.put(KEY, JSON.stringify(snap));
      return text(200, "ok");
    }
    return text(404, "not found");
  },
};

// ---------- auth ----------
async function authorized(request, token) {
  if (!token) return false;
  const h = request.headers.get("authorization") || "";
  if (!h.startsWith("Bearer ")) return false;
  const given = h.slice(7).trim();
  const enc = new TextEncoder();
  const a = enc.encode(given), b = enc.encode(token);
  if (a.byteLength !== b.byteLength) return false;
  return crypto.subtle.timingSafeEqual(a, b);
}

// ---------- sanitizing (whitelist of keys and types) ----------
function safeParse(s) { try { const v = JSON.parse(s); return v && typeof v === "object" && !Array.isArray(v) ? v : null; } catch { return null; } }
const str = (v, n = 64) => (typeof v === "string" ? v.slice(0, n) : typeof v === "number" ? String(v) : "");
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : null));
const STATS_KEYS = ["markets", "entries", "settled", "losses", "pnl", "median_depth_usd", "avg_pnl_per_trade", "avg_fetch_ms"];
function stats(o) { const r = {}; if (o && typeof o === "object") for (const k of STATS_KEYS) r[k] = num(o[k]); return r; }
function sanitize(p) {
  const out = {
    generated_at: str(p.generated_at, 40),
    host: str(p.host, 40),
    service: str(p.service, 20),
    rule: str(p.rule, 80),
    first_row_utc: str(p.first_row_utc, 40),
    last_row_utc: str(p.last_row_utc, 40),
    all: stats(p.all), last24h: stats(p.last24h),
    variants: [], recent: [], losses: [], distance: {},
  };
  if (Array.isArray(p.variants)) for (const v of p.variants.slice(0, 12)) out.variants.push({ name: str(v && v.name, 16), ...stats(v) });
  if (Array.isArray(p.recent)) for (const r of p.recent.slice(0, 20)) out.recent.push({
    slug: str(r && r.slug, 40), time: str(r && r.time, 25), side: str(r && r.side, 8), ask: num(r && r.ask),
    secs_left: num(r && r.secs_left), depth_usd: num(r && r.depth_usd), winner: str(r && r.winner, 8), won: num(r && r.won), pnl: num(r && r.pnl) });
  if (Array.isArray(p.losses)) for (const r of p.losses.slice(0, 20)) out.losses.push({
    slug: str(r && r.slug, 40), time: str(r && r.time, 25), side: str(r && r.side, 8), ask: num(r && r.ask), secs_left: num(r && r.secs_left),
    dist_at_entry: num(r && r.dist_at_entry), min_abs_dist: num(r && r.min_abs_dist), pnl: num(r && r.pnl) });
  const d = p.distance && typeof p.distance === "object" ? p.distance : {};
  out.distance = { samples: num(d.samples), ptb_err_median: num(d.ptb_err_median), ptb_err_p90: num(d.ptb_err_p90),
    min_abs_dist_median: num(d.min_abs_dist_median), entries_with_dist: num(d.entries_with_dist) };
  return out;
}

// ---------- rendering ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n = (v, d = 0) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const money = (v) => (v === null || v === undefined ? "—" : (Number(v) >= 0 ? "+" : "") + "$" + n(Math.abs(Number(v)), 2).replace(/^/, Number(v) < 0 ? "-" : "").replace("-$", "-$"));
const pct = (a, b) => (b ? n((100 * a) / b, 1) + "%" : "—");

function statsRow(label, s) {
  const lossRate = s.settled ? (100 * s.losses) / s.settled : null;
  return `<tr><th>${esc(label)}</th><td>${n(s.markets)}</td><td>${n(s.entries)} <small>(${pct(s.entries, s.markets)})</small></td>
<td>${n(s.settled)}</td><td class="${s.losses ? "bad" : "good"}">${n(s.losses)} <small>(${lossRate === null ? "—" : n(lossRate, 2) + "%"})</small></td>
<td class="${(s.pnl ?? 0) < 0 ? "bad" : "good"}">${money(s.pnl)}</td><td>${s.avg_pnl_per_trade === null ? "—" : money(s.avg_pnl_per_trade)}</td><td>$${n(s.median_depth_usd)}</td></tr>`;
}

function render(s) {
  const now = Date.now();
  let title = "BTC 5m 模擬 bot — 尚無資料";
  let body = `<p class="warn">還沒有收到任何資料推送。</p>`;
  if (s) {
    const recv = Date.parse(s.received_at || "") || 0;
    const ageS = recv ? Math.round((now - recv) / 1000) : null;
    const stale = ageS === null || ageS > STALE_AFTER_S;
    const svc = s.service === "active" ? `<span class="good">● active</span>` : `<span class="bad">● ${esc(s.service || "unknown")}</span>`;
    const progress = Math.min(100, Math.round((100 * (s.all.settled || 0)) / 300));
    const lossAll = s.all.settled ? (100 * s.all.losses) / s.all.settled : null;
    const gate = lossAll === null ? "—" : lossAll <= 1 ? `<span class="good">≤1%（可考慮放大）</span>` : lossAll > 1.9 ? `<span class="bad">&gt;1.9%（計畫要求停）</span>` : `<span class="warn">1~1.9%（觀察）</span>`;
    title = "BTC 5m 模擬 bot";
    body = `
<section class="cards">
  <div class="card"><div class="k">服務</div><div class="v">${svc}</div><div class="s">${esc(s.host)}</div></div>
  <div class="card ${stale ? "stale" : ""}"><div class="k">資料更新</div><div class="v">${ageS === null ? "—" : ageS < 120 ? ageS + " 秒前" : Math.round(ageS / 60) + " 分鐘前"}</div><div class="s">${esc(s.generated_at)}${stale ? " ⚠ 推送已停" : ""}</div></div>
  <div class="card"><div class="k">累計模擬損益</div><div class="v ${(s.all.pnl ?? 0) < 0 ? "bad" : "good"}">${money(s.all.pnl)}</div><div class="s">${n(s.all.settled)} 筆已結算，輸 ${n(s.all.losses)}</div></div>
  <div class="card"><div class="k">300 筆門檻</div><div class="v">${n(s.all.settled)} / 300</div><div class="bar"><i style="width:${progress}%"></i></div><div class="s">輸的比例 ${gate}</div></div>
</section>
<h2>主規則 <small>${esc(s.rule)}</small></h2>
<table><thead><tr><th></th><th>場次</th><th>可進場</th><th>已結算</th><th>輸</th><th>模擬損益</th><th>每筆平均</th><th>98~99¢ 賣單中位數</th></tr></thead>
<tbody>${statsRow("最近 24 小時", s.last24h)}${statsRow("累計", s.all)}</tbody></table>
<h2>影子變體 <small>同一場盤、不同進場規則（w = 最後幾秒開始找，p = 最低買價）</small></h2>
<table><thead><tr><th>變體</th><th>場次</th><th>可進場</th><th>已結算</th><th>輸</th><th>模擬損益</th><th>每筆平均</th><th>賣單中位數</th></tr></thead>
<tbody>${s.variants.map((v) => statsRow(v.name, v)).join("")}</tbody></table>
<h2>BTC 現價與基準價 <small>priceToBeat 收盤後才公布，距離為結算後回填</small></h2>
<table class="kv"><tbody>
<tr><th>有現價樣本的場次</th><td>${n(s.distance.samples)}</td><th>開盤現價 vs 基準價誤差（中位數 / P90，美元）</th><td>${n(s.distance.ptb_err_median, 2)} / ${n(s.distance.ptb_err_p90, 2)}</td></tr>
<tr><th>最後 60 秒最接近基準價（中位數，美元）</th><td>${n(s.distance.min_abs_dist_median, 2)}</td><th>已有距離資料的進場</th><td>${n(s.distance.entries_with_dist)}</td></tr>
</tbody></table>
<h2>最近進場（主規則）</h2>
<table><thead><tr><th>時間（UTC）</th><th>場</th><th>邊</th><th>價</th><th>剩秒</th><th>賣單深度</th><th>結果</th><th>損益</th></tr></thead>
<tbody>${s.recent.length ? s.recent.map((r) => `<tr><td>${esc(r.time)}</td><td>${esc(r.slug)}</td><td>${esc(r.side)}</td><td>${n(r.ask, 2)}</td><td>${n(r.secs_left, 1)}</td><td>$${n(r.depth_usd)}</td><td class="${r.won === 0 ? "bad" : r.won === 1 ? "good" : ""}">${esc(r.winner)}${r.won === 0 ? " ✗" : r.won === 1 ? " ✓" : ""}</td><td class="${(r.pnl ?? 0) < 0 ? "bad" : ""}">${money(r.pnl)}</td></tr>`).join("") : `<tr><td colspan="8">尚無</td></tr>`}</tbody></table>
<h2>輸掉的場（所有變體）</h2>
<table><thead><tr><th>時間（UTC）</th><th>場</th><th>邊</th><th>價</th><th>剩秒</th><th>進場時距離</th><th>最接近</th><th>損益</th></tr></thead>
<tbody>${s.losses.length ? s.losses.map((r) => `<tr><td>${esc(r.time)}</td><td>${esc(r.slug)}</td><td>${esc(r.side)}</td><td>${n(r.ask, 2)}</td><td>${n(r.secs_left, 1)}</td><td>${n(r.dist_at_entry, 2)}</td><td>${n(r.min_abs_dist, 2)}</td><td class="bad">${money(r.pnl)}</td></tr>`).join("") : `<tr><td colspan="8" class="good">目前 0 筆</td></tr>`}</tbody></table>
<p class="foot">只讀模擬，不下單。資料區間 ${esc(s.first_row_utc)} → ${esc(s.last_row_utc)}（UTC）。頁面每 60 秒自動重新整理。</p>`;
  }
  return `<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><meta name="robots" content="noindex"><title>${esc(title)}</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262a33;--fg:#e6e8ee;--mut:#8b93a7;--good:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#58a6ff}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6eb;--fg:#1f2328;--mut:#6b7280;--acc:#0969da}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans TC",sans-serif}
main{max-width:1100px;margin:0 auto}h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;margin:22px 0 8px}h2 small{color:var(--mut);font-weight:normal;font-size:12px;margin-left:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.card.stale{border-color:var(--warn)}.k{color:var(--mut);font-size:12px}.v{font-size:22px;font-weight:600;margin:2px 0}.s{color:var(--mut);font-size:12px}
.bar{height:6px;background:var(--line);border-radius:3px;margin:6px 0;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}th:first-child,td:first-child{text-align:left}thead th{color:var(--mut);font-weight:500;font-size:12px}
tbody tr:last-child td{border-bottom:0}table.kv th{text-align:left;color:var(--mut);font-weight:500}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
small{color:var(--mut)}.foot{color:var(--mut);font-size:12px;margin-top:18px}.table-wrap{overflow-x:auto}
</style></head><body><main><h1>${esc(title)} <small>Polymarket BTC 5 分鐘盤 · 最後 45 秒買 98~99¢ · 只讀模擬</small></h1>${body}</main></body></html>`;
}

function html(s) { return new Response(s, { status: 200, headers: { "content-type": "text/html; charset=utf-8", ...SECURITY_HEADERS } }); }
function text(status, s, extra = {}) { return new Response(s, { status, headers: { "content-type": "text/plain; charset=utf-8", ...SECURITY_HEADERS, ...extra } }); }
