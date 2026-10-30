// node --test tests/test_pair_plugin.js
const test = require("node:test"); const assert = require("node:assert");
const L = require("../pair_plugin/logic.js");
const buy = (outcome, price, size, timestamp) => ({ side: "BUY", outcome, price, size, timestamp });

test("leg 2 limit price leaves the target after leg 1's taker fee; maker leg pays no fee", () => {
  assert.strictEqual(L.leg2Price(0.40, 0.07), 0.51);
  assert.strictEqual(L.leg2Price(0.31, 0.03), 0.64);
  assert.ok(L.makerProfit(0.40, 0.51) >= 0.07 - 1e-9);
  assert.ok(L.makerProfit(0.31, 0.64) > L.pairProfit(0.31, 0.64));   // fish 10-30: 31c + 64c only nets ~1c if both legs pay the fee
  assert.strictEqual(L.leg2Cap(0.40), 0.56);
});

test("first leg: the human picks the moment; market only, fixed amount; nothing before the open or after LAST", () => {
  const empty = L.positionFrom([]);
  assert.strictEqual(L.decide(-10, { Up: 0.51, Down: 0.50 }, empty).phase, "pre");
  let d = L.decide(12, { Up: 0.45, Down: 0.56 }, empty);
  assert.deepStrictEqual(d.allow, { Up: true, Down: true });
  assert.deepStrictEqual(d.market, { Up: 5, Down: 5 });
  assert.deepStrictEqual(d.limit, { Up: null, Down: null });
  assert.deepStrictEqual(d.suggest, { kind: "market", side: "Up", amount: 5 });
  d = L.decide(30, { Up: 0.72, Down: 0.29 }, empty, { AMOUNT: 25 }); assert.deepStrictEqual(d.suggest, { kind: "market", side: "Down", amount: 25 });
  d = L.decide(241, { Up: 0.30, Down: 0.71 }, empty); assert.strictEqual(d.phase, "late"); assert.deepStrictEqual(d.allow, { Up: false, Down: false });
  d = L.decide(100, { Up: 0.30, Down: 0.71 }, empty, { LAST: 60 }); assert.strictEqual(d.phase, "late");
});

test("second leg: the human picks the moment; the third number is the live profit of locking now", () => {
  const pos = L.positionFrom([buy("Up", 0.35, 14.29, 1793293515)]);
  let d = L.decide(60, { Up: 0.40, Down: 0.70 }, pos);
  assert.strictEqual(d.phase, "second");
  assert.deepStrictEqual(d.allow, { Up: false, Down: true });                       // leg-1 side locked
  assert.ok(d.live.perShare < 0 && Math.abs(d.live.perShare - L.pairProfit(0.35, 0.70)) < 1e-12);  // shown even when it loses
  assert.deepStrictEqual(d.suggest, { kind: "market", side: "Down", shares: 14.29, amount: 10.01, price: 0.70 });   // press = buy now, no resting order
  d = L.decide(60, { Up: 0.40, Down: 0.43 }, pos);
  const want = L.pairProfit(0.35, 0.43) / (0.35 + 0.43 + L.fee(0.35) + L.fee(0.43));
  assert.ok(Math.abs(d.live.pct - want) < 1e-12 && d.live.pct > 0.2);              // 0.35 + 0.43: about +23%
  assert.strictEqual(d.level, "go");
  d = L.decide(60, { Up: 0.40, Down: 0.555 }, pos); assert.strictEqual(d.limit.Down.price, 0.56);  // a manual limit may go up to the ask
  d = L.decide(60, { Up: 0.40, Down: 0.52 }, pos, { fillPrice: 0.5332 });            // thin book: the third number uses the average fill
  assert.ok(Math.abs(d.live.perShare - L.pairProfit(0.35, 0.5332)) < 1e-12 && d.suggest.amount === Math.ceil(14.29 * 0.5332 * 100) / 100);
  assert.ok(d.market.Down >= d.suggest.amount);
  d = L.decide(25, { Up: 0.40, Down: 0.62 }, pos, null, { leg2: { price: 0.55, shares: 14.29 } });
  assert.strictEqual(d.phase, "waiting"); assert.deepStrictEqual(d.allow, { Up: false, Down: false });
  assert.ok(d.live && d.live.kind === "lock");                                       // still shown while waiting
  assert.deepStrictEqual(L.decide(20, { Up: 0.45, Down: 0.56 }, L.positionFrom([])).live, { kind: "sum", sum: 1.01 });
});

test("partial fills keep waiting; equal shares finish the game; settlement skips the maker fee", () => {
  const st = { leg2: { price: 0.61, shares: 16.66 } };
  let pos = L.positionFrom([buy("Up", 0.30, 16.66, 1), buy("Down", 0.61, 6, 9)]);
  assert.strictEqual(L.decide(60, { Up: 0.3, Down: 0.6 }, pos, null, st).phase, "waiting");
  pos = L.positionFrom([buy("Up", 0.30, 16.66, 1), buy("Down", 0.61, 6, 9), buy("Down", 0.61, 10.66, 12)]);
  const d = L.decide(60, { Up: 0.2, Down: 0.8 }, pos, null, st);
  assert.strictEqual(d.phase, "done"); assert.deepStrictEqual(d.allow, { Up: false, Down: false });
  const r = L.settle(pos, "Up", true);
  assert.ok(r.paired);
  assert.ok(Math.abs(r.pnl - 16.66 * (1 - 0.30 - 0.61 - L.fee(0.30))) < 1e-9);
  assert.ok(Math.abs(L.settle(pos, "Up", false).fees - 16.66 * (L.fee(0.30) + L.fee(0.61))) < 1e-9);
});

test("unpaired single leg settles to all-or-nothing; journal exports as CSV", () => {
  const pos = L.positionFrom([buy("Down", 0.40, 12.5, 1)]);
  assert.ok(Math.abs(L.settle(pos, "Up", true).pnl - (-5 - 12.5 * L.fee(0.40))) < 1e-9);
  const csv = L.toCSV([{ mode: "sim", slug: "btc-updown-5m-1", asset: "BTC", leg1_side: "Down", leg1_price: 0.4, path60: "1:0.5/0.5;3:0.4/0.6" }]);
  const [head, row] = csv.trim().split("\n");
  assert.strictEqual(head.split(",").length, L.COLS.length);
  assert.ok(row.startsWith("sim,btc-updown-5m-1,BTC,Down,0.4,"));
});

test("opening line = mean of the 60 s before the open; distance is signed toward the bought side", () => {
  const start = 1000, pts = [];
  for (let sec = start - 70; sec < start + 5; sec++) pts.push([sec, sec < start - 60 ? 1 : sec < start ? 100 + (sec - (start - 60)) * 0.1 : 999]);
  const line = L.lineFrom(pts, start);
  assert.ok(Math.abs(line - (100 + 59 * 0.1 / 2)) < 1e-9);                       // only [start-60, start)
  assert.strictEqual(L.lineFrom(pts.filter(p => p[0] >= start - 40), start), null); // under 45 s of data: unknown
  const up = L.distance(102.95 + 10, 102.95, "Up", 75), dn = L.distance(102.95 + 10, 102.95, "Down", 75);
  assert.ok(Math.abs(up.usd - 10) < 1e-9 && Math.abs(dn.usd + 10) < 1e-9);
  assert.ok(Math.abs(up.bps - 10 / 102.95 * 1e4) < 1e-9);
  assert.ok(Math.abs(up.z - Math.abs(up.bps) / 15) < 1e-9);                       // √(300 − 75) = 15
  assert.strictEqual(L.distance(null, 100, "Up", 10), null);
  assert.strictEqual(L.distance(100, null, "Up", 10), null);
});

test("practice fills walk the real book: dollars for leg 1, shares capped at the limit for a crossing leg 2", () => {
  const asks = [[0.42, 5], [0.40, 10], [0.45, 100]];
  const f = L.fillMarket(asks, 5);                       // 10 @0.40 = $4, then $1 / 0.42 = 2.381 @0.42
  assert.ok(Math.abs(f.shares - (10 + 1 / 0.42)) < 1e-9 && Math.abs(f.price - 5 / f.shares) < 1e-9);
  assert.strictEqual(L.fillMarket([[0.40, 2]], 5), null);  // not enough on the book
  const g = L.fillShares(asks, 12, 0.42);                // 10 @0.40 + 2 @0.42
  assert.ok(Math.abs(g.shares - 12) < 1e-9 && Math.abs(g.price - (4 + 0.84) / 12) < 1e-9);
  const h = L.fillShares(asks, 20, 0.42); assert.ok(Math.abs(h.shares - 15) < 1e-9);   // only 15 at or under the limit
  assert.strictEqual(L.fillShares(asks, 5, 0.39), null);
});

test("market fills leave a few odd shares: within 3% still counts as a pair", () => {
  const pos = L.positionFrom([buy("Up", 0.35, 14.29, 1), buy("Down", 0.55, 13.95, 9)]);   // 0.34 short (2.4%)
  assert.strictEqual(L.decide(60, { Up: 0.4, Down: 0.6 }, pos).phase, "done");
  assert.ok(L.settle(pos, "Up").paired);
  const far = L.positionFrom([buy("Up", 0.35, 14.29, 1), buy("Down", 0.55, 10, 9)]);
  assert.strictEqual(L.decide(60, { Up: 0.4, Down: 0.6 }, far).phase, "second");
});

test("recent 20 s drop: high of the window minus now; unknown with under 10 s of data", () => {
  const h = []; for (let s = -5; s <= 10; s++) h.push([s, s > 0 ? 0.5 - s * 0.015 : 0.5, 0.5]);
  const r = L.recentDrop(h, "Up", 10);
  assert.ok(Math.abs(r.drop - 0.15) < 1e-9 && r.hi === 0.5 && r.span === 15);
  assert.ok(L.recentDrop(h, "Down", 10).drop === 0);
  assert.strictEqual(L.recentDrop(h.slice(-5), "Up", 10), null);
  const old = [[-30, 0.9, 0.1], ...h]; assert.ok(Math.abs(L.recentDrop(old, "Up", 10).drop - 0.15) < 1e-9);  // older than 20 s ignored
  assert.strictEqual(L.DROP_MIN, 0.10);
});
