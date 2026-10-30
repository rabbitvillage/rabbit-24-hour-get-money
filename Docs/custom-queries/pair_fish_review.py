"""fish 練習場次逐場還原（2026-10-30）：讀插件匯出的成交（JSON：[{slug, legs:[{side, price, size, s}]}]），
對照記錄器盤口，看第一張買下前後發生什麼事。
  進場前：買的那邊開盤價、前 20 秒最高、開盤以來最低、最後 5 秒的變化（還在跌還是已回升）、幣價最後 10 秒的走勢。
  進場後：買的那邊之後最低到哪、對面之後最低到哪（最多能賺多少）、第一次能配成（相加 < 1）的時間。
唯讀 /opt/pm-pair/data。用法：cd /opt/pm-pair/data && python3 -I ../pair_fish_review.py fish_trades.json
"""
import csv, glob, gzip, json, sys
fee = lambda p: 0.07 * p * (1 - p)
G = json.load(open(sys.argv[1])); want = {g["slug"] for g in G}
rows = {s: [] for s in want}; cl = {}
for p in sorted(glob.glob("book_*_*.csv") + glob.glob("book_*_*.csv.gz")):
    for r in csv.DictReader(gzip.open(p, "rt") if p.endswith(".gz") else open(p)):
        if r["slug"] in want:
            g = lambda k: float(r[k]) if r[k] else None
            rows[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("dn_ask"), g("cl_price")))
for g in G:
    sl = g["slug"]; R = sorted(x for x in rows[sl] if x[1] is not None and x[2] is not None)
    if not R: print(sl, "沒有盤口資料"); continue
    l1 = g["legs"][0]; side, p1, t1 = l1["side"], l1["price"], l1["s"]
    own = lambda x: x[1] if side == "Up" else x[2]; oth = lambda x: x[2] if side == "Up" else x[1]
    at = lambda s: min(R, key=lambda x: abs(x[0] - s))
    pre = [x for x in R if 0 <= x[0] <= t1]; post = [x for x in R if x[0] > t1]
    hi20 = max((own(x) for x in R if t1 - 20 <= x[0] <= t1), default=None)
    lo0 = min((own(x) for x in pre), default=None)
    d5 = own(at(t1)) - own(at(t1 - 5)); d10cl = (at(t1)[3] or 0) - (at(t1 - 10)[3] or 0)
    olo = min(post, key=oth) if post else None; slo = min(post, key=own) if post else None
    first = next((x for x in post if p1 + oth(x) < 1), None)
    paired = len(g["legs"]) > 1
    print(f"=== {sl}  買 {side} {p1:.3f} 在第 {t1:.0f} 秒  {'成對' if paired else '單邊'}" + (f"（第二張 {g['legs'][1]['price']:.3f} 在第 {g['legs'][1]['s']:.0f} 秒）" if paired else ""))
    print(f"  進場前：{side} 開盤 {own(R[0]):.2f}，前 20 秒最高 {hi20:.2f}（跌了 {hi20 - p1:+.2f}），開盤以來最低 {lo0:.2f}；最後 5 秒 {d5:+.2f}（{'還在跌' if d5 < -0.005 else '已回升' if d5 > 0.005 else '持平'}）；幣價最後 10 秒 {d10cl:+.1f} 美元")
    print(f"  進場後：{side} 最低 {own(slo):.2f}（第 {slo[0]:.0f} 秒）；對面最低 {oth(olo):.2f}（第 {olo[0]:.0f} 秒），最多每股淨賺 {(1 - p1 - oth(olo) - fee(p1) - fee(oth(olo))) * 100:+.1f} 分；"
          + (f"第一次相加 < 1 在第 {first[0]:.0f} 秒（{side} 買後 {first[0] - t1:.0f} 秒）" if first else "整場都沒有相加 < 1"))
    path = " ".join(f"{x[0]:.0f}:{x[1]:.2f}/{x[2]:.2f}" for x in R if x[0] <= t1 + 30 and abs(x[0] - round(x[0] / 3) * 3) < 0.13)
    print(f"  走勢（秒:Up/Down，到買後 30 秒）：{path}")
