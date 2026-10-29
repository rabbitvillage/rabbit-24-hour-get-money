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

test("second leg: only the other side, as a limit order with the same shares; leg-1 side locked", () => {
  const pos = L.positionFrom([buy("Up", 0.30, 16.66, 1793293515)]);
  let d = L.decide(20, { Up: 0.33, Down: 0.70 }, pos);
  assert.strictEqual(d.phase, "second");
  assert.deepStrictEqual(d.allow, { Up: false, Down: true });
  assert.deepStrictEqual(d.limit.Down, { price: 0.61, shares: 16.66 });
  assert.deepStrictEqual(d.suggest, { kind: "limit", side: "Down", price: 0.61, shares: 16.66 });
  assert.strictEqual(d.market.Down, 0);                                       // 0.70 as a taker loses money
  d = L.decide(20, { Up: 0.40, Down: 0.62 }, pos); assert.ok(d.market.Down > 0);  // a profitable taker fill is still allowed
  d = L.decide(25, { Up: 0.40, Down: 0.62 }, pos, null, { leg2: { price: 0.61, shares: 16.66 } });
  assert.strictEqual(d.phase, "waiting"); assert.deepStrictEqual(d.allow, { Up: false, Down: false });
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
  const csv = L.toCSV([{ slug: "btc-updown-5m-1", asset: "BTC", leg1_side: "Down", leg1_price: 0.4, path60: "1:0.5/0.5;3:0.4/0.6" }]);
  const [head, row] = csv.trim().split("\n");
  assert.strictEqual(head.split(",").length, L.COLS.length);
  assert.ok(row.startsWith("btc-updown-5m-1,BTC,Down,0.4,"));
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
