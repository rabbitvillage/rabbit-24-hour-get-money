# FR-20261026-io-monitor：IO『獨立作業』影子監測（最後 60 秒出現 0.97 就買）

狀態：active（跑滿 24 小時後看報表才算 verified）

1. **改了什麼**：新增獨立程序 `btc5m_sim/io_monitor.py` 和 systemd 的 `btc5m-io.service`／`btc5m-io.timer`（每 5 分鐘跑一次）。規則：每一場最後 60 秒內，只要領先方的最佳賣價是 0.97（±0.005，0.001 tick 的盤也算）而且有掛單，就當作用那個價買進，一場只買一次，結算後記帳。每筆 10 股，只扣交易所手續費（7%×p×(1−p)），不算滑價——fish 10-26 裁示「只看勝率」。輸出在 `btc5m_sim/io/<asset>/`：`io_trades.csv`（每筆影子單）、`io_state.json`（讀到 sim_book.csv 的哪裡、還沒結算的場）、`io_report.txt`（每次重寫）。預設看 data、data-eth、data-sol 三個幣。
2. **為什麼**：fish 想看「專吃 0.97」一天下來的勝率。驗收週不能動真單程式，所以做成完全獨立的程序：不 import 機器人、不讀 .env、不寫 data*/。
3. **修改前**：沒有這個監測；0.97 只能事後用 `Docs/custom-queries/tail98.py` 回顧。
4. **修改後**：主機 timer 每 5 分鐘增量讀 `sim_book.csv` 新增的列（sim 在每場結算後才把最後 150 秒的盤口一次寫入）和 `sim_trades.csv` 的贏家，持續記帳並重寫報表。隨時看：`cat btc5m_sim/io/btc/io_report.txt` 或 `python3 -m btc5m_sim.io_monitor --report`。帳本從安裝時刻起算；要含歷史得刪掉 `btc5m_sim/io/` 再用 `--backfill` 跑一次。
5. **影響檔案**：新增 `btc5m_sim/io_monitor.py`、`btc5m_sim/btc5m-io.service`、`btc5m_sim/btc5m-io.timer`；主機 `/etc/systemd/system/` 同名兩檔、主機新資料夾 `btc5m_sim/io/`。既有程式、`.env`、既有服務都沒改、沒重啟。
6. **交易／下單／風控／資料判斷**：無。不下單、不改任何既有檔案。
7. **驗證**：主機用 9 天歷史 `--backfill` 試跑（輸出到 /tmp，已清掉）：BTC 668 場觸發、翻船 15（2.25%，損益兩平 2.80%）、ETH 333／12（3.60%）、SOL 264／8（3.03%），和 tail98.py 一致；第二次執行增量讀取數字不變。待驗收：10-27 21:35 之後看一天報表。移除：`systemctl disable --now btc5m-io.timer`。
