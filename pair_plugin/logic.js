// 開盤配對規則（純函式，content script 和測試共用）。fish 2026-10-29：
//   開盤後第一分鐘，哪一邊先跌到 L1（0.40）就用市價買第一張；之後另一邊跌到「扣兩次手續費後仍賺錢」的價位再買同樣股數；
//   抱到結算拿 $1。每張金額固定，不准放大；同一場只做一組。
var PairLogic = (function () {
  const FEE_RATE = 0.07;                         // Polymarket 加密盤吃單手續費：7% × p × (1 − p) × 股數，另外加收
  const DEFAULTS = { L1: 0.40, FIRST: 60, AMOUNT: 5, LEG2_SLACK: 0.03 };

  function fee(p) { return FEE_RATE * p * (1 - p); }
  function pairProfit(p1, p2) { return 1 - p1 - p2 - fee(p1) - fee(p2); }

  // 第二張最高可以買到多少錢（每股），買到這個價以下就保證賺錢（> 0.001）
  function leg2Cap(p1) {
    let lo = 0, hi = 1;
    for (let i = 0; i < 50; i++) { const mid = (lo + hi) / 2; if (pairProfit(p1, mid) > 0.001) lo = mid; else hi = mid; }
    return Math.floor(lo * 100 + 1e-9) / 100;
  }

  // 從自己的成交紀錄整理出兩邊的股數、平均價和第一張
  function positionFrom(trades) {
    const pos = { Up: { shares: 0, cost: 0 }, Down: { shares: 0, cost: 0 }, first: null };
    const buys = (trades || []).filter(t => String(t.side).toUpperCase() === "BUY").sort((a, b) => a.timestamp - b.timestamp);
    for (const t of buys) {
      const o = t.outcome === "Up" ? "Up" : t.outcome === "Down" ? "Down" : null;
      if (!o) continue;
      pos[o].shares += Number(t.size); pos[o].cost += Number(t.size) * Number(t.price);
      if (!pos.first) pos.first = o;
    }
    for (const o of ["Up", "Down"]) pos[o].avg = pos[o].shares ? pos[o].cost / pos[o].shares : null;
    return pos;
  }

  function other(side) { return side === "Up" ? "Down" : "Up"; }

  // 現在該做什麼、哪一邊可以按買入、金額上限多少。sFromOpen = 現在 − 開盤時間（秒）
  function decide(sFromOpen, asks, pos, cfg) {
    cfg = Object.assign({}, DEFAULTS, cfg || {});
    const res = { phase: "", msg: "", allow: { Up: false, Down: false }, maxAmount: { Up: 0, Down: 0 }, cap: null, need: null, level: "wait" };
    const hasUp = pos.Up.shares > 0, hasDown = pos.Down.shares > 0;
    if (hasUp && hasDown) {
      const sh = Math.min(pos.Up.shares, pos.Down.shares);
      const locked = sh * (1 - pos.Up.avg - pos.Down.avg - fee(pos.Up.avg) - fee(pos.Down.avg));
      res.phase = "done"; res.level = "done";
      res.msg = `本場已成對：Up ${pos.Up.shares.toFixed(2)} 股 @${pos.Up.avg.toFixed(3)}，Down ${pos.Down.shares.toFixed(2)} 股 @${pos.Down.avg.toFixed(3)}，鎖定約 $${locked.toFixed(2)}。本場不再下單。`;
      return res;
    }
    if (!pos.first) {
      if (sFromOpen < 0) { res.phase = "pre"; res.msg = `等開盤（還有 ${Math.ceil(-sFromOpen)} 秒）`; return res; }
      if (sFromOpen > cfg.FIRST) { res.phase = "late"; res.level = "stop"; res.msg = `開盤已超過 ${cfg.FIRST} 秒，本場不開第一張。`; return res; }
      res.phase = "first";
      const hit = ["Up", "Down"].filter(s => asks[s] != null && asks[s] <= cfg.L1 + 1e-9);
      if (!hit.length) { res.msg = `等任一邊跌到 ${cfg.L1.toFixed(2)}（剩 ${Math.max(0, Math.floor(cfg.FIRST - sFromOpen))} 秒）`; return res; }
      for (const s of hit) { res.allow[s] = true; res.maxAmount[s] = cfg.AMOUNT; }
      res.level = "go";
      res.msg = `可以買第一張：${hit.map(s => `${s} @${asks[s].toFixed(2)}`).join("、")}（市價，金額 $${cfg.AMOUNT}）`;
      return res;
    }
    // 已有第一張，等第二張
    const s1 = pos.first, s2 = other(s1), p1 = pos[s1].avg;
    res.phase = "second"; res.cap = leg2Cap(p1); res.need = pos[s1].shares - pos[s2].shares;
    const a2 = asks[s2];
    if (a2 != null && a2 <= res.cap + 1e-9) {
      res.allow[s2] = true; res.level = "go";
      res.maxAmount[s2] = Math.round(res.need * a2 * (1 + cfg.LEG2_SLACK) * 100) / 100;
      res.msg = `可以買第二張：${s2} @${a2.toFixed(2)} ≤ ${res.cap.toFixed(2)}，買 ${res.need.toFixed(2)} 股（約 $${(res.need * a2).toFixed(2)}），每股賺約 ${(pairProfit(p1, a2) * 100).toFixed(1)} 分`;
    } else {
      res.msg = `已買 ${s1} ${pos[s1].shares.toFixed(2)} 股 @${p1.toFixed(3)}。等 ${s2} 跌到 ${res.cap.toFixed(2)} 以下（現在 ${a2 == null ? "—" : a2.toFixed(2)}）。不准加買 ${s1}。`;
    }
    return res;
  }

  // 結算後這一場的損益（含估計手續費）
  function settle(pos, winner) {
    const cost = pos.Up.cost + pos.Down.cost;
    const fees = (pos.Up.shares ? fee(pos.Up.avg) * pos.Up.shares : 0) + (pos.Down.shares ? fee(pos.Down.avg) * pos.Down.shares : 0);
    const payout = winner === "Up" ? pos.Up.shares : winner === "Down" ? pos.Down.shares : 0;
    const paired = pos.Up.shares > 0 && pos.Down.shares > 0;
    return { cost, fees, payout, pnl: payout - cost - fees, paired };
  }

  return { FEE_RATE, DEFAULTS, fee, pairProfit, leg2Cap, positionFrom, decide, settle, other };
})();
if (typeof module !== "undefined") module.exports = PairLogic;
