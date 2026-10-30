const $ = id => document.getElementById(id);
const D = { addr: "", amount: 5, last: PairLogic.DEFAULTS.LAST };
chrome.storage.sync.get(D, v => { for (const k in D) $(k).value = v[k]; });
$("save").onclick = () => {
  const addr = $("addr").value.trim();
  if (addr && !/^0x[0-9a-fA-F]{40}$/.test(addr)) { $("ok").textContent = "地址格式不對"; return; }
  const amount = Math.max(1, Math.round(Number($("amount").value) || 5));
  const last = Math.min(290, Math.max(10, Math.round(Number($("last").value) || D.last)));
  chrome.storage.sync.set({ addr, amount, last }, () => { $("ok").textContent = "已儲存"; setTimeout(() => $("ok").textContent = "", 1500); });
};
chrome.storage.local.get({ journal: {} }, v => { $("cnt").textContent = `目前 ${Object.keys(v.journal).length} 場`; });
$("export").onclick = () => chrome.storage.local.get({ journal: {} }, v => {
  const rows = Object.values(v.journal).sort((a, b) => String(a.slug).localeCompare(String(b.slug)));
  const url = URL.createObjectURL(new Blob(["﻿" + PairLogic.toCSV(rows)], { type: "text/csv" }));
  const a = document.createElement("a"); a.href = url; a.download = `pair-journal-${new Date().toISOString().slice(0, 10)}.csv`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
});
