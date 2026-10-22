"""Live commentary feed for the dashboard: what is happening in the current 5-minute market, right now.

sim.py calls update() on every order-book poll and finish()/resolved() at market end; a background thread POSTs the
state to the dashboard (DASH_URL's /api/now) every FEED_EVERY seconds when something changed, else a heartbeat
every FEED_HEARTBEAT seconds. Plain Traditional-Chinese sentences; the page shows them as text only.
"""
import json, os, re, threading, time, urllib.request
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "8"))))
DASH_URL = os.getenv("DASH_URL", "")                       # .../api/push ; we derive .../api/now
DASH_TOKEN = os.getenv("DASH_TOKEN", "")
FEED_EVERY = float(os.getenv("FEED_EVERY", "5"))
FEED_HEARTBEAT = float(os.getenv("FEED_HEARTBEAT", "30"))
MAX_EVENTS = 8

_lock = threading.Lock()
_state = {"market": None, "last": None, "events": [], "updated": ""}
_rule_text = {}      # rule -> last state text (to detect changes)
_dirty = False
_log = print


def _now():
    return datetime.now(TZ).strftime("%H:%M:%S")


def init(log=None):
    global _log
    if log: _log = log
    if DASH_URL and DASH_TOKEN:
        threading.Thread(target=_pusher, daemon=True).start()
        _log(f"feed: pushing live commentary every {FEED_EVERY:g}s to {DASH_URL.rsplit('/', 1)[0]}/now")
    else:
        _log("feed: DASH_URL/DASH_TOKEN not set, live commentary disabled")


def event(text):
    with _lock:
        _state["events"].insert(0, f"{_now()} {text}")
        del _state["events"][MAX_EVENTS:]
        _mark()


def _mark():
    global _dirty
    _state["updated"] = _now(); _dirty = True


def start_market(slug, start_ts):
    """New market being watched: reset the live block (the previous market's summary stays in `last`)."""
    with _lock:
        _rule_text.clear()
        t0 = datetime.fromtimestamp(start_ts, TZ).strftime("%H:%M"); t1 = datetime.fromtimestamp(start_ts + 300, TZ).strftime("%H:%M")
        _state["market"] = dict(slug=slug, window=f"{t0}–{t1}", secs_left=None, book="", line="", rules=[], phase="等開盤後資料")
        _state["events"] = []
        _mark()


def update(slug, secs_left, asks, outcomes, dl, line_src, rules):
    """Called on every poll. `rules` = list of (name, state_text, is_live). Only changes produce events."""
    with _lock:
        m = _state["market"]
        if not m or m["slug"] != slug: return
        m["secs_left"] = round(secs_left)
        up, down = asks[0], asks[1]
        def fmt(a): return "—" if a[0] is None else f"{a[0]:.3f}（${a[2]:,.0f}）"
        lead = "Up" if (up[0] or 0) >= (down[0] or 0) else "Down"
        m["book"] = f"Up 賣價 {fmt(up)}｜Down 賣價 {fmt(down)}｜領先 {lead}"
        m["line"] = "幣價：還沒有現價" if dl is None else f"幣價離開盤線 {dl:+.1f} 美元（{'偏 Up' if dl > 0 else '偏 Down'}，線用{line_src}）"
        m["phase"] = f"剩 {round(secs_left)} 秒" + ("，進場窗口內" if secs_left <= 60 else "，還沒到進場窗口（最後 60 秒）")
        m["rules"] = [dict(name=n, text=t, live=l) for n, t, l in rules]
        for n, t, l in rules:
            key = re.sub(r"[+\-]?\d+(\.\d+)?", "#", t)      # the kind of state, numbers removed: a price or distance wobble is not an event
            if _rule_text.get(n) != key:
                if n in _rule_text and l:           # only live rules narrate changes (shadow rules would be noise)
                    _state["events"].insert(0, f"{_now()} 剩 {round(secs_left)} 秒 {n}：{t}")
                    del _state["events"][MAX_EVENTS:]
                _rule_text[n] = key
        _mark()


def finish(slug, summary_lines):
    """Market closed (before resolution): freeze a summary so the box explains why nothing (or something) happened."""
    with _lock:
        m = _state["market"]
        if m and m["slug"] == slug:
            m["phase"] = "已收盤，等結算（約 5 分鐘）"
        _state["last"] = dict(slug=slug, window=m["window"] if m else "", result="結算中…", lines=summary_lines)
        _mark()


def resolved(slug, winner, lines):
    with _lock:
        last = _state["last"]
        if last and last["slug"] == slug:
            last["result"] = f"結果 {winner}"; last["lines"] = lines
        _mark()


def snapshot():
    with _lock:
        return json.loads(json.dumps(_state))


def _pusher():
    global _dirty
    url = DASH_URL.rsplit("/", 1)[0] + "/now"
    last_push = 0.0
    while True:
        time.sleep(1)
        now = time.time()
        with _lock:
            due = (_dirty and now - last_push >= FEED_EVERY) or (now - last_push >= FEED_HEARTBEAT)
            if not due: continue
            body = json.dumps({**_state, "pushed": _now()}, ensure_ascii=False).encode()
            _dirty = False
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"content-type": "application/json", "authorization": f"Bearer {DASH_TOKEN}", "user-agent": "btc5m-feed"})
            with urllib.request.urlopen(req, timeout=8) as r: r.read()
            last_push = now
        except Exception as e:
            _log(f"feed: push failed {type(e).__name__}"); last_push = now
