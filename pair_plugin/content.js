// 開盤配對助手（人工模式）：在 Polymarket 5 分鐘盤頁面顯示提示面板、擋違規買入、一鍵下單、每場記錄。
// 練習模式（預設開）：按鍵不碰頁面上的下單按鈕，改用當下真實盤口模擬成交，收盤後照真實結果算損益；不花錢。
// 插件只在人按鍵或點按鈕時才動作，不會自己下單：
//   第一張：人決定時機，空白鍵＝市價買較便宜的那邊（U／D 鍵指定邊），金額固定。
//   第二張：時機由人決定。面板第三格即時顯示「現在補對面能賺幾 %」；空白鍵＝直接買（市價）對面同樣股數，不留掛單。
(function () {
  "use strict";
  const L = PairLogic;
  const VER = (() => { try { return chrome.runtime.getManifest().version; } catch (e) { return "?"; } })();
  // 插件重新載入後，已開著的分頁裡的舊程式會變成「孤兒」（chrome.runtime.id 消失）：這時要讓開、提示重新整理頁面
  const alive = () => { try { return !!(chrome.runtime && chrome.runtime.id); } catch (e) { return false; } };
  const WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
  const SLUG_RE = /((btc|eth|sol|xrp)-updown-5m-(\d{9,11}))/;
  const CFG0 = { addr: "", amount: 5, margin: L.DEFAULTS.MARGIN, last: L.DEFAULTS.LAST, practice: true };
  let cfg = Object.assign({}, CFG0);
  let mkt = null;          // {slug, asset, start, tokens:{Up,Down}, conditionId}
  let book = {};           // tokenId -> {asks: Map(price->size), bids: Map}
  let ws = null, pingT = null;
  let pos = L.positionFrom([]);
  let pendingUntil = 0;    // 剛送出後的冷卻，避免成交紀錄還沒更新就再按一次
  let lastDecision = null;
  let store = { settled: {}, legs: {}, journal: {}, sim: {}, settledSim: {} };   // 真實：結算、第二張狀態；每場紀錄；練習：模擬成交與結算
  let hist = [];           // 這一場每秒的兩邊賣價 [s, up, down]
  const RTDS = "wss://ws-live-data.polymarket.com";   // Polymarket 公開 Chainlink 每秒價格（連上會先送最近約 1 分鐘）
  let clWs = null, clPing = null, clPts = [], clLine = null;   // clPts = [[秒, 價格]]；clLine = 自己算的開盤基準線

  // ---------- 設定與儲存 ----------
  chrome.storage.sync.get(CFG0, v => { cfg = v; });
  chrome.storage.onChanged.addListener((ch, area) => { if (area === "sync") for (const k in ch) cfg[k] = ch[k].newValue; });
  chrome.storage.local.get({ settled: {}, legs: {}, journal: {}, sim: {}, settledSim: {} }, v => { store = v; });
  let saveT = null;
  function save() { clearTimeout(saveT); saveT = setTimeout(() => chrome.storage.local.set(store), 300); }
  const practice = () => cfg.practice !== false;
  const stOf = slug => practice() ? (store.sim[slug] = store.sim[slug] || { trades: [] }) : (store.legs[slug] = store.legs[slug] || {});
  const settledMap = () => practice() ? store.settledSim : store.settled;
  const recOf = () => { const k = (practice() ? "sim:" : "") + mkt.slug;
    return (store.journal[k] = store.journal[k] || { mode: practice() ? "sim" : "live", slug: mkt.slug, asset: mkt.asset }); };
  const lcfg = () => ({ AMOUNT: Number(cfg.amount) || 5, MARGIN: Number(cfg.margin) || L.DEFAULTS.MARGIN, LAST: Number(cfg.last) || L.DEFAULTS.LAST });

  // ---------- 面板 ----------
  const panel = document.createElement("div"); panel.id = "pp-panel";
  panel.innerHTML = `<div class="pp-h"><span id="pp-title">開盤配對助手</span><span><span class="pp-t" id="pp-time"></span> <span class="pp-x" id="pp-min">—</span></span></div>
    <div id="pp-body"><label class="pp-mode" id="pp-modebox"><input type="checkbox" id="pp-practice"> 練習模式（不花錢、不會下單）</label><div class="pp-asks"><div class="pp-ask" id="pp-up">Up 賣價<b>—</b></div><div class="pp-ask" id="pp-dn">Down 賣價<b>—</b></div><div class="pp-ask pp-lock" id="pp-lock">現在補<b>—</b><i></i></div></div>
    <div class="pp-msg" id="pp-msg">打開任何一場 BTC／ETH／SOL 5 分鐘盤</div>
    <button id="pp-go" class="pp-go" disabled>空白鍵</button>
    <div class="pp-row" id="pp-line"></div><div class="pp-row" id="pp-pos"></div><div class="pp-row" id="pp-stats"></div>
    <div class="pp-row"><a id="pp-reset" class="pp-link" style="display:none">第二張重掛（先在頁面上取消舊掛單）</a></div>
    <div class="pp-warn" id="pp-warn"></div></div>`;
  function mount() { if (!document.getElementById("pp-panel")) document.body.appendChild(panel); }
  mount();
  panel.querySelector("#pp-min").onclick = () => { const b = panel.querySelector("#pp-body"); b.style.display = b.style.display === "none" ? "" : "none"; };
  panel.querySelector("#pp-reset").onclick = () => {
    if (!mkt) return; const st = stOf(mkt.slug);
    if (practice() && st.leg2 && st.leg2.done) return flash("練習的第二張已經成交，不能取消");
    delete st.leg2; save(); flash(practice() ? "練習：已取消第二張掛單" : "已清除第二張掛單紀錄，可以重新按空白鍵掛單", "ok");
  };
  const modeBox = panel.querySelector("#pp-practice");
  modeBox.addEventListener("change", () => {
    const on = modeBox.checked; modeBox.blur();
    if (!on && !confirm("關掉練習模式後，空白鍵會真的下單、真的花錢。確定？")) { modeBox.checked = true; return; }
    cfg.practice = on; chrome.storage.sync.set({ practice: on }); pos = L.positionFrom([]); loadTrades();
    flash(on ? "練習模式：不會下單" : "真實模式：空白鍵會真的下單", on ? "ok" : "");
  });
  (function drag() {
    const h = panel.querySelector(".pp-h"); let dx = 0, dy = 0, on = false;
    h.addEventListener("mousedown", e => { on = true; dx = e.clientX - panel.offsetLeft; dy = e.clientY - panel.offsetTop; });
    document.addEventListener("mousemove", e => { if (on) { panel.style.left = (e.clientX - dx) + "px"; panel.style.top = (e.clientY - dy) + "px"; panel.style.right = "auto"; } });
    document.addEventListener("mouseup", () => { on = false; });
  })();
  function flash(text, kind) {
    let f = document.getElementById("pp-flash");
    if (!f) { f = document.createElement("div"); f.id = "pp-flash"; document.body.appendChild(f); }
    f.textContent = text; f.className = kind === "ok" ? "ok" : ""; f.style.display = "block";
    clearTimeout(flash.t); flash.t = setTimeout(() => { f.style.display = "none"; }, 3500);
  }
  const warn = t => { panel.querySelector("#pp-warn").textContent = t; };

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
  function connectCl() {
    if (clWs) { try { clWs.onclose = null; clWs.close(); } catch (e) {} } clearInterval(clPing); clWs = null;
    if (!mkt) return;
    const slug = mkt.slug, sym = mkt.asset.toLowerCase() + "/usd";
    clWs = new WebSocket(RTDS);
    clWs.onopen = () => {   // 篩選條件要用沒有空格的 JSON，不然收不到更新；一條連線只訂一個幣
      clWs.send(JSON.stringify({ action: "subscribe", subscriptions: [{ topic: "crypto_prices_chainlink", type: "*", filters: JSON.stringify({ symbol: sym }) }] }));
      clPing = setInterval(() => { try { clWs.send("PING"); } catch (e) {} }, 10000);
    };
    clWs.onmessage = e => {
      let d; try { d = JSON.parse(String(e.data)); } catch (x) { return; }
      const p = d && d.payload; if (!p || (p.symbol && p.symbol !== sym)) return;
      const add = (ts, v) => { const sec = Math.floor(Number(ts) / 1000), val = Number(v); if (sec > 0 && val > 0 && !clPts.some(x => x[0] === sec)) clPts.push([sec, val]); };
      if (Array.isArray(p.data)) for (const x of p.data) add(x.timestamp, x.value); else if (p.value != null) add(p.timestamp, p.value);
      clPts.sort((a, b) => a[0] - b[0]); if (clPts.length > 600) clPts = clPts.slice(-600);
      if (clLine == null && mkt && Date.now() / 1000 >= mkt.start) clLine = L.lineFrom(clPts, mkt.start);
    };
    clWs.onclose = () => { clearInterval(clPing); if (mkt && mkt.slug === slug) setTimeout(() => { if (mkt && mkt.slug === slug) connectCl(); }, 1500); };
  }
  const clNow = () => (clPts.length ? clPts[clPts.length - 1][1] : null);
  const asksNow = () => ({ Up: best(mkt.tokens.Up, "ask"), Down: best(mkt.tokens.Down, "ask") });

  // ---------- 市場、自己的成交、結算 ----------
  async function loadMarket(slug) {
    const d = await (await fetch(`https://gamma-api.polymarket.com/events?slug=${slug}`)).json();
    if (!d || !d.length) return null;
    const m = d[0].markets[0]; const toks = JSON.parse(m.clobTokenIds), outs = JSON.parse(m.outcomes);
    return { slug, asset: slug.split("-")[0].toUpperCase(), start: Number(slug.split("-").pop()), conditionId: m.conditionId,
             tokens: { Up: String(toks[outs.indexOf("Up")]), Down: String(toks[outs.indexOf("Down")]) } };
  }
  function posToJournal() {                         // 把成交（真實或練習）寫進這一場的紀錄
    if (!mkt || !pos.first) return;
    const rec = recOf(), s1 = pos.first, s2 = L.other(s1);
    Object.assign(rec, { leg1_side: s1, leg1_price: pos[s1].avg, leg1_shares: pos[s1].shares, leg1_s: pos[s1].firstTs - mkt.start,
                         leg2_filled_shares: pos[s2].shares, leg2_fill_price: pos[s2].avg, leg2_fill_s: pos[s2].lastTs != null ? pos[s2].lastTs - mkt.start : null });
    save();
  }
  async function loadTrades() {
    if (!mkt) return;
    if (practice()) { pos = L.positionFrom(stOf(mkt.slug).trades); posToJournal(); return; }
    if (!cfg.addr) return;
    try {
      const r = await fetch(`https://data-api.polymarket.com/trades?user=${cfg.addr}&market=${mkt.conditionId}&limit=100&takerOnly=false`);
      const d = await r.json(); if (!Array.isArray(d) || practice()) return;
      pos = L.positionFrom(d); posToJournal();
    } catch (e) {}
  }
  async function checkSettled() {
    if (!mkt || settledMap()[mkt.slug] || (pos.Up.shares + pos.Down.shares) === 0) return;
    if (Date.now() / 1000 < mkt.start + 300 + 20) return;
    try {
      const d = await (await fetch(`https://gamma-api.polymarket.com/events?slug=${mkt.slug}`)).json();
      const m = d[0].markets[0]; const pr = JSON.parse(m.outcomePrices || "[]"); const outs = JSON.parse(m.outcomes);
      const i = pr.findIndex(x => Number(x) >= 0.99); if (i < 0) return;
      const st = stOf(mkt.slug), res = L.settle(pos, outs[i], !!(st.leg2 && !st.leg2.taker));
      settledMap()[mkt.slug] = Object.assign({ winner: outs[i], day: new Date().toISOString().slice(0, 10), up: pos.Up.shares, down: pos.Down.shares }, res);
      Object.assign(recOf(), { winner: outs[i], paired: res.paired ? 1 : 0, pnl: res.pnl });
      fillLine(recOf(), d[0].eventMetadata);
      save();
    } catch (e) {}
  }
  // 收盤後用官方開盤價（priceToBeat）重算離基準線距離；進行中官方還沒公布
  function fillLine(rec, meta) {
    const off = meta && Number(meta.priceToBeat);
    if (!(off > 0) || rec.line_src === "official") return;
    rec.line = off; rec.line_src = "official";
    const dd = L.distance(rec.cl_click, off, rec.click_side, rec.click_s);
    if (dd) Object.assign(rec, { dist_usd: dd.usd, dist_bps: dd.bps, dist_z: dd.z });
  }
  // 已經離開頁面的場次：收盤後補官方開盤價和贏家（損益要停在那一頁才算得到）
  async function backfill() {
    const now = Date.now() / 1000;
    for (const rec of Object.values(store.journal)) {
      if (rec.line_src === "official" && rec.winner && (rec.mode !== "sim" || store.settledSim[rec.slug])) continue;
      const start = Number(String(rec.slug).split("-").pop()); if (!(now > start + 320) || now > start + 86400 * 3) continue;
      try {
        const d = await (await fetch(`https://gamma-api.polymarket.com/events?slug=${rec.slug}`)).json(); if (!d || !d.length) continue;
        fillLine(rec, d[0].eventMetadata);
        if (!rec.winner) { const m = d[0].markets[0]; const pr = JSON.parse(m.outcomePrices || "[]"); const i = pr.findIndex(x => Number(x) >= 0.99); if (i >= 0) rec.winner = JSON.parse(m.outcomes)[i]; }
        const sim = store.sim[rec.slug];                 // 練習的場次：成交都存在本機，離開頁面也能結算
        if (rec.mode === "sim" && rec.winner && sim && sim.trades.length && !store.settledSim[rec.slug]) {
          const ps = L.positionFrom(sim.trades), res = L.settle(ps, rec.winner, !!(sim.leg2 && !sim.leg2.taker));
          store.settledSim[rec.slug] = Object.assign({ winner: rec.winner, day: new Date(start * 1000).toISOString().slice(0, 10), up: ps.Up.shares, down: ps.Down.shares }, res);
          Object.assign(rec, { paired: res.paired ? 1 : 0, pnl: res.pnl });
        }
        save();
      } catch (e) {}
    }
  }

  // ---------- 讀頁面（2026-10-30 實測，未登入）----------
  // 結果按鈕 role="radio"（文字「Up 51¢」）；買入／賣出也是 role="radio"；
  // 下單模式選單：button[aria-haspopup=menu] 文字為 1-Tap／盤口（市價）／限價，選項是 role="menuitemradio"，鍵盤 Enter 才打得開；
  // 1-Tap：按鈕「$5 win $9.80」；市價：金額欄 placeholder「$0」；限價：價格欄 placeholder「0¢」（單位分）、份額欄 placeholder「0」；
  // 送出按鈕未登入時文字是「交易」，登入後可能是「買入 Up」之類，兩種都認。
  const norm = b => (b.innerText || b.textContent || "").trim().replace(/\s+/g, " ");
  const cap1 = x => x[0].toUpperCase() + x.slice(1).toLowerCase();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const radios = re => [...document.querySelectorAll('button[role="radio"]')].filter(b => re.test(norm(b)));
  const isOn = b => b.getAttribute("aria-checked") === "true" || b.getAttribute("data-state") === "checked";
  const outcomeRadios = () => radios(/^(Up|Down)\b/i);
  function checkedOutcome() { const r = outcomeRadios().find(isOn); return r ? cap1(norm(r).split(" ")[0]) : null; }
  const sellTabOn = () => radios(/^(賣出|Sell)$/i).some(isOn);
  const MODE_RE = /^(1-Tap|盤口|市價|限價|Market|Limit)$/i;
  const modeTrigger = () => [...document.querySelectorAll('button[aria-haspopup="menu"]')].find(b => MODE_RE.test(norm(b)));
  function currentMode() { const t = modeTrigger(); if (!t) return null; const x = norm(t); return /1-Tap/i.test(x) ? "tap" : /限價|Limit/i.test(x) ? "limit" : "market"; }
  const MODE_NAMES = { tap: /^1-Tap$/i, market: /^(盤口|市價|Market)$/i, limit: /^(限價|Limit)$/i };
  async function setMode(m) {
    if (currentMode() === m) return true;
    const t = modeTrigger(); if (!t) return false;
    const key = (el, k) => el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
    if (!document.querySelector('[role="menuitemradio"]')) { t.focus(); key(t, "Enter"); await sleep(200); }
    const it = [...document.querySelectorAll('[role="menuitemradio"]')].find(e => MODE_NAMES[m].test(norm(e)));
    if (!it) return false;
    it.click(); await sleep(200);
    const still = document.querySelector('[role="menuitemradio"]'); if (still) { key(still, "Escape"); await sleep(150); }
    return currentMode() === m;
  }
  const decInputs = () => [...document.querySelectorAll('input[inputmode="decimal"], input[type="text"], input[type="number"]')].filter(i => !/搜尋|search/i.test(i.placeholder || "") && i.offsetParent);
  const priceInput = () => decInputs().find(i => /¢/.test(i.placeholder || ""));
  const sharesInput = () => decInputs().find(i => (i.placeholder || "") === "0");
  const amountInput = () => decInputs().find(i => /\$/.test(i.placeholder || ""));
  const numOf = inp => { if (!inp) return null; const v = Number(String(inp.value).replace(/[^0-9.]/g, "")); return String(inp.value).trim() === "" || !isFinite(v) ? null : v; };
  const SUBMIT_RE = /^(交易|Trade|(買入|Buy)\s+(Up|Down)\b.*)$/i;
  const ONETAP_RE = /^\$\s*([0-9]+(?:\.[0-9]+)?)\s+(win|贏)/i;
  const findSubmit = () => [...document.querySelectorAll("button:not([role])")].find(b => SUBMIT_RE.test(norm(b)) && b.offsetParent);
  const findOneTap = amount => [...document.querySelectorAll("button")].find(b => { const m = norm(b).match(ONETAP_RE); return m && Number(m[1]) === amount; });
  const setVal = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  function fill(inp, v) { inp.focus(); setVal.call(inp, String(v)); inp.dispatchEvent(new Event("input", { bubbles: true })); inp.dispatchEvent(new Event("change", { bubbles: true })); inp.blur(); }

  // 被點的是不是買入：1-Tap 金額按鈕，或送出按鈕（市價／限價）。回傳要檢查的內容。
  function classify(el) {
    const b = el && el.closest ? el.closest("button") : null; if (!b || b.getAttribute("role")) return null;
    const t = norm(b); let m = t.match(ONETAP_RE);
    if (m) return { el: b, kind: "market", side: checkedOutcome(), amount: Number(m[1]) };
    if (!SUBMIT_RE.test(t)) return null;
    if (sellTabOn()) return { sell: true };
    m = t.match(/(Up|Down)\b/i); const side = m ? cap1(m[1]) : checkedOutcome();
    if (currentMode() === "limit") {
      const c = numOf(priceInput()); return { el: b, kind: "limit", side, price: c == null ? null : c / 100, shares: numOf(sharesInput()) };
    }
    return { el: b, kind: "market", side, amount: numOf(amountInput()) };
  }

  // ---------- 擋違規 ----------
  function recordClick(side) {                        // 記下按下第一張那一刻的盤面，之後分析進場判斷
    const s = Date.now() / 1000 - mkt.start, a = asksNow();
    const path = hist.filter(h => h[0] >= s - 60).filter((h, i) => i % 2 === 0).map(h => `${h[0]}:${h[1] ?? ""}/${h[2] ?? ""}`).join(";");
    const cl = clNow(), dd = L.distance(cl, clLine, side, s);
    const dr = L.recentDrop(hist, side, s);
    Object.assign(recOf(), { click_s: s, click_side: side, click_up_ask: a.Up, click_dn_ask: a.Down, path60: path, cl_click: cl, drop20: dr ? dr.drop : null },
                  clLine != null ? { line: clLine, line_src: "live" } : {}, dd ? { dist_usd: dd.usd, dist_bps: dd.bps, dist_z: dd.z } : {});
    save();
  }
  function guard(e) {
    if (!mkt || !alive()) return; const bb = classify(e.target); if (!bb || bb.sell) return;
    const block = why => { e.preventDefault(); e.stopImmediatePropagation(); flash("擋下：" + why); warn("擋下：" + why); };
    if (practice()) return block("練習模式中不會真的下單；要真的下單，先在面板上關掉練習模式");
    if (!cfg.addr) return block("先在插件設定填你的錢包地址，插件才知道你買了什麼");
    if (Date.now() < pendingUntil) return block("上一張剛送出，等成交紀錄更新（約 5 秒）");
    const d = lastDecision; if (!d) return block("還沒收到盤口");
    if (!bb.side) return block("讀不到現在選的是 Up 還是 Down");
    if (!d.allow[bb.side]) return block(d.msg);
    if (bb.kind === "limit") {
      if (d.phase === "first") return block("第一張不可以用限價單，請用市價或 1-Tap");
      const lim = d.limit[bb.side]; if (!lim) return block("這一邊現在不能掛限價");
      if (bb.price == null || bb.shares == null) return block("讀不到限價或份額");
      if (bb.price > lim.price + 1e-9) return block(`限價 ${bb.price.toFixed(2)} 高於上限 ${lim.price.toFixed(2)}`);
      if (bb.shares > lim.shares + 0.01) return block(`份額 ${bb.shares} 超過第一張的股數 ${lim.shares.toFixed(2)}，不准放大`);
      stOf(mkt.slug).leg2 = { price: bb.price, shares: bb.shares, s: Date.now() / 1000 - mkt.start };
      Object.assign(recOf(), { leg2_price: bb.price, leg2_shares: bb.shares, leg2_placed_s: Date.now() / 1000 - mkt.start }); save();
      warn(`已放行：掛 ${bb.side} 限價 ${bb.price.toFixed(2)} × ${bb.shares} 股`);
    } else {
      const max = d.market[bb.side];
      if (!(max > 0)) return block(d.phase === "second" ? "第二張請用限價掛單（空白鍵）；對面現在的價格吃單不賺錢" : d.msg);
      if (bb.amount == null) return block("讀不到金額，請確認金額欄有填");
      if (bb.amount > max + 1e-9) return block(`金額 $${bb.amount} 超過上限 $${max.toFixed(2)}，不准放大`);
      if (d.phase === "first") recordClick(bb.side);
      warn(`已放行：市價買 ${bb.side} $${bb.amount}`);
    }
    pendingUntil = Date.now() + 5000;
  }
  document.addEventListener("click", guard, true);
  document.addEventListener("keydown", e => {
    if (e.key !== "Enter" || !mkt || !alive()) return; const a = document.activeElement;
    if (a && a.tagName === "INPUT") { e.preventDefault(); e.stopImmediatePropagation(); flash("請用按鈕或空白鍵下單（輸入框裡的 Enter 已停用）"); }
  }, true);

  // ---------- 練習模式：照真實盤口模擬成交 ----------
  const askList = side => { const b = book[mkt.tokens[side]]; return b ? [...b.asks].map(([p, z]) => [p, z]) : []; };
  function simExec(sg) {
    const st = stOf(mkt.slug), now = Date.now() / 1000, s = now - mkt.start;
    if (sg.kind === "market" && sg.shares != null) {      // 第二張直接買：照賣單吃到同樣股數，不留掛單
      const f = L.fillShares(askList(sg.side), sg.shares, Infinity); if (!f) return flash("練習：對面沒有賣單");
      st.trades.push({ side: "BUY", outcome: sg.side, price: f.price, size: f.shares, timestamp: now });
      Object.assign(recOf(), { leg2_price: f.price, leg2_shares: sg.shares, leg2_placed_s: s, leg2_taker: 1, leg2_fill_strict: 1 });
      flash(f.shares >= sg.shares - 1e-9 ? `練習：直接買 ${sg.side} ${f.shares.toFixed(2)} 股，均價 ${f.price.toFixed(3)}，本場成對`
                                          : `練習：賣單不夠，只買到 ${f.shares.toFixed(2)} 股（均價 ${f.price.toFixed(3)}）`, "ok");
    } else if (sg.kind === "market") {
      const f = L.fillMarket(askList(sg.side), sg.amount); if (!f) return flash("練習：盤口不夠深，吃不到 $" + sg.amount);
      recordClick(sg.side);
      st.trades.push({ side: "BUY", outcome: sg.side, price: f.price, size: f.shares, timestamp: now });
      flash(`練習：市價買 ${sg.side} ${f.shares.toFixed(2)} 股，均價 ${f.price.toFixed(3)}`, "ok");
    } else {
      // 第二張：限價掛在當下賣價。盤口夠深就全部成交（吃單、收手續費）；不夠的股數照同一價格掛著，之後對面跌到這價格才成交（掛單、不收手續費）
      const f = L.fillShares(askList(sg.side), sg.shares, sg.price);
      st.leg2 = { price: sg.price, shares: sg.shares, s, filled: f ? f.shares : 0, taker: !!f };
      Object.assign(recOf(), { leg2_price: sg.price, leg2_shares: sg.shares, leg2_placed_s: s, leg2_taker: f ? 1 : 0 });
      if (f) st.trades.push({ side: "BUY", outcome: sg.side, price: f.price, size: f.shares, timestamp: now });
      if (f && f.shares >= sg.shares - 1e-9) { st.leg2.done = true; recOf().leg2_fill_strict = 1; flash(`練習：第二張成交 ${sg.side} ${f.shares.toFixed(2)} 股 @${f.price.toFixed(3)}，本場成對`, "ok"); }
      else flash(`練習：第二張先成交 ${(f ? f.shares : 0).toFixed(2)} 股，其餘 ${(sg.shares - (f ? f.shares : 0)).toFixed(2)} 股照 ${sg.price.toFixed(2)} 掛著等`, "ok");
    }
    save(); pos = L.positionFrom(st.trades); posToJournal();
  }
  // 練習的掛單：對面賣價跌到掛單價就算成交（和回放同算法）；「嚴格成交」= 跌破掛單價 1 分以上，比較保守
  function simTick(s) {
    if (!practice() || !mkt || s >= 300) return;
    const st = stOf(mkt.slug), lg = st.leg2; if (!lg || lg.done) return;
    const side = L.other(pos.first || ""), a = side ? best(mkt.tokens[side], "ask") : null;
    if (a != null && a <= lg.price - 0.01 + 1e-9) lg.strict = 1;
    // 這個價位如果已被我們吃過（部分成交），盤口上看到的那幾股其實已經沒了，要等對面跌破這價格才算剩下的成交
    if (a == null || (lg.filled > 0 ? a >= lg.price - 1e-9 : a > lg.price + 1e-9)) return;
    const left = lg.shares - (lg.filled || 0); if (left <= 1e-9) { lg.done = true; return; }
    st.trades.push({ side: "BUY", outcome: side, price: lg.price, size: left, timestamp: Date.now() / 1000 });
    lg.filled = lg.shares; lg.done = true; recOf().leg2_fill_strict = lg.strict ? 1 : 0;
    save(); pos = L.positionFrom(st.trades); posToJournal();
    flash(`練習：第二張剩下的 ${left.toFixed(2)} 股成交 @${lg.price.toFixed(2)}，本場成對`, "ok");
  }

  // ---------- 一鍵下單（人按了才執行；送出時一樣經過上面的檢查）----------
  let busy = false;
  async function quickBuy(force) {
    if (busy || !mkt) return; const d = lastDecision;
    if (!practice() && !cfg.addr) return flash("先在插件設定填你的錢包地址");
    if (!d) return flash("還沒收到盤口");
    if (!practice() && Date.now() < pendingUntil) return flash("上一張剛送出，等成交紀錄更新");
    if (force && d.phase !== "first") return flash("U／D 只用在第一張；第二張按空白鍵");
    const sg = force ? (d.allow[force] ? { kind: "market", side: force, amount: d.market[force] } : null) : d.suggest;
    if (!sg) return flash("現在不能下單：" + d.msg);
    if (practice()) return simExec(sg);
    busy = true;
    try {
      const buyTab = radios(/^(買入|Buy)$/i)[0]; if (buyTab && !isOn(buyTab)) { buyTab.click(); await sleep(120); }
      if (sg.kind === "market") {
        const tapOk = sg.shares == null && await setMode("tap");   // 第二張金額不是整數，直接用市價單
        const ob = outcomeRadios().find(b => new RegExp("^" + sg.side + "\\b", "i").test(norm(b))); if (!ob) return flash(`找不到「${sg.side}」按鈕`);
        if (!isOn(ob)) { ob.click(); await sleep(150); }
        const t = tapOk && findOneTap(sg.amount); if (t) { t.click(); return; }
        if (!(await setMode("market"))) return flash("切不到市價模式");
        const ob2 = outcomeRadios().find(b => new RegExp("^" + sg.side + "\\b", "i").test(norm(b))); if (ob2 && !isOn(ob2)) { ob2.click(); await sleep(150); }
        const inp = amountInput(); if (!inp) return flash("找不到金額欄");
        fill(inp, sg.amount); await sleep(200);
        const sb = findSubmit(); if (!sb) return flash("找不到送出按鈕"); sb.click();
      } else {
        if (!(await setMode("limit"))) return flash("切不到限價模式");
        const ob = outcomeRadios().find(b => new RegExp("^" + sg.side + "\\b", "i").test(norm(b))); if (!ob) return flash(`找不到「${sg.side}」按鈕`);
        if (!isOn(ob)) { ob.click(); await sleep(150); }
        const pi = priceInput(), si = sharesInput(); if (!pi || !si) return flash("找不到限價或份額欄");
        fill(pi, Math.round(sg.price * 100)); fill(si, sg.shares.toFixed(2)); await sleep(250);
        const sb = findSubmit(); if (!sb) return flash("找不到送出按鈕"); sb.click();
      }
    } finally { setTimeout(() => { busy = false; }, 400); }
  }
  panel.querySelector("#pp-go").onclick = e => { e.preventDefault(); e.stopPropagation(); quickBuy(); };
  document.addEventListener("keydown", e => {
    if (!mkt || e.repeat || !alive()) return;
    const a = document.activeElement;
    if (a && ((a.tagName === "INPUT" && !/^(checkbox|radio|button)$/i.test(a.type)) || a.tagName === "TEXTAREA" || a.isContentEditable)) return;
    const f = e.code === "Space" ? null : e.code === "KeyU" ? "Up" : e.code === "KeyD" ? "Down" : undefined;
    if (f === undefined || e.metaKey || e.ctrlKey || e.altKey) return;
    e.preventDefault(); e.stopImmediatePropagation(); quickBuy(f);
  }, true);

  // ---------- 主迴圈 ----------
  let lastPath = "";
  async function onRoute() {
    const m = location.pathname.match(SLUG_RE); lastPath = location.pathname;
    if (!m) { mkt = null; connect(); connectCl(); render(); return; }
    if (mkt && mkt.slug === m[1]) return;
    mkt = null; pos = L.positionFrom([]); hist = []; clPts = []; clLine = null; render();
    try { mkt = await loadMarket(m[1]); } catch (e) { mkt = null; }
    connect(); connectCl(); loadTrades();
  }
  // 只在內容真的變了才寫進面板：避免每 0.25 秒整塊重寫，讓 Polymarket 自己的倒數動畫跟著重新排版（fish 10-30 遇到倒數顯示 16 分）
  const $ = s => panel.querySelector(s);
  const setT = (sel, v) => { const e = $(sel); if (e && e.textContent !== v) e.textContent = v; };
  const setH = (sel, v) => { const e = $(sel); if (e && e.__h !== v) { e.innerHTML = v; e.__h = v; } };
  const setC = (sel, v) => { const e = $(sel); if (e && e.className !== v) e.className = v; };
  const setD = (sel, v) => { const e = $(sel); if (e && e.style.display !== v) e.style.display = v; };
  function render() {
    mount();
    if (!alive()) {                                   // 舊版程式：停用並提示
      setT("#pp-title", `開盤配對助手 v${VER}（舊版，已停用）`);
      setT("#pp-msg", "插件已更新：請按 Cmd+R 重新整理這一頁，才會換成新版。舊版已停止接管空白鍵和下單按鈕。");
      setC("#pp-msg", "pp-msg stop"); $("#pp-go").disabled = true; setT("#pp-go", "請按 Cmd+R 重新整理頁面");
      lastDecision = null; return;
    }
    if (!mkt) { setT("#pp-title", `開盤配對助手 v${VER}`); setT("#pp-time", ""); setT("#pp-msg", "打開任何一場 BTC／ETH／SOL 5 分鐘盤"); setC("#pp-msg", "pp-msg"); $("#pp-go").disabled = true; lastDecision = null; return; }
    const s = Date.now() / 1000 - mkt.start, asks = asksNow();
    const si = Math.floor(s); if (!hist.length || hist[hist.length - 1][0] !== si) { hist.push([si, asks.Up, asks.Down]); if (hist.length > 400) hist.shift(); }
    simTick(s);
    let fillPrice = null;                                     // 第二張買滿同樣股數的平均價（照盤口）
    if (pos.first) {
      const s1 = pos.first, s2 = L.other(s1), need = pos[s1].shares - pos[s2].shares;
      const f = need > 0 ? L.fillShares(askList(s2), need, Infinity) : null; if (f) fillPrice = f.price;
    }
    const st = stOf(mkt.slug), d = L.decide(s, asks, pos, Object.assign(lcfg(), { fillPrice }), st);
    lastDecision = d;
    const hh = new Date(mkt.start * 1000); setT("#pp-title", `${mkt.asset} ${String(hh.getHours()).padStart(2, "0")}:${String(hh.getMinutes()).padStart(2, "0")} 場 · v${VER}`);
    setT("#pp-time", s < 0 ? `開盤前 ${Math.ceil(-s)}s` : `開盤後 +${Math.floor(s)}s`);
    const hot = x => d.suggest && d.suggest.side === x;
    const dr = { Up: L.recentDrop(hist, "Up", s), Down: L.recentDrop(hist, "Down", s) };
    const drTxt = x => x == null ? "近20秒：資料不足" : x.drop >= 0.005 ? `近20秒跌 ${x.drop.toFixed(2)}` : "近20秒沒跌";
    const drCls = x => x == null ? "" : x.drop >= L.DROP_MIN - 1e-9 ? " deep" : " shallow";
    setH("#pp-up", `Up 賣價<b>${asks.Up == null ? "—" : asks.Up.toFixed(2)}</b><i>${drTxt(dr.Up)}</i>`); setC("#pp-up", "pp-ask" + (hot("Up") ? " hot" : "") + drCls(dr.Up));
    setH("#pp-dn", `Down 賣價<b>${asks.Down == null ? "—" : asks.Down.toFixed(2)}</b><i>${drTxt(dr.Down)}</i>`); setC("#pp-dn", "pp-ask" + (hot("Down") ? " hot" : "") + drCls(dr.Down));
    const lv = d.live;
    if (lv && lv.kind === "lock") {
      setH("#pp-lock", `現在補 ${lv.side}<b>${lv.pct >= 0 ? "+" : ""}${(lv.pct * 100).toFixed(1)}%</b><i>每股 ${lv.perShare >= 0 ? "+" : ""}${(lv.perShare * 100).toFixed(1)} 分</i>`);
      setC("#pp-lock", "pp-ask pp-lock " + (lv.perShare > 0 ? "up" : "down"));
    } else if (lv && lv.kind === "sum") {
      setH("#pp-lock", `兩邊相加<b>${lv.sum.toFixed(2)}</b><i>買第一張後顯示利潤</i>`); setC("#pp-lock", "pp-ask pp-lock");
    } else { setH("#pp-lock", `現在補<b>—</b><i></i>`); setC("#pp-lock", "pp-ask pp-lock"); }
    const ready = practice() || !!cfg.addr, P = practice() ? "練習｜" : "";
    let warnTxt = "";
    if (d.phase === "first" && d.suggest) {
      const x = dr[d.suggest.side];
      warnTxt = x == null ? `　⚠ 近 20 秒資料不足，看不出 ${d.suggest.side} 跌了多少。`
        : x.drop < L.DROP_MIN - 1e-9 ? `　⚠ ${d.suggest.side} 近 20 秒只跌 ${x.drop.toFixed(2)}（成功的場次多半跌 0.10 以上才買；輸的 11:10 那場只跌 0.06）。` : "";
    }
    setT("#pp-msg", ready ? P + d.msg + warnTxt : "先到插件設定填你的錢包地址（右鍵插件圖示 → 選項）");
    setC("#pp-msg", "pp-msg " + (ready ? d.level : "stop"));
    const go = $("#pp-go"), sg = ready ? d.suggest : null;
    if (go.disabled !== !sg) go.disabled = !sg;
    if (go.classList.contains("warn") !== !!warnTxt) go.classList.toggle("warn", !!warnTxt);
    setT("#pp-go", !sg ? "現在沒有可以做的" : P + (sg.kind === "market" && sg.shares == null ? `空白鍵：市價買 ${sg.side} $${sg.amount}（U／D 指定邊）`
      : `空白鍵：直接買 ${sg.side} ${sg.shares.toFixed(2)} 股（${lv && lv.kind === "lock" ? (lv.pct >= 0 ? "+" : "") + (lv.pct * 100).toFixed(1) + "%" : ""}）`));
    if (document.activeElement !== modeBox && modeBox.checked !== practice()) modeBox.checked = practice();
    setC("#pp-modebox", "pp-mode " + (practice() ? "sim" : "live"));
    setD("#pp-reset", d.phase === "waiting" ? "" : "none");
    const cl = clNow(), dl = L.distance(cl, clLine, "Up", s);
    setT("#pp-reset", practice() ? "取消練習的第二張掛單" : "第二張重掛（先在頁面上取消舊掛單）");
    setT("#pp-line", cl == null ? "幣價：連線中…" : clLine == null ? `幣價 ${cl.toFixed(2)}｜開盤線：要在開盤前 1 分鐘內打開頁面才算得出來（收盤後紀錄會補官方值）`
      : `幣價 ${cl.toFixed(2)}｜離開盤線 ${dl.usd >= 0 ? "+" : ""}${dl.usd.toFixed(2)}（${dl.usd >= 0 ? "偏 Up" : "偏 Down"}，標準化 ${dl.z.toFixed(2)}）`);
    setT("#pp-pos", `部位：Up ${pos.Up.shares.toFixed(2)} 股${pos.Up.avg ? " @" + pos.Up.avg.toFixed(3) : ""}｜Down ${pos.Down.shares.toFixed(2)} 股${pos.Down.avg ? " @" + pos.Down.avg.toFixed(3) : ""}`);
    const today = new Date().toISOString().slice(0, 10); const rs = Object.values(settledMap()).filter(r => r.day === today);
    const tag = practice() ? "今日練習" : "今日真實";
    if (rs.length) {
      const pnl = rs.reduce((a, r) => a + r.pnl, 0), paired = rs.filter(r => r.paired).length;
      setT("#pp-stats", `${tag}：${rs.length} 場，成對 ${paired}、沒補到 ${rs.length - paired}，損益 ${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`);
    } else setT("#pp-stats", `${tag}：尚無結算`);
  }
  setInterval(() => { if (location.pathname !== lastPath) onRoute(); render(); }, 250);
  setInterval(() => { loadTrades(); checkSettled(); }, 2000);
  setInterval(backfill, 30000);
  onRoute();
})();
