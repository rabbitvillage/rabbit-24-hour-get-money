// Static plain-language report (frozen numbers as of 2026-10-21 15:20 Taiwan). No user input, no JS.
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Horizontal bar chart. items: [{label, value, color?}] ; fmt: value formatter
function hbar(items, { min, max, fmt, zero = true }) {
  const W = 640, rowH = 34, L = 150, R = 70, T = 8;
  const H = T + items.length * rowH + 8;
  const x = (v) => L + ((v - min) / (max - min)) * (W - L - R);
  const z = x(0);
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img">
${zero && min < 0 ? `<line x1="${z.toFixed(1)}" x2="${z.toFixed(1)}" y1="${T}" y2="${H - 8}" class="axis"/>` : ""}
${items.map((it, i) => {
    const y = T + i * rowH; const x0 = Math.min(z, x(it.value)), x1 = Math.max(z, x(it.value));
    return `<text x="${L - 8}" y="${y + 22}" class="lbl" text-anchor="end">${esc(it.label)}</text>
<rect x="${x0.toFixed(1)}" y="${y + 6}" width="${Math.max(2, x1 - x0).toFixed(1)}" height="22" rx="4" fill="${it.color || "var(--acc)"}"/>
<text x="${(it.value >= 0 ? x1 + 6 : x0 - 6).toFixed(1)}" y="${y + 22}" class="val" text-anchor="${it.value >= 0 ? "start" : "end"}">${esc(fmt(it.value))}</text>`;
  }).join("\n")}
</svg>`;
}

// The 2026-10-20 14:05 market: BTC spot minus the line, seconds left -> USD (sampled from sim_book.csv)
const FLIP = [[140, -55.0], [130, -34.6], [120, -34.6], [110, -34.6], [100, -35.7], [90, -30.1], [80, -38.6], [70, -11.1], [60, -12.8], [55, -4.7], [50, -14.1], [45, -4.8], [40, -4.7], [37, -7.8], [34, -11.5], [32, -8.1], [31, 6.9], [30, 15.3], [29, 22.0], [25, 23.0], [20, 22.7], [15, 20.5], [10, 15.3], [6, 15.3]];
function flipChart() {
  const W = 640, H = 260, L = 50, R = 16, T = 40, B = 34, yMin = -60, yMax = 32;
  const xs = (s) => L + ((150 - s) / 150) * (W - L - R);
  const ys = (v) => T + (1 - (v - yMin) / (yMax - yMin)) * (H - T - B);
  const pts = FLIP.map(([s, v]) => `${xs(s).toFixed(1)},${ys(v).toFixed(1)}`).join(" ");
  const grid = [-60, -40, -20, 0, 20].map((v) => `<line x1="${L}" x2="${W - R}" y1="${ys(v).toFixed(1)}" y2="${ys(v).toFixed(1)}" class="${v === 0 ? "zero" : "grid"}"/><text x="${L - 6}" y="${(ys(v) + 4).toFixed(1)}" class="lbl" text-anchor="end">${v > 0 ? "+" : ""}${v}</text>`).join("");
  const xl = [150, 120, 90, 60, 45, 30, 15, 0].map((s) => `<text x="${xs(s).toFixed(1)}" y="${H - 10}" class="lbl" text-anchor="middle">${s}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img">${grid}${xl}
<text x="${W - R}" y="${(ys(0) - 6).toFixed(1)}" class="lbl" text-anchor="end">開盤價（基準線）</text>
<rect x="${xs(60).toFixed(1)}" y="${T}" width="${(xs(0) - xs(60)).toFixed(1)}" height="${H - T - B}" fill="var(--warn)" opacity="0.08"/>
<text x="${xs(30).toFixed(1)}" y="${(H - B - 8).toFixed(1)}" class="lbl" text-anchor="middle">最後 60 秒＝結算只看這段的平均</text>
<polyline fill="none" stroke="var(--acc)" stroke-width="2.5" points="${pts}"/>
<circle cx="${xs(37).toFixed(1)}" cy="${ys(-7.8).toFixed(1)}" r="6" fill="var(--bad)"/>
<text x="${(xs(37) - 12).toFixed(1)}" y="${(ys(-7.8) + 26).toFixed(1)}" class="note" text-anchor="end">剩 36 秒：離線只有 8 美元，市場卻說 Down 99% 穩 →</text>
<circle cx="${xs(29).toFixed(1)}" cy="${ys(22).toFixed(1)}" r="6" fill="var(--good)"/>
<text x="${(xs(29) - 12).toFixed(1)}" y="${(ys(22) - 14).toFixed(1)}" class="note" text-anchor="end">5 秒後跳到線上方 22 美元，翻成 Up →</text>
<text x="${((L + W - R) / 2).toFixed(1)}" y="${H - 24}" class="lbl" text-anchor="middle">剩餘秒數 →</text>
</svg>`;
}

export function reportHtml() {
  const money = (v) => (v >= 0 ? "+" : "−") + "$" + Math.abs(v).toFixed(1);
  const profit = hbar([
    { label: "原本的規則", value: -0.4, color: "var(--bad)" },
    { label: "方案 A 穩的", value: 4.8, color: "var(--good)" },
    { label: "方案 B 高收益", value: 9.4, color: "var(--acc)" },
    { label: "方案 C 兩條一起", value: 14.2, color: "var(--acc)" },
  ], { min: -3, max: 16, fmt: money });
  const losses = hbar([
    { label: "原本的規則", value: 1.0, color: "var(--bad)" },
    { label: "方案 A 穩的", value: 0, color: "var(--good)" },
    { label: "方案 B 高收益", value: 4, color: "var(--warn)" },
  ], { min: 0, max: 5, fmt: (v) => v === 0 ? "0 筆（541 場沒輸過）" : `約 ${v} 筆` });
  const stake = hbar([
    { label: "方案 A 穩的", value: 105, color: "var(--good)" },
    { label: "方案 B 高收益", value: 55, color: "var(--acc)" },
    { label: "方案 C 兩條一起", value: 110, color: "var(--acc)" },
  ], { min: 0, max: 130, fmt: (v) => `每筆約 $${v}` });

  return `<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>BTC 5 分鐘盤：一天跑下來的結論</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262a33;--fg:#e6e8ee;--mut:#8b93a7;--good:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#58a6ff}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6eb;--fg:#1f2328;--mut:#6b7280;--acc:#0969da}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans TC",sans-serif}
main{max-width:860px;margin:0 auto}h1{font-size:24px;line-height:1.3;margin:0 0 6px}h2{font-size:19px;margin:34px 0 10px;padding-top:18px;border-top:1px solid var(--line)}
.sub{color:var(--mut);font-size:14px;margin-bottom:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:12px 0}
.big{font-size:20px;font-weight:600}.chart{width:100%;height:auto;display:block;margin:6px 0 2px}.lbl{fill:var(--mut);font-size:12px}.val{fill:var(--fg);font-size:13px;font-weight:600}
.note{fill:var(--fg);font-size:12px}.grid{stroke:var(--line);stroke-width:1}.zero{stroke:var(--mut);stroke-width:1.5;stroke-dasharray:4 3}.axis{stroke:var(--mut);stroke-width:1}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}thead th{color:var(--mut);font-weight:500;font-size:13px;white-space:nowrap}tbody tr:last-child td{border-bottom:0}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.pick{outline:2px solid var(--acc);outline-offset:-2px}
ul{padding-left:22px;margin:8px 0}li{margin:4px 0}.foot{color:var(--mut);font-size:13px;margin-top:30px;padding-top:14px;border-top:1px solid var(--line)}a{color:var(--acc)}
.steps{counter-reset:s}.steps div{position:relative;padding-left:40px;margin:10px 0}.steps div::before{counter-increment:s;content:counter(s);position:absolute;left:0;top:2px;width:28px;height:28px;border-radius:50%;background:var(--acc);color:#fff;text-align:center;line-height:28px;font-weight:700}
</style></head><body><main>
<h1>比特幣 5 分鐘盤：模擬跑了一天，結論是什麼？</h1>
<div class="sub">資料截至 2026-10-21 15:20（台灣時間）。全部是模擬，沒有下過任何真單。即時數據在 <a href="/">看板</a>。</div>

<div class="card"><div class="big">一句話：原本的規則會賠錢，加一個「看幣價站在哪邊」的檢查之後就不會輸；想賺更多要提早進場，但會偶爾輸。</div></div>

<h2>這個遊戲在玩什麼</h2>
<div class="card">
每 5 分鐘一場，猜比特幣「最後 60 秒的平均價」比開盤價高（Up）還是低（Down）。快收盤時，幾乎確定的那一邊會賣到 98～99¢，買對了拿 $1。
我們的做法是在最後幾十秒買那個「幾乎確定」的一邊，一筆賺 1～2 分錢，靠一天幾十筆累積。
</div>

<h2>為什麼會輸：翻盤長這樣</h2>
<div class="card">
${flipChart()}
<p>這是真實輸掉的一場。幣價整場黏在開盤價附近，剩 36 秒時只差 8 美元，市場卻把 Down 賣到 99¢。我們照買，5 秒後幣價跳到線上方，結果 Up。
<b>輸的原因不是慢，是買了一個「市場說 99%、物理上只有六七成」的場。</b> 三次輸全部是這種「貼線」場。</p>
</div>

<h2>解法：買之前看一眼幣價站在哪邊、離線多遠</h2>
<div class="card">
<ul>
<li><b>方案 A（穩的）</b>：照舊在最後 45 秒買 98～99¢，但幣價必須站在買的那一邊。跑了 541 場，<span class="good">0 輸</span>。</li>
<li><b>方案 B（高收益）</b>：提早到最後 60 秒，領先方 95¢ 以下就買（常常買到 90¢，一贏賺 10¢，是 A 的五倍），但要離開盤價 20 美元以上。一天約 50 筆，<span class="warn">輸 4 筆</span>。</li>
<li><b>方案 C</b>：A 和 B 同時跑，各自下單。</li>
</ul>
</div>

<h2>一天能賺多少（每筆 5 股，約 $4.5）</h2>
<div class="card">${profit}<p class="sub" style="margin:4px 0 0">用模擬一天 288 場換算。原本的規則三筆輸把利潤吃光，所以是負的。</p></div>

<h2>一天會輸幾筆</h2>
<div class="card">${losses}<p class="sub" style="margin:4px 0 0">輸一筆＝那一筆的本金全賠。方案 A 贏一筆賺 1.5%，所以一筆輸要 70 筆贏才補得回來；方案 B 贏一筆賺約 9%，一筆輸約 11 筆贏補回。</p></div>

<h2>要賺到 $100／天，每筆要下多少</h2>
<div class="card">${stake}<p class="sub" style="margin:4px 0 0">方案 C 是 A 每筆 $70 加 B 每筆 $40。總本金建議比每筆多留 3～5 倍當輸的緩衝：A 約 $300、B 約 $500、C 約 $600。</p></div>

<h2>三個方案一次比</h2>
<div class="table-wrap"><table><thead><tr><th></th><th>方案 A 穩的</th><th>方案 B 高收益</th><th class="pick">方案 C 兩條一起（建議）</th></tr></thead><tbody>
<tr><td>一天進場</td><td>約 64 筆</td><td>約 49 筆</td><td class="pick">約 113 筆</td></tr>
<tr><td>一天輸</td><td class="good">0 筆</td><td class="warn">約 4 筆</td><td class="pick">約 4 筆</td></tr>
<tr><td>每筆 5 股一天賺</td><td>+$4.8</td><td>+$9.4</td><td class="pick">+$14.2</td></tr>
<tr><td>賺 $100／天每筆要下</td><td>約 $105</td><td>約 $55</td><td class="pick">$70 ＋ $40</td></tr>
<tr><td>建議總本金</td><td>約 $300</td><td>約 $500</td><td class="pick">約 $600</td></tr>
<tr><td>每天起伏</td><td>很小</td><td>大，壞的一天可能是負的</td><td class="pick">中</td></tr>
<tr><td>盤口吃得下嗎</td><td>可以（深度約 $3,000）</td><td>很輕鬆（深度約 $9,000）</td><td class="pick">都可以</td></tr>
<tr><td>最大風險</td><td>貼線翻盤（目前 0 次）</td><td>基準線量不準、多輸幾筆</td><td class="pick">同左</td></tr>
</tbody></table></div>

<h2>接下來怎麼走</h2>
<div class="card steps">
<div><b>小額實盤</b>：A、B 各 5 股（一筆不到 $5）真的下單一天，看真實成交率和真實輸的比例。這一關不是賺錢，是確認「買得到」。</div>
<div><b>放大</b>：一天後數字出來，決定錢往 A 還是 B 放，每筆逐步加到 $50～100。</div>
<div><b>拚速度</b>：只有在發現常常買到 99¢ 而不是 98¢、或常常搶不到時，才值得花錢上即時連線。</div>
</div>

<h2>幾個誠實的保留</h2>
<div class="card"><ul>
<li>模擬假設「看到賣單就買得到」，真實下單會慢 0.3～0.5 秒，成交筆數和利潤會打折，折多少要靠小額實盤量。</li>
<li>方案 A 的 0 輸是 541 場的結果，不是永遠不會輸；貼線翻盤仍可能發生，只是檢查會把大多數擋掉。</li>
<li>方案 B 的「離線 20 美元」是用免費的鏈上價量的，一成的場次誤差超過 23 美元，這是它多輸幾筆的主因。</li>
</ul></div>

<p class="foot">只讀模擬，不下單。程式與資料：racknerd 主機、GitHub rabbitvillage/rabbit-24-hour-get-money。每小時摘要會送到 Telegram。</p>
</main></body></html>`;
}
