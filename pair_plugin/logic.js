// 開盤配對規則（純函式，content script 和測試共用）。
// 人工模式（fish 2026-10-30）：
//   第一張：什麼時候買、買哪一邊由人決定，只能用市價（1-Tap 或市價單），金額固定，不准放大。
//   第二張：插件算好「同樣股數」和掛單價 p2 = 1 − p1 − 第一張手續費 − 目標利潤，人按空白鍵掛限價單（掛單不收手續費）。
//   抱到結算拿 $1。同一場只做一組；不准加買第一張那一邊。
var PairLogic = (function () {
  const FEE_RATE = 0.07;                         // Polymarket 加密盤吃單手續費：7% × p × (1 − p) × 股數，另外加收；掛單不收
  const DEFAULTS = { AMOUNT: 5, MARGIN: 0.07, LAST: 240, LEG2_SLACK: 0.03 };
  const EPS_SH = 0.05;                           // 兩邊股數差在這以內就算成對（市價買會有零碎股數）

  function fee(p) { return FEE_RATE * p * (1 - p); }
  function pairProfit(p1, p2) { return 1 - p1 - p2 - fee(p1) - fee(p2); }       // 兩張都吃單
  function makerProfit(p1, p2) { return 1 - p1 - p2 - fee(p1); }                // 第二張掛單

  // 第二張掛單價：扣掉第一張手續費後，每股至少留 margin 的利潤，取整到 1 分
  function leg2Price(p1, margin) { return Math.floor((1 - p1 - fee(p1) - margin) * 100 + 1e-9) / 100; }
  // 第二張若用市價（吃單），最高可以買到多少錢還賺錢（> 0.001）
  function leg2Cap(p1) {
    let lo = 0, hi = 1;
    for (let i = 0; i < 50; i++) { const mid = (lo + hi) / 2; if (pairProfit(p1, mid) > 0.001) lo = mid; else hi = mid; }
    return Math.floor(lo * 100 + 1e-9) / 100;
  }

  // 從自己的成交紀錄整理出兩邊的股數、平均價、第一張是哪邊、成交時間
  function positionFrom(trades) {
    const mk = () => ({ shares: 0, cost: 0, firstTs: null, lastTs: null });
    const pos = { Up: mk(), Down: mk(), first: null };
    const buys = (trades || []).filter(t => String(t.side).toUpperCase() === "BUY").sort((a, b) => a.timestamp - b.timestamp);
    for (const t of buys) {
      const o = t.outcome === "Up" ? "Up" : t.outcome === "Down" ? "Down" : null;
      if (!o) continue;
      pos[o].shares += Number(t.size); pos[o].cost += Number(t.size) * Number(t.price);
      if (pos[o].firstTs == null) pos[o].firstTs = Number(t.timestamp);
      pos[o].lastTs = Number(t.timestamp);
      if (!pos.first) pos.first = o;
    }
    for (const o of ["Up", "Down"]) pos[o].avg = pos[o].shares ? pos[o].cost / pos[o].shares : null;
    return pos;
  }

  function other(side) { return side === "Up" ? "Down" : "Up"; }
  const f2 = x => (x == null ? "—" : x.toFixed(2));

  // 現在可以做什麼。sFromOpen = 現在 − 開盤時間（秒）；st = 插件記住的這一場狀態（st.leg2 = {price, shares} 表示第二張已掛）
  // allow[side]：這邊能不能買；market[side]：市價最多花多少美元；limit[side]：{price, shares} 限價最高價和最多股數；
  // suggest：空白鍵要做的事。
  function decide(sFromOpen, asks, pos, cfg, st) {
    cfg = Object.assign({}, DEFAULTS, cfg || {}); st = st || {};
    const res = { phase: "", msg: "", level: "wait", allow: { Up: false, Down: false }, market: { Up: 0, Down: 0 }, limit: { Up: null, Down: null }, suggest: null };
    if (pos.Up.shares > 0 && pos.Down.shares > 0 && Math.abs(pos.Up.shares - pos.Down.shares) <= EPS_SH) {
      const s1 = pos.first, s2 = other(s1), sh = Math.min(pos.Up.shares, pos.Down.shares);
      const locked = sh * (1 - pos.Up.avg - pos.Down.avg) - pos[s1].shares * fee(pos[s1].avg) - (st.leg2 ? 0 : pos[s2].shares * fee(pos[s2].avg));
      res.phase = "done"; res.level = "done";
      res.msg = `本場已成對：Up ${pos.Up.shares.toFixed(2)} 股 @${pos.Up.avg.toFixed(3)}，Down ${pos.Down.shares.toFixed(2)} 股 @${pos.Down.avg.toFixed(3)}，鎖定約 $${locked.toFixed(2)}。本場不再下單。`;
      return res;
    }
    if (sFromOpen >= 300) { res.phase = "closed"; res.level = "stop"; res.msg = "本場已收盤。"; return res; }
    if (!pos.first) {
      if (sFromOpen < 0) { res.phase = "pre"; res.msg = `等開盤（還有 ${Math.ceil(-sFromOpen)} 秒）`; return res; }
      if (sFromOpen > cfg.LAST) { res.phase = "late"; res.level = "stop"; res.msg = `開盤已超過 ${cfg.LAST} 秒，本場不開第一張。`; return res; }
      res.phase = "first"; res.level = "ready";
      for (const s of ["Up", "Down"]) if (asks[s] != null) { res.allow[s] = true; res.market[s] = cfg.AMOUNT; }
      const cheap = asks.Up == null ? "Down" : asks.Down == null ? "Up" : (asks.Up <= asks.Down ? "Up" : "Down");
      if (asks[cheap] == null) { res.msg = "等盤口報價"; return res; }
      res.suggest = { kind: "market", side: cheap, amount: cfg.AMOUNT };
      res.msg = `進場時機你決定。空白鍵＝市價買較便宜的 ${cheap} @${f2(asks[cheap])}（$${cfg.AMOUNT}）；U／D 鍵指定邊。` +
        `買在 ${f2(asks[cheap])}，對面要跌到 ${f2(leg2Price(asks[cheap], cfg.MARGIN))} 才補得到。`;
      return res;
    }
    // 已有第一張，處理第二張
    const s1 = pos.first, s2 = other(s1), p1 = pos[s1].avg, a2 = asks[s2];
    const need = Math.floor((pos[s1].shares - pos[s2].shares) * 100) / 100;
    const p2 = leg2Price(p1, cfg.MARGIN);
    res.phase = "second"; res.p2 = p2; res.need = need;
    if (need < EPS_SH) { res.phase = "done"; res.level = "done"; res.msg = "兩邊股數已一樣，本場不再下單。"; return res; }
    if (st.leg2) {
      res.phase = "waiting";
      res.msg = `第二張掛單已送出：${s2} @${f2(st.leg2.price)} × ${st.leg2.shares.toFixed(2)} 股，已成交 ${pos[s2].shares.toFixed(2)} 股。` +
        `等 ${s2} 賣價跌到 ${f2(st.leg2.price)}（現在 ${f2(a2)}）。不准加買 ${s1}。`;
      return res;
    }
    res.allow[s2] = true; res.limit[s2] = { price: p2, shares: need };
    if (a2 != null && pairProfit(p1, a2) > 0.001) res.market[s2] = Math.round(need * a2 * (1 + cfg.LEG2_SLACK) * 100) / 100;
    res.suggest = { kind: "limit", side: s2, price: p2, shares: need };
    res.level = "go";
    const cross = a2 != null && a2 <= p2 + 1e-9;
    res.msg = `已買 ${s1} ${pos[s1].shares.toFixed(2)} 股 @${p1.toFixed(3)}。空白鍵＝掛 ${s2} 限價 @${f2(p2)} × ${need.toFixed(2)} 股` +
      `（零手續費，成交後每股賺約 ${(makerProfit(p1, p2) * 100).toFixed(1)} 分）。` +
      (cross ? `對面現在 ${f2(a2)} 已低於掛單價，會立刻吃單成交（收手續費，每股約賺 ${(pairProfit(p1, a2) * 100).toFixed(1)} 分）。` : `對面現在 ${f2(a2)}。`) +
      `不准加買 ${s1}。`;
    return res;
  }

  // 結算後這一場的損益（第一張收吃單手續費；第二張若是掛單不收）
  function settle(pos, winner, leg2Maker) {
    const cost = pos.Up.cost + pos.Down.cost;
    let fees = 0;
    for (const o of ["Up", "Down"]) if (pos[o].shares && !(leg2Maker && o !== pos.first)) fees += fee(pos[o].avg) * pos[o].shares;
    const payout = winner === "Up" ? pos.Up.shares : winner === "Down" ? pos.Down.shares : 0;
    const paired = pos.Up.shares > 0 && pos.Down.shares > 0 && Math.abs(pos.Up.shares - pos.Down.shares) <= EPS_SH;
    return { cost, fees, payout, pnl: payout - cost - fees, paired };
  }

  // 幣價離開盤基準線多遠，正 = 幣價站在 side 這一邊（side 是 Up 時幣價高於基準線為正）。
  // z = |bps| / √剩餘秒數（和 Docs/custom-queries/pair_undecided_check.py 同算法），越小代表結果越沒定。
  function distance(cl, line, side, sFromOpen) {
    if (cl == null || line == null || !(line > 0)) return null;
    const sg = side === "Down" ? -1 : 1, usd = (cl - line) * sg, bps = usd / line * 1e4;
    return { usd, bps, z: Math.abs(bps) / Math.sqrt(Math.max(300 - sFromOpen, 1)) };
  }
  // 開盤基準線：開盤前 60 秒 Chainlink 每秒價格的平均（和官方算法相同）。pts = [[秒, 價格], ...]；資料不到 45 秒就不算
  function lineFrom(pts, start) {
    const seen = new Map();
    for (const [sec, v] of pts) if (sec >= start - 60 && sec < start && v > 0) seen.set(sec, v);
    if (seen.size < 45) return null;
    let a = 0; for (const v of seen.values()) a += v; return a / seen.size;
  }

  // 練習模式：照盤口模擬市價成交。asks = [[價格, 股數], ...]（任意順序）。
  // byAmount：花 amount 美元能買到幾股、平均價；byShares：買 shares 股（只吃 ≤ maxPrice 的掛單）。吃不到回傳 null。
  function fillMarket(asks, amount) {
    const lv = asks.filter(a => a[1] > 0).sort((a, b) => a[0] - b[0]); let left = amount, sh = 0, cost = 0;
    for (const [p, sz] of lv) { const take = Math.min(sz, left / p); sh += take; cost += take * p; left -= take * p; if (left <= 1e-9) break; }
    return sh > 0 && left <= 1e-6 ? { price: cost / sh, shares: sh } : null;
  }
  function fillShares(asks, shares, maxPrice) {
    const lv = asks.filter(a => a[1] > 0 && a[0] <= maxPrice + 1e-9).sort((a, b) => a[0] - b[0]); let left = shares, cost = 0;
    for (const [p, sz] of lv) { const take = Math.min(sz, left); cost += take * p; left -= take; if (left <= 1e-9) break; }
    const got = shares - Math.max(left, 0);
    return got > 0 ? { price: cost / got, shares: got } : null;
  }

  // 每場紀錄匯出成 CSV（給之後分析 fish 的進場判斷）
  const COLS = ["mode", "slug", "asset", "leg1_side", "leg1_price", "leg1_shares", "leg1_s", "click_s", "click_side", "click_up_ask", "click_dn_ask",
                "cl_click", "line", "line_src", "dist_usd", "dist_bps", "dist_z", "path60",
                "leg2_price", "leg2_shares", "leg2_placed_s", "leg2_taker", "leg2_filled_shares", "leg2_fill_s", "leg2_fill_strict", "winner", "paired", "pnl"];
  function toCSV(records) {
    const esc = v => { if (v == null) return ""; const s = typeof v === "number" ? String(Math.round(v * 10000) / 10000) : String(v);
                       return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    return [COLS.join(",")].concat(records.map(r => COLS.map(c => esc(r[c])).join(","))).join("\n") + "\n";
  }

  return { FEE_RATE, DEFAULTS, EPS_SH, fee, pairProfit, makerProfit, leg2Price, leg2Cap, positionFrom, decide, settle, other, toCSV, COLS, distance, lineFrom, fillMarket, fillShares };
})();
if (typeof module !== "undefined") module.exports = PairLogic;
