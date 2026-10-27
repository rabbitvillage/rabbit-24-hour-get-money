"""Self-review (fish 2026-10-26): compare the last hours of real trading with what the bot's own history says to expect,
then decide one of three things and act on it:
  continue  results match expectations                                   -> keep going
  fix       results differ AND a known cause is found                    -> apply that cause's fix (or ask fish when only a
                                                                            human can fix it), notify
  stop      results clearly differ and no known cause explains it        -> emergency stop (kill file), notify
It only ever REDUCES trading: disable one rule, pause a coin for a while, place the kill file, or restart a stalled service.
Turning anything back on is left to a human. Run hourly by btc5m-review.timer:  python3 -m btc5m_sim.review [--dry]
Writes <data dir>/review_log.csv and sends Telegram when it acts (plus one daily all-clear at REVIEW_DAILY_HOUR)."""
import csv, json, math, os, statistics, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COINS = [c.split(":") for c in os.getenv("REVIEW_COINS", "BTC:data:btc5m-sim,SOL:data-sol:btc5m-sim@sol").split(",") if c.count(":") == 2]
WINDOW_H = float(os.getenv("REVIEW_WINDOW_H", "24"))       # judge the last 24 h ...
BASE_H = float(os.getenv("REVIEW_BASE_H", "72"))           # ... against the 72 h before it
P_DEFAULT = float(os.getenv("REVIEW_P_DEFAULT", "0.05"))   # expected loss rate when the baseline is too small
MIN_BASE = int(os.getenv("REVIEW_MIN_BASE", "30"))
TAIL = float(os.getenv("REVIEW_TAIL", "0.05"))             # "clearly worse than expected" = less than 5% likely by chance
LATE_GAP = float(os.getenv("REVIEW_LATE_GAP", "0.10"))     # a fill this much cheaper than the ask seen = the market moved first
VOL_X = float(os.getenv("REVIEW_VOL_X", "1.8"))            # price swings this many times the baseline = abnormal market
PAUSE_H = float(os.getenv("REVIEW_PAUSE_H", "2"))
STALE_MIN = float(os.getenv("REVIEW_STALE_MIN", "30"))
DAILY_HOUR = int(os.getenv("REVIEW_DAILY_HOUR", "9"))
# Only judge trades after this moment (ISO time, e.g. a fresh start after a settings change); empty = no limit.
SINCE = os.getenv("REVIEW_SINCE", "").strip()
HARD_ERRORS = ("restricted", "signer", "unauthorized", "invalid", "forbidden")
LFIELDS = ["ts", "coin", "decision", "reason", "action", "window_n", "window_losses", "window_pnl", "expected_loss_rate", "tail"]


def binom_tail(n, k, p):
    """P(X >= k) for X ~ Binomial(n, p): how likely this many losses (or more) are by plain bad luck."""
    if k <= 0: return 1.0
    p = min(max(p, 1e-6), 1 - 1e-6)
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def _ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)


def load(d):
    """Real (sent and filled) orders with their settled result; skipped/rejected orders with their reason."""
    orders = list(csv.DictReader(open(os.path.join(d, "live_orders.csv")))) if os.path.exists(os.path.join(d, "live_orders.csv")) else []
    filled = {(o["variant"], o["slug"]): o for o in orders if o.get("mode") == "live" and o.get("status") == "filled"}
    res = []
    p = os.path.join(d, "live_results.csv")
    for r in (csv.DictReader(open(p)) if os.path.exists(p) else []):
        o = filled.get((r["variant"], r["slug"]))
        if not o or r.get("won") not in ("0", "1"): continue
        try: fill = float(o["cost"]) / float(o["filled_size"])
        except (ValueError, ZeroDivisionError): fill = float(o["ask"] or 0)
        res.append(dict(t=_ts(r["ts"]), rule=r["variant"], slug=r["slug"], won=r["won"] == "1", pnl=float(r["pnl"] or 0),
                        fee=float(r.get("fee") or 0), ask=float(o["ask"] or 0), fill=fill))
    return orders, res


def swings(d, slugs):
    """Median price swing (bps) per market from the per-second book: how jumpy the market was."""
    if not slugs: return None
    lo, hi = {}, {}
    p = os.path.join(d, "sim_book.csv")
    if not os.path.exists(p): return None
    for r in csv.DictReader(open(p)):
        s = r["slug"]
        if s not in slugs or not r.get("spot"): continue
        try: v = float(r["spot"])
        except ValueError: continue
        lo[s] = min(lo.get(s, v), v); hi[s] = max(hi.get(s, v), v)
    xs = [(hi[s] - lo[s]) / lo[s] * 1e4 for s in lo if lo[s] > 0]
    return statistics.median(xs) if xs else None


FLOOR = float(os.getenv("MIN_BALANCE_USDC", "50"))


def wallet_free():
    """Free USDC right now (needs the CLOB credentials from .live.env); None when it cannot be read."""
    try:
        from btc5m_sim import live
        return live.wallet_balance(force=True)
    except Exception:
        return None


def equity(d, orders, res):
    """(free USDC, money in filled orders not yet settled) — judged on the wallet as it is now, not on old skip lines."""
    free = wallet_free()
    if free is None:
        return None
    done = {(r["rule"], r["slug"]) for r in res}
    settled = set()
    p = os.path.join(d, "live_results.csv")
    if os.path.exists(p):
        settled = {(r["variant"], r["slug"]) for r in csv.DictReader(open(p))}
    pending = sum(float(o.get("cost") or 0) for o in orders if o.get("mode") == "live" and o.get("status") == "filled"
                  and (o["variant"], o["slug"]) not in settled and (o["variant"], o["slug"]) not in done)
    return free, pending


def review_coin(coin, d, unit, now, act=True):
    out = dict(coin=coin, decision="continue", reason="", action="", window_n=0, window_losses=0, window_pnl=0.0, expected_loss_rate="", tail="")
    def do(kind, what):
        out["action"] = (out["action"] + "；" if out["action"] else "") + what
        if not act: return
        if kind == "stop": open(os.path.join(d, "STOP"), "a").close()
        elif kind == "pause":
            open(os.path.join(d, "PAUSE_UNTIL"), "w").write(str(int(now.timestamp() + PAUSE_H * 3600)))
        elif kind.startswith("disable:"):
            rule = kind.split(":", 1)[1]; p = os.path.join(d, "DISABLED_RULES")
            have = {l.strip() for l in open(p)} if os.path.exists(p) else set()
            if rule not in have: open(p, "a").write(rule + "\n")
        elif kind == "restart" and unit:
            subprocess.run(["systemctl", "restart", unit], timeout=60)
    if os.path.exists(os.path.join(d, "STOP")):
        out.update(decision="stopped", reason="已有停單檔（人工或先前緊急停機），不檢討"); return out
    orders, res = load(d)
    w0, b0 = now - timedelta(hours=WINDOW_H), now - timedelta(hours=WINDOW_H + BASE_H)
    if SINCE:
        w0 = max(w0, datetime.fromisoformat(SINCE).astimezone(TZ))
    win = [r for r in res if w0 < r["t"] <= now]; base = [r for r in res if b0 < r["t"] <= w0]
    n, l = len(win), sum(1 for r in win if not r["won"])
    out.update(window_n=n, window_losses=l, window_pnl=round(sum(r["pnl"] for r in win), 2), window_fee=round(sum(r["fee"] for r in win), 2))
    # 1) health: things with an obvious cause
    if unit and act and subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode != 0:
        out.update(decision="fix", reason="服務沒有在跑"); do("restart", f"重啟 {unit}"); return out
    book = os.path.join(d, "sim_book.csv")
    if os.path.exists(book) and time.time() - os.path.getmtime(book) > STALE_MIN * 60:
        out.update(decision="fix", reason=f"盤口資料超過 {STALE_MIN:g} 分鐘沒更新"); do("restart", f"重啟 {unit}"); return out
    recent = [o for o in orders if o.get("ts") and _ts(o["ts"]) > now - timedelta(hours=2)]
    eq = equity(d, orders, res)
    if eq is not None and eq[0] + eq[1] < FLOOR:
        out.update(decision="fix", reason=f"錢包可用 ${eq[0]:.2f}＋未結算 ${eq[1]:.2f}，低於停買下限 ${FLOOR:g}，機器人停買中",
                   action="需要你補資金（程式無法代勞）"); return out
    hard = [o for o in recent if o.get("status") in ("rejected", "error") and any(h in o.get("msg", "").lower() for h in HARD_ERRORS)]
    if len(hard) >= 3:
        out.update(decision="fix", reason=f"下單被拒 {len(hard)} 次（地區封鎖或簽名問題）"); do("pause", f"暫停 {PAUSE_H:g} 小時，請檢查代理與簽名")
        return out
    # 2) performance against expectation
    p = (sum(1 for r in base if not r["won"]) / len(base)) if len(base) >= MIN_BASE else P_DEFAULT
    tail = binom_tail(n, l, p)
    out.update(expected_loss_rate=round(p, 4), tail=round(tail, 4))
    if n < 5 or l <= 1 or tail >= TAIL:
        out["reason"] = f"近 {WINDOW_H:g} 小時 {n} 筆輸 {l}，預期輸率 {p:.1%}，在正常範圍"; return out
    # 3) clearly worse than expected: look for a known cause
    losses = [r for r in win if not r["won"]]
    by = {}
    for r in win: by.setdefault(r["rule"], []).append(r)
    for rule, rs in sorted(by.items(), key=lambda kv: -sum(1 for r in kv[1] if not r["won"])):
        lr = sum(1 for r in rs if not r["won"])
        bp_rs = [r for r in base if r["rule"] == rule]
        bp = (sum(1 for r in bp_rs if not r["won"]) / len(bp_rs)) if len(bp_rs) >= MIN_BASE else p
        if lr >= 2 and lr >= 0.6 * l and binom_tail(len(rs), lr, bp) < TAIL:
            out.update(decision="fix", reason=f"輸單集中在 {rule}（{len(rs)} 筆輸 {lr}）"); do(f"disable:{rule}", f"停用 {rule} 的真單（影子照收）")
            return out
    late = [r for r in losses if r["ask"] - r["fill"] >= LATE_GAP]
    base_late = sum(1 for r in base if r["ask"] - r["fill"] >= LATE_GAP) / max(len(base), 1)
    if len(late) >= 2 and len(late) / max(n, 1) >= max(2 * base_late, 0.05):
        out.update(decision="fix", reason=f"單子晚到：{len(late)} 筆輸單成交價比看到的便宜 {LATE_GAP:g} 以上（市場先翻了）")
        do("pause", f"暫停 {PAUSE_H:g} 小時"); return out
    sw_w, sw_b = swings(d, {r["slug"] for r in win}), swings(d, {r["slug"] for r in base})
    if sw_w and sw_b and sw_w >= VOL_X * sw_b:
        out.update(decision="fix", reason=f"行情波動異常（每場振幅 {sw_w:.0f} bps，平常 {sw_b:.0f}）"); do("pause", f"暫停 {PAUSE_H:g} 小時")
        return out
    out.update(decision="stop", reason=f"明顯偏離預期（{n} 筆輸 {l}，預期輸率 {p:.1%}，機率 {tail:.1%}），找不到已知原因")
    do("stop", "緊急停機（放停單檔），恢復請手動刪除 STOP")
    return out


def tg(text):
    tok = os.getenv("TG_TOKEN", "")
    for chat in [c.strip() for c in os.getenv("TG_CHAT_ID", "").replace(";", ",").split(",") if c.strip()]:
        try:
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                   data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()), timeout=10)
        except Exception as e:
            print("TG send failed", type(e).__name__)


ICON = {"continue": "✅", "fix": "🔧", "stop": "🛑", "stopped": "⏹"}
WORD = {"continue": "符合預期，繼續", "fix": "跟預期不同，找到原因", "stop": "明顯偏離預期", "stopped": "已停單"}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry" in argv; now = datetime.now(TZ)
    for coin, sub, unit in COINS:
        d = sub if os.path.isabs(sub) else os.path.join(BASE_DIR, sub)
        o = review_coin(coin, d, unit, now, act=not dry)
        msg = f"{ICON[o['decision']]} [{coin}] 自我檢討：{WORD[o['decision']]}。{o['reason']}" + (f"｜處理：{o['action']}" if o["action"] else "") \
              + f"｜近 {WINDOW_H:g} 小時真單 {o['window_n']} 筆輸 {o['window_losses']}，淨 {o['window_pnl']:+.2f}" \
              + (f"（扣手續費 {o['window_pnl'] - o['window_fee']:+.2f}）" if o.get("window_fee") else "")
        print(now.strftime("%m-%d %H:%M"), msg)
        if dry: continue
        p = os.path.join(d, "review_log.csv"); new = not os.path.exists(p)
        with open(p, "a", newline="") as f:
            w = csv.DictWriter(f, LFIELDS)
            if new: w.writeheader()
            w.writerow({k: (now.strftime("%Y-%m-%d %H:%M:%S") if k == "ts" else o.get(k, "")) for k in LFIELDS})
        if o["decision"] in ("fix", "stop") or (o["decision"] == "continue" and now.hour == DAILY_HOUR):
            tg(msg)


if __name__ == "__main__":
    main()
