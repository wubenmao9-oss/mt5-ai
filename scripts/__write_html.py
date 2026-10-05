import pathlib
html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MT5-Ai Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
*{font-family:'Inter',system-ui,sans-serif}
</style>
</head>
<body class="bg-gray-950 text-gray-300 min-h-screen">
<div id="app"></div>
<script>
const API = "http://localhost:8000";
const TARGET = 10.0;
let chart = null, candleSeries = null, markerSeries = null;

async function get(p){const r=await fetch(API+p);return(await r.json()).data||(await r.json())}
function fmt(v){return (v??0).toFixed(2)}
function pnlColor(v){return v>0?"text-emerald-400":v<0?"text-rose-400":"text-gray-400"}
function stateLabel(s){
const m={RUNNING:"bg-emerald-500 Running",SLEEP:"bg-amber-500 Sleeping",KILLED:"bg-rose-500 Killed",ERROR:"bg-red-600 Error",INIT:"bg-blue-500 Init"}
return m[s]||"bg-gray-500 Unknown"
}

async function render(){
const [st,pos]=await Promise.all([get("/api/status"),get("/api/positions")]);
const acc=st.account||{};const s=st.stats||{};const ei=st.engine_info||{};
const dp=st.daily_pnl||0;const pp=Math.min(100,Math.max(0,dp/TARGET*100));

document.getElementById("app").innerHTML=\`
<div class="max-w-7xl mx-auto p-6">
<!-- Header -->
<div class="flex items-center justify-between mb-8 pb-4 border-b border-gray-800">
<div class="flex items-center gap-4">
<div class="bg-gradient-to-br from-emerald-500 to-emerald-300 p-2.5 rounded-xl">
<svg class="w-6 h-6 text-gray-950" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
</div>
<div><h1 class="text-xl font-bold text-gray-100 tracking-wide">MT5-Ai Smart Engine</h1>
<div class="text-xs text-gray-500 mt-0.5">v2.0 B2 Strategy</div></div>
<div class="h-8 w-px bg-gray-800 mx-2"></div>
<div class="flex items-center gap-2 bg-gray-800/50 px-3 py-1.5 rounded-full border border-gray-700">
<span class="w-2.5 h-2.5 rounded-full \${
stateLabel(st.engine_state).split(" ")[0]
} animate-pulse"></span>
<span class="text-sm font-medium">\${stateLabel(st.engine_state).split(" ").slice(1).join(" ")}</span>
</div>
</div>
<button onclick="killswitch()" class="flex items-center gap-2 bg-rose-600 hover:bg-rose-500 px-4 py-2 rounded-lg font-medium transition shadow-lg shadow-rose-900/20">Killswitch</button>
</div>

<div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
<!-- Left sidebar -->
<div class="lg:col-span-3 space-y-6">
<!-- Account card -->
<div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
<h2 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-4">Account</h2>
<div class="space-y-4">
<div><div class="text-xs text-gray-500 mb-1">Equity</div>
<div class="text-3xl font-bold text-gray-100">\${fmt(acc.equity)}</div></div>
<div class="grid grid-cols-2 gap-4 pt-4 border-t border-gray-800/50">
<div><div class="text-xs text-gray-500 mb-1">Balance</div>
<div class="text-lg font-mono text-gray-300">\${fmt(acc.balance)}</div></div>
<div><div class="text-xs text-gray-500 mb-1">Free Margin</div>
<div class="text-lg font-mono text-gray-300">\${fmt(acc.margin_free)}</div></div>
</div></div></div>

<!-- Daily target card -->
<div class="bg-gray-900 rounded-xl border border-gray-800 p-5 relative overflow-hidden">
<h2 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-4">Daily Progress</h2>
<div class="flex justify-between items-end mb-2">
<div><div class="text-xs text-gray-500 mb-1">Realized PnL</div>
<div class="text-2xl font-bold \${pnlColor(dp)}">\${dp>0?"+":""}\${fmt(dp)} USD</div></div>
<div class="text-right"><div class="text-xs text-gray-500 mb-1">Target</div>
<div class="text-sm font-mono text-gray-300">\${TARGET.toFixed(2)} USD</div></div>
</div>
<div class="h-3 w-full bg-gray-800 rounded-full overflow-hidden shadow-inner">
<div class="h-full bg-gradient-to-r from-emerald-500 to-emerald-300 transition-all duration-1000 rounded-full" style="width:\${pp}%"></div>
</div>
<div class="mt-3 flex justify-between text-xs">
<span class="text-gray-500">Trades: <span class="text-gray-300">\${st.daily_trades||0}</span></span>
<span class="text-gray-500">Drawdown: <span class="text-rose-400">\${fmt(st.daily_max_drawdown)}</span></span>
</div></div>

<!-- Macro card -->
<div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
<h2 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-4">Strategy</h2>
<div class="space-y-4">
<div class="flex justify-between items-center">
<span class="text-sm text-gray-400">H1 Macro</span>
\${st.macro_bias==="BULLISH"?'<span class="text-emerald-400 bg-emerald-400/10 px-2 py-1 rounded text-sm flex items-center gap-1">Bullish</span>':
st.macro_bias==="BEARISH"?'<span class="text-rose-400 bg-rose-400/10 px-2 py-1 rounded text-sm flex items-center gap-1">Bearish</span>':
'<span class="text-gray-400 bg-gray-800 px-2 py-1 rounded text-sm">Neutral</span>'}
</div>
<div class="flex justify-between items-center pt-3 border-t border-gray-800/50">
<span class="text-sm text-gray-400">Mode</span>
<span class="text-sm font-medium text-amber-200 bg-amber-900/30 px-2 py-1 rounded border border-amber-800/50">\${ei.state||"INIT"}</span>
</div>
<div class="flex justify-between items-center pt-3 border-t border-gray-800/50">
<span class="text-sm text-gray-400">Win Rate</span>
<span class="text-sm font-mono text-gray-300">\${(s.win_rate||0).toFixed(1)}% (\${s.wins||0}/\${s.total_trades||0})</span>
</div>
</div></div></div>

<!-- Main area -->
<div class="lg:col-span-9">
<div class="bg-gray-900 rounded-xl border border-gray-800 h-full flex flex-col">
<div class="px-6 py-5 border-b border-gray-800 flex justify-between items-center">
<h2 class="text-lg font-semibold text-gray-100">Active Positions
<span class="ml-3 bg-gray-800 text-gray-400 text-xs px-2.5 py-0.5 rounded-full">\${pos.length}</span></h2>
<div class="flex items-center text-xs text-gray-500">
<span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse mr-2"></span>LIVE
</div>
</div>
<div class="overflow-x-auto flex-1">
<table class="w-full text-left border-collapse min-w-[750px]">
<thead><tr class="bg-gray-900/80 text-xs uppercase tracking-wider text-gray-500 border-b border-gray-800">
<th class="px-6 py-4 font-medium">Symbol / Dir</th>
<th class="px-6 py-4 font-medium">Vol</th>
<th class="px-6 py-4 font-medium">Open -> Current</th>
<th class="px-6 py-4 font-medium">SL / Status</th>
<th class="px-6 py-4 font-medium text-right">PnL</th>
<th class="px-6 py-4 font-medium text-right">Duration</th>
</tr></thead>
<tbody class="divide-y divide-gray-800/60">
\${pos.length===0?
'<tr><td colspan="6" class="px-6 py-24 text-center"><div class="text-gray-600 text-lg">No active positions</div><div class="text-sm mt-1 text-gray-700">Waiting for signals...</div></td></tr>':
[...pos].sort((a,b)=>b.profit-a.profit).map(p=>{
const isBuy=p.direction==="BUY";
const slStatus=p.sl_status==="break_even"?"Break-even":p.sl_status==="profit_lock"?"Locked":"Initial";
return \`<tr class="hover:bg-gray-800/30">
<td class="px-6 py-4"><div class="flex items-center"><span class="w-1.5 h-6 rounded-full mr-3 \${isBuy?"bg-emerald-500":"bg-rose-500"}"></span>
<div><div class="font-bold text-gray-200">\${p.symbol}</div>
<div class="text-xs font-semibold \${isBuy?"text-emerald-400":"text-rose-400"}">\${p.direction}</div></div></div></td>
<td class="px-6 py-4 font-mono text-gray-300">\${p.volume.toFixed(2)}</td>
<td class="px-6 py-4 font-mono text-sm"><div class="text-gray-400">\${p.open_price.toFixed(3)}</div>
<div class="text-gray-100 mt-1">\${p.current_price.toFixed(3)}</div></td>
<td class="px-6 py-4 font-mono text-sm"><div class="text-gray-200">\${p.sl?p.sl.toFixed(3):"NONE"}</div>
<div class="text-xs mt-1 font-sans \${p.sl_status==="initial"?"text-gray-500":p.sl_status==="break_even"?"text-amber-400":"text-emerald-400"}">\${slStatus}</div></td>
<td class="px-6 py-4 text-right"><div class="text-lg font-bold font-mono \${pnlColor(p.profit)}">\${p.profit>0?"+":""}\${fmt(p.profit)}</div></td>
<td class="px-6 py-4 text-right"><div class="flex items-center justify-end text-sm text-gray-400">
<svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
\${Math.floor(p.duration_min)}m</div></td>
</tr>\`
}).join("")}
</tbody></table></div></div></div></div>
\`;
setTimeout(render,3000);
}

async function killswitch(){
if(!confirm("Kill all positions?"))return;
const r=await fetch(API+"/api/killswitch",{method:"POST"});const d=await r.json();
alert("Killed: "+d.closed_positions+" pos, "+d.cancelled_orders+" orders");
}
render();
</script>
</body>
</html>"""
pathlib.Path("src/web/static/index.html").write_text(html, encoding="utf-8")
print("Written OK, size:", len(html))
