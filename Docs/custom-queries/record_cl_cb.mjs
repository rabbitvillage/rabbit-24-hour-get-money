// Record Chainlink (Polymarket RTDS, public) and Coinbase ticker side by side. Read-only. Usage: node recorder.mjs <minutes> <outfile>
import fs from 'fs';
const mins = +process.argv[2] || 40, out = fs.createWriteStream(process.argv[3]);
const w = (o) => out.write(JSON.stringify(o) + '\n');
function rtds() {
  const ws = new WebSocket('wss://ws-live-data.polymarket.com'); let ping;
  ws.onopen = () => { ws.send(JSON.stringify({action:'subscribe', subscriptions:[
      {topic:'crypto_prices_chainlink', type:'*', filters:'{"symbol":"btc/usd"}'},
      {topic:'crypto_prices_chainlink', type:'*', filters:'{"symbol":"sol/usd"}'}]}));
    ping = setInterval(()=>{ try{ws.send('PING')}catch{} }, 5000); };
  ws.onmessage = (e) => { const r = Date.now(); const s = String(e.data); if (!s || s[0] !== '{') return;
    try { const m = JSON.parse(s); const p = m.payload || {};
      if (p.data) for (const d of p.data) w({src:'cl', sym:p.symbol, ts:d.timestamp, v:d.value, snap:1, recv:r});
      else if (p.value !== undefined) w({src:'cl', sym:p.symbol, ts:p.timestamp, v:p.value, srv:m.timestamp, recv:r}); } catch {} };
  ws.onclose = () => { clearInterval(ping); setTimeout(rtds, 1000); };
  ws.onerror = () => {};
}
function coinbase() {
  const ws = new WebSocket('wss://ws-feed.exchange.coinbase.com');
  ws.onopen = () => ws.send(JSON.stringify({type:'subscribe', product_ids:['BTC-USD','SOL-USD'], channels:['ticker']}));
  ws.onmessage = (e) => { const r = Date.now(); try { const m = JSON.parse(String(e.data)); if (m.type !== 'ticker') return;
    w({src:'cb', sym:m.product_id, ts:Date.parse(m.time), bid:+m.best_bid, ask:+m.best_ask, px:+m.price, recv:r}); } catch {} };
  ws.onclose = () => setTimeout(coinbase, 1000); ws.onerror = () => {};
}
rtds(); coinbase();
setTimeout(() => { out.end(); process.exit(0); }, mins * 60000);
