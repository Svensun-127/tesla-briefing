from flask import Flask, jsonify, render_template_string
import os, time, requests
from datetime import datetime, timezone, timedelta
from functools import lru_cache

app = Flask(__name__)
BEIJING_TZ = timezone(timedelta(hours=8))
NEWS_KEY = os.environ.get("NEWS_API_KEY", "d08226e0fa1f43f3932b74fec6e4142a")
AV_KEY   = os.environ.get("AV_API_KEY",   "FYWWE2WSTU9OEHE2")
TD_KEY   = os.environ.get("TWELVE_API_KEY","aab68d4088ec4f7d9762027839651f8b")

# 简单内存缓存，避免频繁调用API
_cache = {}
_cache_time = {}

def cached(key, ttl, fn):
    now = time.time()
    if key not in _cache or now - _cache_time.get(key, 0) > ttl:
        try:
            result = fn()
            if result is not None:
                _cache[key] = result
                _cache_time[key] = now
        except Exception as e:
            print(f"[{key}] {e}")
    return _cache.get(key)

def get(url, params=None):
    r = requests.get(url, params=params, timeout=15,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()

def fetch_quote():
    d = get("https://www.alphavantage.co/query", {
        "function": "GLOBAL_QUOTE", "symbol": "TSLA", "apikey": AV_KEY
    })
    q = d.get("Global Quote", {})
    if not q or not q.get("05. price"):
        return None
    price = float(q["05. price"])
    chg   = float(q["09. change"])
    pct   = q["10. change percent"].replace("%","")
    sign  = "+" if chg >= 0 else ""
    return {
        "price": f"${round(price,2)}",
        "change": f"{sign}{round(chg,2)}",
        "change_pct": f"{sign}{round(float(pct),2)}%",
        "is_positive": chg >= 0,
        "updated": datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
    }

def fetch_candles():
    d = get("https://www.alphavantage.co/query", {
        "function": "TIME_SERIES_INTRADAY", "symbol": "TSLA",
        "interval": "5min", "outputsize": "compact", "apikey": AV_KEY
    })
    ts = d.get("Time Series (5min)", {})
    if not ts:
        return None
    candles = []
    for t, v in sorted(ts.items()):
        candles.append({
            "time": t[5:], "open": round(float(v["1. open"]),2),
            "high": round(float(v["2. high"]),2), "low": round(float(v["3. low"]),2),
            "close": round(float(v["4. close"]),2), "volume": int(v["5. volume"])
        })
    return candles

def fetch_news():
    d = get("https://newsapi.org/v2/everything", {
        "q": "Tesla TSLA stock", "language": "en",
        "sortBy": "publishedAt", "pageSize": 8, "apiKey": NEWS_KEY
    })
    if d.get("status") != "ok":
        return None
    excluded = ["fox news","msnbc","buzzfeed","tmz","daily mail","new york post"]
    items = []
    for a in d.get("articles", []):
        src = (a.get("source",{}).get("name") or "").lower()
        if any(x in src for x in excluded): continue
        items.append({
            "title": a.get("title",""), "url": a.get("url","#"),
            "source": a.get("source",{}).get("name","Unknown"),
            "published": a.get("publishedAt","")
        })
    return items[:8]

def fetch_analyst():
    d = get("https://newsapi.org/v2/everything", {
        "q": "Tesla analyst price target rating upgrade downgrade",
        "language": "en", "sortBy": "publishedAt",
        "pageSize": 6, "apiKey": NEWS_KEY
    })
    if d.get("status") != "ok":
        return None
    return [{"title": a.get("title",""), "url": a.get("url","#"),
             "source": a.get("source",{}).get("name","Unknown"),
             "published": a.get("publishedAt","")}
            for a in d.get("articles", [])][:6]

def fetch_rec():
    d = get("https://api.twelvedata.com/recommendations", {
        "symbol": "TSLA", "apikey": TD_KEY
    })
    data = d.get("data", [])
    if not data: return None
    r = data[0]
    sb,b,h,s,ss = int(r.get("strong_buy",0)),int(r.get("buy",0)),int(r.get("hold",0)),int(r.get("sell",0)),int(r.get("strong_sell",0))
    tot = sb+b+h+s+ss
    if not tot: return None
    return {
        "buy": min(round((sb*10+b*7)/tot,1),10),
        "hold": min(round(h*10/tot,1),10),
        "sell": min(round((s*7+ss*10)/tot,1),10),
        "num_analysts": tot,
        "mean": round((sb*1+b*2+h*3+s*4+ss*5)/tot,2)
    }

@app.route("/api/quote")
def api_quote():
    data = cached("quote", 60, fetch_quote)
    return jsonify(data or {"price":"N/A","change":"N/A","change_pct":"N/A","is_positive":True,"updated":""})

@app.route("/api/candles")
def api_candles():
    data = cached("candles", 300, fetch_candles)
    return jsonify({"candles": data or []})

@app.route("/api/full")
def api_full():
    news     = cached("news",     900, fetch_news)
    analyst  = cached("analyst",  900, fetch_analyst)
    rec      = cached("rec",     3600, fetch_rec)
    return jsonify({
        "news": news or [],
        "analyst_articles": analyst or [],
        "tweets": [],
        "ratings": [],
        "recommendation": rec,
        "last_full_update": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    })

HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tesla小助手</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.logo-area{display:flex;align-items:center;gap:12px}
.logo-area h1{font-size:18px;font-weight:700;color:#e6edf3}
.price-area{display:flex;align-items:center;gap:16px}
.price-big{font-size:26px;font-weight:800}
.price-change{font-size:13px;font-weight:600}
.pos{color:#3fb950}.neg{color:#f85149}
.updated{font-size:11px;color:#8b949e;margin-top:2px;text-align:right}
.refresh-btn{background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px}
.container{max-width:920px;margin:0 auto;padding:20px}
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:18px;overflow:hidden}
.sec-title{padding:12px 18px;font-size:12px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid #30363d;background:#0d1117;display:flex;align-items:center;gap:8px}
.sec-body{padding:14px 18px}
.news-item{padding:11px 0;border-bottom:1px solid #21262d}
.news-item:last-child{border-bottom:none}
.news-title a{color:#58a6ff;text-decoration:none;font-size:14px;line-height:1.5}
.news-title a:hover{text-decoration:underline}
.news-meta{font-size:11px;color:#8b949e;margin-top:3px}
#chart{width:100%;height:320px}
.score-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.score-card{background:#0d1117;border-radius:8px;padding:14px;text-align:center;border:1px solid #30363d}
.score-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px}
.score-num{font-size:36px;font-weight:800}
.score-bar-wrap{background:#21262d;border-radius:4px;height:6px;margin-top:8px;overflow:hidden}
.score-bar{height:6px;border-radius:4px}
.score-buy .score-label,.score-buy .score-num{color:#3fb950}.score-buy .score-bar{background:#3fb950}
.score-hold .score-label,.score-hold .score-num{color:#d29922}.score-hold .score-bar{background:#d29922}
.score-sell .score-label,.score-sell .score-num{color:#f85149}.score-sell .score-bar{background:#f85149}
.score-meta{font-size:12px;color:#8b949e;text-align:center}
.score-meta a{color:#58a6ff;text-decoration:none}
.verdict{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;margin-left:8px}
.v-buy{background:#1f4a2e;color:#3fb950}.v-hold{background:#3d2f00;color:#d29922}.v-sell{background:#3d0e0e;color:#f85149}
.empty{color:#8b949e;font-size:13px;padding:4px 0}
</style>
</head>
<body>
<div class="header">
  <div class="logo-area">
    <svg width="30" height="30" viewBox="0 0 342 342" fill="#e82127" xmlns="http://www.w3.org/2000/svg">
      <path d="M0 66.5C56.9 66.5 85.4 78.3 85.4 78.3L171 342l85.6-263.7S285.1 66.5 342 66.5V0C285.1 0 256.6 11.7 256.6 11.7L171 275.4 85.4 11.7S56.9 0 0 0v66.5z"/>
      <path d="M171 66.5c-27.6 0-85.6-11.7-85.6-11.7S57 66.5 0 66.5c0 0 28.5 11.7 85.4 11.7H171h85.6c56.9 0 85.4-11.7 85.4-11.7-57 0-85.4-11.7-85.4-11.7S198.6 66.5 171 66.5z"/>
    </svg>
    <h1>Tesla小助手</h1>
  </div>
  <div class="price-area">
    <button class="refresh-btn" onclick="location.reload()">刷新页面</button>
    <div>
      <div style="display:flex;align-items:baseline;gap:8px">
        <span class="price-big" id="price">加载中</span>
        <span class="price-change" id="change"></span>
      </div>
      <div class="updated">更新: <span id="quote-time">—</span></div>
    </div>
  </div>
</div>
<div class="container">
  <div class="section">
    <div class="sec-title">📰 最新资讯</div>
    <div class="sec-body" id="news-body"><div class="empty">加载中...</div></div>
  </div>
  <div class="section">
    <div class="sec-title">🔬 分析师分析文章</div>
    <div class="sec-body" id="analyst-body"><div class="empty">加载中...</div></div>
  </div>
  <div class="section">
    <div class="sec-title">📊 近期股价走势</div>
    <div style="padding:12px 12px 0"><div id="chart"></div></div>
    <div style="padding:6px 18px 10px;font-size:11px;color:#555">5分钟K线</div>
  </div>
  <div class="section">
    <div class="sec-title">🎯 今日 Buy / Hold / Sell 评分 <span id="verdict-badge"></span></div>
    <div class="sec-body" id="rec-body"><div class="empty">加载中...</div></div>
  </div>
</div>
<script>
let chart;
function renderChart(candles){
  const el=document.getElementById('chart');
  if(chart){chart.remove();chart=null;}
  if(!candles||!candles.length){el.innerHTML='<div style="text-align:center;padding:60px;color:#8b949e;font-size:13px">暂无K线数据</div>';return;}
  chart=LightweightCharts.createChart(el,{width:el.clientWidth,height:320,layout:{background:{color:'#161b22'},textColor:'#8b949e'},grid:{vertLines:{color:'#21262d'},horzLines:{color:'#21262d'}},timeScale:{borderColor:'#30363d'},rightPriceScale:{borderColor:'#30363d'}});
  const s=chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',borderUpColor:'#3fb950',borderDownColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149'});
  s.setData(candles.map((c,i)=>({time:i+1,open:c.open,high:c.high,low:c.low,close:c.close})));
  chart.timeScale().fitContent();
  window.addEventListener('resize',()=>{if(chart)chart.resize(el.clientWidth,320);});
}
async function refreshQuote(){
  try{
    const d=await(await fetch('/api/quote')).json();
    document.getElementById('price').textContent=d.price;
    document.getElementById('price').className='price-big '+(d.is_positive?'pos':'neg');
    document.getElementById('change').innerHTML=`<span class="${d.is_positive?'pos':'neg'}">${d.change} (${d.change_pct})</span>`;
    document.getElementById('quote-time').textContent=d.updated||'—';
  }catch(e){}
}
async function refreshCandles(){
  try{const d=await(await fetch('/api/candles')).json();renderChart(d.candles);}catch(e){}
}
async function loadFull(){
  try{
    const d=await(await fetch('/api/full')).json();
    const nb=document.getElementById('news-body');
    nb.innerHTML=d.news&&d.news.length?d.news.map(n=>`<div class="news-item"><div class="news-title"><a href="${n.url}" target="_blank">${n.title}</a></div><div class="news-meta">${n.source} · ${n.published?new Date(n.published).toLocaleDateString('zh-CN'):''}</div></div>`).join(''):'<div class="empty">暂无新闻</div>';
    const ab=document.getElementById('analyst-body');
    ab.innerHTML=d.analyst_articles&&d.analyst_articles.length?d.analyst_articles.map(n=>`<div class="news-item"><div class="news-title"><a href="${n.url}" target="_blank">${n.title}</a></div><div class="news-meta">${n.source} · ${n.published?new Date(n.published).toLocaleDateString('zh-CN'):''}</div></div>`).join(''):'<div class="empty">暂无分析师文章</div>';
    const rec=d.recommendation;
    const eb=document.getElementById('rec-body');
    const badge=document.getElementById('verdict-badge');
    if(!rec){eb.innerHTML='<div class="empty">暂无数据</div>';badge.innerHTML='';}
    else{
      const top=[{k:'buy',v:rec.buy},{k:'hold',v:rec.hold},{k:'sell',v:rec.sell}].sort((a,b)=>b.v-a.v)[0];
      badge.innerHTML=`<span class="verdict v-${top.k}">${top.k.toUpperCase()}</span>`;
      eb.innerHTML=`<div class="score-grid"><div class="score-card score-buy"><div class="score-label">BUY</div><div class="score-num">${rec.buy}</div><div class="score-bar-wrap"><div class="score-bar" style="width:${rec.buy*10}%"></div></div></div><div class="score-card score-hold"><div class="score-label">HOLD</div><div class="score-num">${rec.hold}</div><div class="score-bar-wrap"><div class="score-bar" style="width:${rec.hold*10}%"></div></div></div><div class="score-card score-sell"><div class="score-label">SELL</div><div class="score-num">${rec.sell}</div><div class="score-bar-wrap"><div class="score-bar" style="width:${rec.sell*10}%"></div></div></div></div><div class="score-meta">基于 <strong>${rec.num_analysts}</strong> 位分析师共识</div>`;
    }
  }catch(e){console.error(e);}
}
refreshQuote();refreshCandles();loadFull();
setInterval(refreshQuote,60000);
setInterval(refreshCandles,300000);
setInterval(loadFull,900000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
