const $ = id => document.getElementById(id);
chrome.storage.sync.get({ addr: "", amount: 5, l1: 0.40 }, v => { $("addr").value = v.addr; $("amount").value = v.amount; $("l1").value = v.l1; });
$("save").onclick = () => {
  const addr = $("addr").value.trim();
  if (addr && !/^0x[0-9a-fA-F]{40}$/.test(addr)) { $("ok").textContent = "地址格式不對"; return; }
  const amount = Math.max(1, Math.round(Number($("amount").value) || 5));
  const l1 = Math.min(0.49, Math.max(0.10, Number($("l1").value) || 0.40));
  chrome.storage.sync.set({ addr, amount, l1 }, () => { $("ok").textContent = "已儲存"; setTimeout(() => $("ok").textContent = "", 1500); });
};
