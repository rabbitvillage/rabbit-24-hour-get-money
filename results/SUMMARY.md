# 回測結果（2026-09-19 ~ 10-02，49 城 × 14 天，唯讀）

指令：`python3 -m weather_bot.backtest --days 14 --out results/bt14` 然後 `python3 -m weather_bot.bt_report results/bt14`
檔案：bt14/days.csv（每城每天）、bt14/trades_checked.csv（每筆成交，含手續費、是否其實贏了）、bt14/report.json（按日/城/價位彙總）

## 結論
- 區間被 METAR 判死後、No 在 99¢ 以下的成交，扣 taker 費後淨利 **約 $5,700 / 14 天**（全部 49 城合計，是別人實際吃到的）。
- 其中 $3,256 集中在 9/20 一天；扣掉 9/20，平常約 **$190/天**。
- 大部分錢在 97–99¢（用了 $16.6 萬本金周轉才賺 $2,477，約 1.5%）；95¢ 以下的機會很少。
- **9/20 有 5 個亞洲城市（北京、廣州、馬尼拉、青島、新加坡）METAR 顯示的最高溫比實際結算高 1–2°C**，被判死的區間最後反而贏了。照策略買的人會虧 **$27,865**，把所有利潤吃光好幾倍。推測 NOAA 結算頁那幾小時缺資料（未證實）。
- 這是下限：看不到沒成交的掛單，也沒算競爭。

## 修正
- polymarket.py：單位原本因描述裡都有 "Fahrenheit" 字樣，把攝氏城市全當成華氏 → 改讀 "whole degrees Celsius/Fahrenheit"。這個錯也影響即時掃描器。
- 新增 bt_report.py：對照實際結算、扣手續費。

## 對策（尚未做）
- 判死不能只看 METAR，要直接讀結算頁（weather.gov timeseries）確認已顯示該數值。
