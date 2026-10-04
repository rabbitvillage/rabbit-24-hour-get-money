# weather-bot — Polymarket 天氣盤口掃描器（唯讀）

設計文件：https://claude.ai/code/artifact/3ec48743-e538-4d5f-8791-d6519d12ad54

只讀資料、不下真單。Python 3.10+，只用標準庫。

    python3 -m weather_bot.scan                          # 49 個城市模組掃一次
    python3 -m weather_bot.scan --loop 30                # 每 30 秒
    python3 -m weather_bot.scan --city tokyo --city taipei
    python3 -m weather_bot.scan --bankroll 2000 --paper  # 模擬下單，部位記在 ledger.json
    python3 -m unittest discover -s tests                # 離線測試

- weather_bot/cities.py       49 個城市模組（測站、時區、單位、規則特例），一城一列
- weather_bot/allocator.py    資金調度模組：上限、排序、依掛單深度定量、模擬帳本
- weather_bot/detector.py     判定死區間、試算扣費後利潤
- weather_bot/observations.py METAR 觀測，換算成結算來源會顯示的值
- weather_bot/polymarket.py   Gamma 盤口、規則解析、CLOB 訂單簿
- weather_bot/scan.py         主程式：平行跑所有城市模組 → 資金調度 → 輸出計畫

## 回測（需要網路）

    python3 -m weather_bot.backtest --days 14              # 49 城 × 14 天
    python3 -m weather_bot.backtest --days 14 --city tokyo

對照 Polymarket 歷史成交（data-api）與歷史 METAR（Iowa State Mesonet），
算出每個城市每天「區間被判死之後，還有多少錢在 99¢ 以下成交」、多久之後、被多少個錢包吃掉。
結果在 backtest_results/{days.csv,trades.csv,summary.json}。

需要放行的網域：gamma-api.polymarket.com, clob.polymarket.com, data-api.polymarket.com,
aviationweather.gov, mesonet.agron.iastate.edu
