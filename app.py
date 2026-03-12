from flask import Flask, jsonify, render_template_string
import yfinance as yf
import feedparser
import os
import threading
import time
from datetime import datetime, timezone, timedelta
import re

app = Flask(__name__)
BEIJING_TZ = timezone(timedelta(hours=8))

cache = {
    "quote": {"price": "N/A", "change": "N/A", "change_pct": "N/A", "is_positive": True, "updated": ""},
    "candles": [],
    "news": [],
    "ratings": [],
    "tweets": [],
    "recommendation": None,
    "last_full_update": None,
}
cache_lock = threading.Lock()

def get_bj():
    return datetime.now(BEIJING_TZ)

def safe_sleep(seconds):
    time.sleep(seconds)

# ── 股价（30秒刷新）
def refresh_quote():
    fail = 0
    while True:
        try:
            tsla = yf.Ticker("TSLA")
            info = tsla.fast_info
            price = round(float(info.last_price), 2)
            prev = round(float(info.previous_close), 2)
            change = round(price - prev, 2)
            pct = round((change / prev) * 100, 2)
            sign = "+" if change >= 0 else ""
            with cache_lock:
                cache["quote"] = {
                    "price": f"${price}",
                    "change": f"{sign}{change}",
                    "change_pct": f"{sign}{pct}%",
                    "is_positive": change >= 0,
                    "updated": get_bj().strftime("%H:%M:%S"),
                }
            fail = 0
            safe_sleep(30)
        except Exception as e:
            print(f"[Quote] {e}")
            fail += 1
            safe_sleep(min(60 * fail, 300))

# ── K线（5分钟刷新）
def refresh_candles():
    fail = 0
    while True:
        try:
            tsla = yf.Ticker("TSLA")
            hist = tsla.history(period="2d", interval="1m", prepost=True)
            candles = []
            for idx, row in hist.iterrows():
                if row["Volume"] == 0:
                    continue
                bj = idx.astimezone(BEIJING_TZ)
                candles.append({
                    "time": bj.strftime("%m/%d %H:%M"),
                    "open": round(float(row["Open"]), 2),
                    "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                })
            with cache_lock:
                cache["candles"] = candles[-240:]
            fail = 0
            safe_sleep(300)
        except Exception as e:
            print(f"[Candles] {e}")
            fail += 1
            safe_sleep(min(120 * fail, 600))

# ── 新闻（1小时刷新）
def refresh_news():
    while True:
        try:
            url = "https://news.google.com/rss/search?q=Tesla+TSLA+stock&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(url)
            excluded = ["fox news", "msnbc", "buzzfeed", "tmz"]
            news = []
            for entry in feed.entries[:20]:
                source = entry.get("source", {}).get("title", "Unknown")
                if any(ex in source.lower() for ex in excluded):
                    continue
                news.append({
                    "title": entry.title,
                    "url": entry.link,
                    "source": source,
                    "published": entry.get("published", ""),
                })
                if len(news) >= 6:
                    break
            with cache_lock:
                cache["news"] = news
        except Exception as e:
            print(f"[News] {e}")
        safe_sleep(3600)

# ── 评级（1小时刷新）
def refresh_ratings():
    fail = 0
    while True:
        try:
            tsla = yf.Ticker("TSLA")
            upgrades = tsla.upgrades_downgrades
            result = []
            if upgrades is not None and not upgrades.empty:
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
                recent = upgrades[upgrades.index >= cutoff]
                for idx, row in recent.head(8).iterrows():
                    bj_date = idx.astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
                    result.append({
                        "date": bj_date,
                        "firm": row.get("Firm", "Unknown"),
                        "from_grade": row.get("FromGrade", "—"),
                        "to_grade": row.get("ToGrade", "—"),
                        "url": "https://finance.yahoo.com/quote/TSLA/analysis/",
                    })
            with cache_lock:
                cache["ratings"] = result
            fail = 0
            safe_sleep(3600)
        except Exception as e:
            print(f"[Ratings] {e}")
            fail += 1
            safe_sleep(min(120 * fail, 600))

# ── 推文（30分钟刷新）
def refresh_tweets():
    while True:
        try:
            mirrors = [
                "https://nitter.poast.org/elonmusk/rss",
                "https://nitter.privacydev.net/elonmusk/rss",
                "https://nitter.net/elonmusk/rss",
                "https://nitter.1d4.us/elonmusk/rss",
            ]
            tweets = []
            for mirror in mirrors:
                try:
                    feed = feedparser.parse(mirror)
                    if not feed.entries:
                        continue
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                    for entry in feed.entries[:30]:
                        title = entry.get("title", "")
                        if title.startswith("RT @") or title.startswith("R to @"):
                            continue
                        published = entry.get("published_parsed")
                        if published:
                            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                            if pub_dt < cutoff:
                                continue
                        summary = entry.get("summary", title)
                        clean = re.sub(r'<[^>]+>', '', summary).strip()
                        clean = re.sub(r'\s+', ' ', clean)
                        tweets.append({
                            "text": clean[:400],
                            "url": entry.get("link", "https://x.com/elonmusk"),
                            "published": entry.get("published", ""),
                        })
                        if len(tweets) >= 5:
                            break
                    if tweets:
                        break
                except:
                    continue
            with cache_lock:
                cache["tweets"] = tweets
        except Exception as e:
            print(f"[Tweets] {e}")
        safe_sleep(1800)

# ── Buy/Hold/Sell（1小时刷新）
def refresh_recommendation():
    fail = 0
    while True:
        try:
            tsla = yf.Ticker("TSLA")
            info = tsla.info
            mean = info.get("recommendationMean")
            key = info.get("recommendationKey", "")
            num = info.get("numberOfAnalystOpinions", 0)
            if mean is not None:
                buy_score = round(max(0, min(10, (3.0 - mean) / 2.0 * 10)), 1)
                hold_score = round(max(0, min(10, 10 - abs(mean - 3.0) * 4)), 1)
                sell_score = round(max(0, min(10, (mean - 3.0) / 2.0 * 10)), 1)
                rec = {"mean": round(mean, 2), "key": key, "num_analysts": num,
                       "buy": buy_score, "hold": hold_score, "sell": sell_score}
            else:
                rec = None
            with cache_lock:
                cache["recommendation"] = rec
                cache["last_full_update"] = get_bj().strftime("%Y-%m-%d %H:%M:%S")
            fail = 0
            safe_sleep(3600)
        except Exception as e:
            print(f"[Rec] {e}")
            fail += 1
            safe_sleep(min(120 * fail, 600))

# 错开5秒启动各线程，避免同时触发rate limit
def delayed(fn, delay):
    time.sleep(delay)
    fn()

for i, fn in enumerate([refresh_quote, refresh_candles, refresh_news,
                         refresh_ratings, refresh_tweets, refresh_recommendation]):
    threading.Thread(target=delayed, args=(fn, i * 8), daemon=True).start()

# ── HTML ──────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tesla 每日简报</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.logo-area{display:flex;align-items:center;gap:12px}
.logo-area h1{font-size:18px;font-weight:700;color:#e6edf3}
.price-area{display:flex;align-items:center;gap:16px}
.price-big{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}
.price-change{font-size:13px;font-weight:600}
.pos{color:#3fb950}.neg{color:#f85149}
.updated{font-size:11px;color:#8b949e;margin-top:2px;text-align:right}
.refresh-btn{background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px}
.refresh-btn:hover{background:#30363d}
.container{max-width:920px;margin:0 auto;padding:20px}
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:18px;overflow:hidden}
.sec-title{padding:12px 18px;font-size:12px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid #30363d;background:#0d1117;display:flex;align-items:center;gap:8px}
.sec-body{padding:14px 18px}
.news-item{padding:11px 0;border-bottom:1px solid #21262d}
.news-item:last-child{border-bottom:none}
.news-title a{color:#58a6ff;text-decoration:none;font-size:14px;line-height:1.5}
.news-title a:hover{text-decoration:underline}
.news-meta{font-size:11px;color:#8b949e;margin-top:3px}
.rating-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #21262d;font-size:13px}
.rating-row:last-child{border-bottom:none}
.firm{font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grade{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap}
.gb{background:#1f4a2e;color:#3fb950}.gh{background:#3d2f00;color:#d29922}.gs{background:#3d0e0e;color:#f85149}.gd{background:#21262d;color:#8b949e}
.arrow{color:#555}
.tweet-item{padding:11px 0;border-bottom:1px solid #21262d;font-size:13px;line-height:1.6}
.tweet-item:last-child{border-bottom:none}
.tweet-link{display:inline-block;margin-top:5px;font-size:11px;color:#58a6ff;text-decoration:none}
#chart{width:100%;height:320px}
.score-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.score-card{background:#0d1117;border-radius:8px;padding:14px;text-align:center;border:1px solid #30363d}
.score-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px}
.score-num{font-size:36px;font-weight:800;font-variant-numeric:tabular-nums}
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
    <h1>Tesla 每日简报</h1>
  </div>
  <div class="price-area">
    <button class="refresh-btn" onclick="location.reload()">刷新页面</button>
    <div>
      <div style="display:flex;align-items:baseline;gap:8px">
        <span class="price-big" id="price">加载中</span>
        <span class="price-change" id="change"></span>
      </div>
      <div class="updated">股价: <span id="quote-time">—</span> · 数据: <span id="full-time">—</span></div>
    </div>
  </div>
</div>
<div class="container">
  <div class="section">
    <div class="sec-title">📰 最新资讯</div>
    <div class="sec-body" id="news-body"><div class="empty">加载中...</div></div>
  </div>
  <div class="section">
    <div class="sec-title">📊 近期股价走势 <span id="candle-count" style="color:#555;font-weight:400;font-size:11px"></span></div>
    <div style="padding:12px 12px 0"><div id="chart"></div></div>
    <div style="padding:6px 18px 10px;font-size:11px;color:#555">含盘前盘后 · 1分钟K线 · 每5分钟更新</div>
  </div>
  <div class="section">
    <div class="sec-title">🐦 Elon Musk 近24小时推文</div>
    <div class="sec-body" id="tweets-body"><div class="empty">加载中...</div></div>
  </div>
  <div class="section">
    <div class="sec-title">🏦 分析师评级变动（近7天）</div>
    <div class="sec-body" id="ratings-body"><div class="empty">加载中...</div></div>
  </div>
  <div class="section">
    <div class="sec-title">🎯 今日 Buy / Hold / Sell 评分 <span id="verdict-badge"></span></div>
    <div class="sec-body" id="rec-body"><div class="empty">加载中...</div></div>
  </div>
</div>
<script>
let chart,candleSeries;
function gc(g){
  if(!g)return'gd';const l=g.toLowerCase();
  if(l.includes('buy')||l.includes('outperform')||l.includes('overweight')||l.includes('positive'))return'gb';
  if(l.includes('sell')||l.includes('underperform')||l.includes('underweight')||l.includes('negative'))return'gs';
  if(l.includes('hold')||l.includes('neutral')||l.includes('equal')||l.includes('market perform'))return'gh';
  return'gd';
}
function renderChart(candles){
  const el=document.getElementById('chart');
  if(!el)return;
  if(chart){chart.remove();chart=null;}
  document.getElementById('candle-count').textContent=candles.length?`(${candles.length}根)`:'';
  if(!candles.length){el.innerHTML='<div style="text-align:center;padding:60px;color:#8b949e;font-size:13px">暂无K线数据</div>';return;}
  chart=LightweightCharts.createChart(el,{width:el.clientWidth,height:320,
    layout:{background:{color:'#161b22'},textColor:'#8b949e'},
    grid:{vertLines:{color:'#21262d'},horzLines:{color:'#21262d'}},
    timeScale:{borderColor:'#30363d',timeVisible:true},
    rightPriceScale:{borderColor:'#30363d'},crosshair:{mode:1}});
  candleSeries=chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',borderUpColor:'#3fb950',borderDownColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149'});
  candleSeries.setData(candles.map((c,i)=>({time:i+1,open:c.open,high:c.high,low:c.low,close:c.close})));
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
  try{const d=await(await fetch('/api/candles')).json();renderChart(d.candles||[]);}catch(e){}
}
async function loadFull(){
  try{
    const d=await(await fetch('/api/full')).json();
    // 新闻
    const nb=document.getElementById('news-body');
    nb.innerHTML=d.news.length?d.news.map(n=>`<div class="news-item"><div class="news-title"><a href="${n.url}" target="_blank">${n.title}</a></div><div class="news-meta">${n.source} · ${n.published?new Date(n.published).toLocaleDateString('zh-CN'):''}</div></div>`).join(''):'<div class="empty">暂无新闻</div>';
    // 推文
    const tb=document.getElementById('tweets-body');
    tb.innerHTML=d.tweets.length?d.tweets.map(t=>`<div class="tweet-item">${t.text}<a href="${t.url}" target="_blank" class="tweet-link">→ 查看原推文</a></div>`).join(''):'<div class="empty">暂时无法获取推文 · <a href="https://x.com/elonmusk" target="_blank" style="color:#58a6ff">前往X查看</a></div>';
    // 评级
    const rb=document.getElementById('ratings-body');
    rb.innerHTML=d.ratings.length?d.ratings.map(r=>`<div class="rating-row"><div class="firm">${r.firm}</div><span class="grade ${gc(r.from_grade)}">${r.from_grade||'—'}</span><span class="arrow">→</span><span class="grade ${gc(r.to_grade)}">${r.to_grade||'—'}</span><span style="color:#8b949e;font-size:11px;margin-left:auto">${r.date}</span><a href="${r.url}" target="_blank" style="color:#58a6ff;font-size:11px">详情</a></div>`).join(''):'<div class="empty">近7天暂无评级变动</div>';
    // 评分
    const rec=d.recommendation;
    const eb=document.getElementById('rec-body');
    const badge=document.getElementById('verdict-badge');
    if(!rec){eb.innerHTML='<div class="empty">暂无分析师共识数据</div>';badge.innerHTML='';return;}
    const top=[{k:'buy',v:rec.buy},{k:'hold',v:rec.hold},{k:'sell',v:rec.sell}].sort((a,b)=>b.v-a.v)[0];
    badge.innerHTML=`<span class="verdict v-${top.k}">${top.k.toUpperCase()}</span>`;
    eb.innerHTML=`<div class="score-grid">
      <div class="score-card score-buy"><div class="score-label">BUY</div><div class="score-num">${rec.buy}</div><div class="score-bar-wrap"><div class="score-bar" style="width:${rec.buy*10}%"></div></div></div>
      <div class="score-card score-hold"><div class="score-label">HOLD</div><div class="score-num">${rec.hold}</div><div class="score-bar-wrap"><div class="score-bar" style="width:${rec.hold*10}%"></div></div></div>
      <div class="score-card score-sell"><div class="score-label">SELL</div><div class="score-num">${rec.sell}</div><div class="score-bar-wrap"><div class="score-bar" style="width:${rec.sell*10}%"></div></div></div>
    </div><div class="score-meta">基于 <strong>${rec.num_analysts}</strong> 位分析师共识 · 均值 ${rec.mean}/5.0 · <a href="https://finance.yahoo.com/quote/TSLA/analysis/" target="_blank">查看详情</a></div>`;
    document.getElementById('full-time').textContent=d.last_full_update||'—';
  }catch(e){}
}
refreshQuote();refreshCandles();loadFull();
setInterval(refreshQuote,30000);
setInterval(refreshCandles,300000);
setInterval(loadFull,3600000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/quote")
def api_quote():
    with cache_lock:
        return jsonify(cache["quote"])

@app.route("/api/candles")
def api_candles():
    with cache_lock:
        return jsonify({"candles": cache["candles"]})

@app.route("/api/full")
def api_full():
    with cache_lock:
        return jsonify({
            "news": cache["news"],
            "tweets": cache["tweets"],
            "ratings": cache["ratings"],
            "recommendation": cache["recommendation"],
            "last_full_update": cache["last_full_update"],
        })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
