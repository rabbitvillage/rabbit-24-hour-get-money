// node --test tests/test_pair_plugin.js
const test = require("node:test"); const assert = require("node:assert");
const L = require("../pair_plugin/logic.js");

test("leg 2 cap keeps the pair profitable after both fees", () => {
  assert.strictEqual(L.leg2Cap(0.40), 0.56);
  assert.ok(L.pairProfit(0.40, 0.56) > 0); assert.ok(L.pairProfit(0.40, 0.57) < 0);
  assert.ok(L.leg2Cap(0.34) >= 0.62);
});

test("first leg only in the first minute, only the side at or under 0.40", () => {
  const empty = L.positionFrom([]);
  assert.strictEqual(L.decide(-10, { Up: 0.51, Down: 0.50 }, empty).phase, "pre");
  let d = L.decide(12, { Up: 0.39, Down: 0.62 }, empty);
  assert.deepStrictEqual(d.allow, { Up: true, Down: false }); assert.strictEqual(d.maxAmount.Up, 5);
  d = L.decide(12, { Up: 0.45, Down: 0.56 }, empty); assert.deepStrictEqual(d.allow, { Up: false, Down: false });
  d = L.decide(61, { Up: 0.30, Down: 0.71 }, empty); assert.strictEqual(d.phase, "late"); assert.deepStrictEqual(d.allow, { Up: false, Down: false });
});

test("second leg: only the other side, under the cap, same number of shares", () => {
  const pos = L.positionFrom([{ side: "BUY", outcome: "Up", price: 0.40, size: 12.5, timestamp: 1 }]);
  let d = L.decide(30, { Up: 0.44, Down: 0.58 }, pos);
  assert.deepStrictEqual(d.allow, { Up: false, Down: false }); assert.strictEqual(d.cap, 0.56);
  d = L.decide(30, { Up: 0.46, Down: 0.55 }, pos);
  assert.deepStrictEqual(d.allow, { Up: false, Down: true });
  assert.ok(Math.abs(d.maxAmount.Down - 12.5 * 0.55 * 1.03) < 0.02);
  d = L.decide(200, { Up: 0.46, Down: 0.55 }, pos); assert.strictEqual(d.allow.Down, true);   // leg 2 may come any time
});

test("a finished pair blocks everything and settles to the winner's shares", () => {
  const pos = L.positionFrom([
    { side: "BUY", outcome: "Up", price: 0.40, size: 12.5, timestamp: 1 },
    { side: "BUY", outcome: "Down", price: 0.55, size: 12.5, timestamp: 9 }]);
  const d = L.decide(40, { Up: 0.30, Down: 0.70 }, pos);
  assert.strictEqual(d.phase, "done"); assert.deepStrictEqual(d.allow, { Up: false, Down: false });
  const r = L.settle(pos, "Down");
  assert.ok(r.paired); assert.ok(Math.abs(r.pnl - (12.5 - 12.5 * 0.95 - 12.5 * (L.fee(0.40) + L.fee(0.55)))) < 1e-9);
  assert.ok(r.pnl > 0);
});
