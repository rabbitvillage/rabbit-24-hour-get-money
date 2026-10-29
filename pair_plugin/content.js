// 開盤配對助手：在 Polymarket 5 分鐘盤頁面上顯示提示面板，並在不符合規則時擋下「買入」按鈕。
// 不會自己下單：所有買入都由人按，插件只負責看盤、提醒、擋違規、記帳。
(function () {
  "use strict";
  const L = PairLogic;
  const WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
  const SLUG_RE = /((btc|eth|sol|xrp)-updown-5m-(\d{9,11}))/;
  let cfg = { addr: "", amount: 5, l1: 0.40 };
  let mkt = null;          // {slug, asset, start, tokens:{Up,Down}, conditionId}
  let book = {};           // tokenId -> {asks: Map(price->size), bids: Map}
  let ws = null, pingT = null;
  let trades = [];         // 自己在這場的成交（data-api）
  let pos = L.positionFrom([]);
  let pendingUntil = 0;    // 剛按下買入後的冷卻，避免成交紀錄還沒更新就再按一次
  let lastDecision = null;
  let settledSlugs = {};

  // ---------- 設定 ----------
  chrome.storage.sync.get({ addr: "", amount: 5, l1: 0.40 }, v => { cfg = v; });
  chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync") for (const k in ch) cfg[k] = ch[k].newValue; });
  chrome.storage.local.get({ settled: {} }, v => { settledSlugs = v.settled || {}; });

  // ---------- 面板 ----------
  const panel = document.createElement("div"); panel.id = "pp-panel";
  panel.innerHTML = `<div class="pp-h"><span id="pp-title">開盤配對助手</span><span><span class="pp-t" id="pp-time"></span> <span class="pp-x" id="pp-min">—</span></span></div>
    <div id="pp-body"><div class="pp-asks"><div class="pp-ask" id="pp-up">Up 賣價<b>—</b></div><div class="pp-ask" id="pp-dn">Down 賣價<b>—</b></div></div>
    <div class="pp-msg" id="pp-msg">打開任何一場 BTC／ETH／SOL 5 分鐘盤</div>
    <button id="pp-go" class="pp-go" disabled>空白鍵／點這裡：買規則指定的那一邊</button>
    <div class="pp-row" id="pp-pos"></div><div class="pp-row" id="pp-stats"></div><div class="pp-warn" id="pp-warn"></div></div>`;
  function mount() { if (!document.getElementById("pp-panel")) document.body.appendChild(panel); }
  mount();
  panel.querySelector("#pp-min").onclick = () => { const b = panel.querySelector("#pp-body"); b.style.display = b.style.display === "none" ? "" : "none"; };
  (function drag() {
    const h = panel.querySelector(".pp-h"); let dx = 0, dy = 0, on = false;
    h.addEventListener("mousedown", e => { on = true; dx = e.clientX - panel.offsetLeft; dy = e.clientY - panel.offsetTop; });
    document.addEventListener("mousemove", e => { if (on) { panel.style.left = (e.clientX - dx) + "px"; panel.style.top = (e.clientY - dy) + "px"; panel.style.right = "auto"; } });
    document.addEventListener("mouseup", () => { on = false; });
  })();
  function flash(text) {
    let f = document.getElementById("pp-flash");
    if (!f) { f = document.createElement("div"); f.id = "pp-flash"; document.body.appendChild(f); }
    f.textContent = text; f.style.display = "block"; clearTimeout(flash.t); flash.t = setTimeout(() => { f.style.display = "none"; }, 3500);
  }

  // ---------- 盤口 ----------
  function best(tok, side) {
    const b = book[tok]; if (!b) return null;
    const m = side === "ask" ? b.asks : b.bids; let v = null;
    for (const [p, s] of m) if (s > 0) v = v == null ? p : (side === "ask" ? Math.min(v, p) : Math.max(v, p));
    return v;
  }
  function onWs(raw) {
    if (!raw || raw[0] !== "[" && raw[0] !== "{") return;
    let d; try { d = JSON.parse(raw); } catch (e) { return; }
    for (const it of (Array.isArray(d) ? d : [d])) {
      if (!it || typeof it !== "object") continue;
      if (it.event_type === "book") {
        book[String(it.asset_id)] = { asks: new Map((it.asks || []).map(x => [Number(x.price), Number(x.size)])), bids: new Map((it.bids || []).map(x => [Number(x.price), Number(x.size)])) };
      } else if (it.event_type === "price_change") {
        for (const c of (it.price_changes || it.changes || [])) {
          const tok = String(c.asset_id || it.asset_id), b = book[tok]; if (!b) continue;
          const m = String(c.side).toUpperCase() === "BUY" ? b.bids : b.asks; const p = Number(c.price), s = Number(c.size);
          if (s <= 0) m.delete(p); else m.set(p, s);
        }
      }
    }
  }
  function connect() {
    if (ws) { try { ws.close(); } catch (e) {} } clearInterval(pingT); book = {};
    if (!mkt) return;
    const toks = [mkt.tokens.Up, mkt.tokens.Down]; const slug = mkt.slug;
    ws = new WebSocket(WS);
    ws.onopen = () => { ws.send(JSON.stringify({ assets_ids: toks, type: "market" })); pingT = setInterval(() => { try { ws.send("PING"); } catch (e) {} }, 10000); };
    ws.onmessage = e => onWs(String(e.data));
    ws.onclose = () => { clearInterval(pingT); if (mkt && mkt.slug === slug) setTimeout(() => { if (mkt && mkt.slug === slug) connect(); }, 1500); };
  }

  // ---------- 市場與自己的成交 ----------
  async function loadMarket(slug) {
    const r = await fetch(`https://gamma-api.polymarket.com/events?slug=${slug}`); const d = await r.json();
    if (!d || !d.length) return null;
    const m = d[0].markets[0]; const toks = JSON.parse(m.clobTokenIds), outs = JSON.parse(m.outcomes);
    return { slug, asset: slug.split("-")[0].toUpperCase(), start: Number(slug.split("-").pop()), conditionId: m.conditionId,
             tokens: { Up: String(toks[outs.indexOf("Up")]), Down: String(toks[outs.indexOf("Down")]) } };
  }
  async function loadTrades() {
    if (!mkt || !cfg.addr) return;
    try {
      const r = await fetch(`https://data-api.polymarket.com/trades?user=${cfg.addr}&market=${mkt.conditionId}&limit=100&takerOnly=false`);
      const d = await r.json(); if (Array.isArray(d)) { trades = d; pos = L.positionFrom(trades); }
    } catch (e) {}
  }
  async function checkSettled() {
    if (!mkt || settledSlugs[mkt.slug] || (pos.Up.shares + pos.Down.shares) === 0) return;
    if (Date.now() / 1000 < mkt.start + 300 + 20) return;
    try {
      const d = await (await fetch(`https://gamma-api.polymarket.com/events?slug=${mkt.slug}`)).json();
      const m = d[0].markets[0]; const pr = JSON.parse(m.outcomePrices || "[]"); const outs = JSON.parse(m.outcomes);
      const i = pr.findIndex(x => Number(x) >= 0.99); if (i < 0) return;
      const res = L.settle(pos, outs[i]);
      settledSlugs[mkt.slug] = Object.assign({ winner: outs[i], day: new Date().toISOString().slice(0, 10), up: pos.Up.shares, down: pos.Down.shares }, res);
      chrome.storage.local.set({ settled: settledSlugs });
    } catch (e) {}
  }

  // ---------- 擋違規的買入 ----------
  // 頁面結構（2026-10-29 實測）：Up／Down 是 role="radio" 的按鈕，文字含滾動數字動畫（例如「Down 1¢ 9 8 7 … ¢」），
  // 選中的那顆 aria-checked="true"；下單模式有「1-Tap」（直接點 $5／$25／$100 就買）和市價（金額欄＋「買入 Up」按鈕）。
  const norm = b => (b.innerText || b.textContent || "").trim().replace(/\s+/g, " ");
  const cap1 = x => x[0].toUpperCase() + x.slice(1).toLowerCase();
  function outcomeRadios() {
    return [...document.querySelectorAll('button[role="radio"]')].filter(b => /^(Up|Down)\b/i.test(norm(b)));
  }
  function checkedOutcome() {
    const r = outcomeRadios().find(b => b.getAttribute("aria-checked") === "true" || b.getAttribute("data-state") === "checked");
    return r ? cap1(norm(r).split(" ")[0]) : null;
  }
  function amountInputNear(el) {
    let root = el; for (let i = 0; i < 10 && root && root.parentElement; i++) root = root.parentElement;
    if (!root) return null;
    return [...root.querySelectorAll("input")].find(i => i.type !== "hidden" && !/搜尋|search/i.test(i.placeholder || "")) || null;
  }
  function amountOnPage(btn) {
    const inp = amountInputNear(btn); if (!inp) return null;
    const v = Number(String(inp.value).replace(/[^0-9.]/g, "")); return isFinite(v) ? v : null;
  }
  // 被點的是不是「買入」：市價模式的「買入 Up／Down」，或 1-Tap 的「$5 win …」
  function buyButton(el) {
    const b = el && el.closest ? el.closest("button") : null; if (!b) return null;
    const t = norm(b);
    let m = t.match(/^(買入|Buy)\s+(Up|Down)\b/i);
    if (m) return { el: b, side: cap1(m[2]), amount: null };
    m = t.match(/^\$\s*([0-9]+(?:\.[0-9]+)?)\s+(win|贏)/i);
    if (m) { const side = checkedOutcome(); return side ? { el: b, side, amount: Number(m[1]), oneTap: true } : { el: b, side: null, amount: Number(m[1]), oneTap: true }; }
    return null;
  }
  function guard(e) {
    const bb = buyButton(e.target); if (!bb || !mkt) return;
    const block = why => { e.preventDefault(); e.stopImmediatePropagation(); flash("擋下：" + why); panel.querySelector("#pp-warn").textContent = "擋下：" + why; };
    if (!cfg.addr) return block("先在插件設定填你的錢包地址，插件才知道你買了什麼");
    if (Date.now() < pendingUntil) return block("上一張剛送出，等成交紀錄更新（約 5 秒）");
    const d = lastDecision; if (!d) return block("還沒收到盤口");
    if (!bb.side) return block("讀不到現在選的是 Up 還是 Down");
    if (!d.allow[bb.side]) return block(d.msg);
    const amt = bb.amount != null ? bb.amount : amountOnPage(bb.el);
    if (amt == null) return block("讀不到金額欄，請確認是市價買入並填了金額");
    if (amt > d.maxAmount[bb.side] + 1e-9) return block(`金額 $${amt} 超過上限 $${d.maxAmount[bb.side].toFixed(2)}，不准放大`);
    pendingUntil = Date.now() + 5000;
    panel.querySelector("#pp-warn").textContent = `已放行：買入 ${bb.side} $${amt}`;
  }
  document.addEventListener("click", guard, true);
  document.addEventListener("keydown", e => { if (e.key === "Enter") { const a = document.activeElement; if (a && a.tagName === "INPUT" && mkt) { e.preventDefault(); e.stopImmediatePropagation(); flash("請用按鈕買入（Enter 已停用）"); } } }, true);

  // ---------- 一鍵買：切邊、填金額、按買入（人按了才執行） ----------
  const setVal = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function findOutcomeButton(side) {
    return outcomeRadios().find(b => new RegExp("^" + side + "\\b", "i").test(norm(b)));
  }
  function findBuyButton(side) {
    const re = new RegExp("^(買入|Buy)\\s+" + side + "\\b", "i");
    return [...document.querySelectorAll("button")].find(b => re.test(norm(b)));
  }
  function findOneTap(amount) {
    return [...document.querySelectorAll("button")].find(b => { const m = norm(b).match(/^\$\s*([0-9]+(?:\.[0-9]+)?)\s+(win|贏)/i); return m && Number(m[1]) === amount; });
  }
  const findAmountInput = near => amountInputNear(near);
  function pickSide(d) {
    const sides = ["Up", "Down"].filter(x => d.allow[x]);
    if (!sides.length) return null;
    if (sides.length === 1) return sides[0];
    const a = { Up: best(mkt.tokens.Up, "ask"), Down: best(mkt.tokens.Down, "ask") };
    return a.Up <= a.Down ? "Up" : "Down";
  }
  let busy = false;
  async function quickBuy() {
    if (busy || !mkt) return; const d = lastDecision;
    if (!cfg.addr) return flash("先在插件設定填你的錢包地址");
    if (!d) return flash("還沒收到盤口");
    const side = pickSide(d); if (!side) return flash("現在不能買：" + d.msg);
    busy = true;
    try {
      const ob = findOutcomeButton(side); if (!ob) return flash(`找不到頁面上的「${side}」按鈕`);
      ob.click(); await sleep(120);
      const pos2 = pos; const a = best(mkt.tokens[side], "ask");
      if (d.phase === "first") {                         // 1-Tap 模式下第一張直接點同金額的按鈕
        const t = findOneTap(Number(cfg.amount) || 5);
        if (t) { t.click(); return; }
      }
      const bb = findBuyButton(side);
      if (!bb) return flash(d.phase === "first" ? `找不到「買入 ${side}」或「$${cfg.amount}」按鈕` : "第二張要買同樣股數：請把下單模式切到市價（有金額欄的那個）");
      const inp = findAmountInput(bb); if (!inp) return flash("找不到金額欄");
      let amt = d.phase === "first" ? Number(cfg.amount) || 5 : Math.floor((pos2[L.other(side)].shares - pos2[side].shares) * a * 100) / 100;
      amt = Math.min(amt, d.maxAmount[side]);
      if (!(amt > 0)) return flash("算不出金額");
      inp.focus(); setVal.call(inp, String(amt)); inp.dispatchEvent(new Event("input", { bubbles: true })); inp.dispatchEvent(new Event("change", { bubbles: true }));
      await sleep(150);
      bb.click();                                   // 會再經過 guard 的規則與金額檢查
    } finally { setTimeout(() => { busy = false; }, 400); }
  }
  panel.querySelector("#pp-go").onclick = e => { e.preventDefault(); e.stopPropagation(); quickBuy(); };
  document.addEventListener("keydown", e => {
    if (e.code !== "Space" || !mkt) return;
    const a = document.activeElement; if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable)) return;
    e.preventDefault(); e.stopImmediatePropagation(); quickBuy();
  }, true);

  // ---------- 主迴圈 ----------
  let lastPath = "";
  async function onRoute() {
    const m = location.pathname.match(SLUG_RE); lastPath = location.pathname;
    if (!m) { mkt = null; connect(); render(); return; }
    if (mkt && mkt.slug === m[1]) return;
    mkt = null; trades = []; pos = L.positionFrom([]); render();
    try { mkt = await loadMarket(m[1]); } catch (e) { mkt = null; }
    connect(); loadTrades();
  }
  function render() {
    mount();
    const $ = s => panel.querySelector(s);
    if (!mkt) { $("#pp-title").textContent = "開盤配對助手"; $("#pp-time").textContent = ""; $("#pp-msg").textContent = "打開任何一場 BTC／ETH／SOL 5 分鐘盤"; $("#pp-msg").className = "pp-msg"; lastDecision = null; return; }
    const s = Date.now() / 1000 - mkt.start;
    const asks = { Up: best(mkt.tokens.Up, "ask"), Down: best(mkt.tokens.Down, "ask") };
    const d = L.decide(s, asks, pos, { L1: Number(cfg.l1) || 0.40, AMOUNT: Number(cfg.amount) || 5 });
    lastDecision = d;
    const hh = new Date(mkt.start * 1000); $("#pp-title").textContent = `${mkt.asset} ${String(hh.getHours()).padStart(2, "0")}:${String(hh.getMinutes()).padStart(2, "0")} 場`;
    $("#pp-time").textContent = s < 0 ? `開盤前 ${Math.ceil(-s)}s` : `開盤後 +${Math.floor(s)}s`;
    $("#pp-up").innerHTML = `Up 賣價<b>${asks.Up == null ? "—" : asks.Up.toFixed(2)}</b>`; $("#pp-up").className = "pp-ask" + (d.allow.Up ? " hot" : "");
    $("#pp-dn").innerHTML = `Down 賣價<b>${asks.Down == null ? "—" : asks.Down.toFixed(2)}</b>`; $("#pp-dn").className = "pp-ask" + (d.allow.Down ? " hot" : "");
    $("#pp-msg").textContent = cfg.addr ? d.msg : "先到插件設定填你的錢包地址（右鍵插件圖示 → 選項）";
    $("#pp-msg").className = "pp-msg " + (cfg.addr ? d.level : "stop");
    const goSide = cfg.addr ? pickSide(d) : null; const go = $("#pp-go");
    go.disabled = !goSide; go.textContent = goSide ? `空白鍵：買 ${goSide}${d.phase === "first" ? `（$${cfg.amount}）` : "（補同樣股數）"}` : "現在沒有可以買的";
    $("#pp-pos").textContent = `部位：Up ${pos.Up.shares.toFixed(2)} 股${pos.Up.avg ? " @" + pos.Up.avg.toFixed(3) : ""}｜Down ${pos.Down.shares.toFixed(2)} 股${pos.Down.avg ? " @" + pos.Down.avg.toFixed(3) : ""}`;
    const today = new Date().toISOString().slice(0, 10); const rs = Object.values(settledSlugs).filter(r => r.day === today);
    if (rs.length) {
      const pnl = rs.reduce((a, r) => a + r.pnl, 0), paired = rs.filter(r => r.paired).length;
      $("#pp-stats").textContent = `今日：${rs.length} 場，成對 ${paired}、補不到 ${rs.length - paired}，損益 ${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`;
    } else $("#pp-stats").textContent = "今日：尚無結算";
  }
  setInterval(() => { if (location.pathname !== lastPath) onRoute(); render(); }, 250);
  setInterval(() => { loadTrades(); checkSettled(); }, 2000);
  onRoute();
})();
