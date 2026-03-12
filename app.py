from flask import Flask, jsonify, render_template_string
import yfinance as yf
import feedparser
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
import requests

app = Flask(__name__)

# 存储最新数据
latest_data = {}
last_updated = None

BEIJING_TZ = timezone(timedelta(hours=8))

def get_beijing_time():
    return datetime.now(BEIJING_TZ)

def fetch_stock_data():
    """获取TSLA股价和K线数据"""
    try:
        tsla = yf.Ticker("TSLA")
        info = tsla.fast_info
        hist = tsla.history(period="2d", interval="1h")
        
        candles = []
        for idx, row in hist.iterrows():
            bj_time = idx.astimezone(BEIJING_TZ)
            candles.append({
                "time": bj_time.strftime("%m/%d %H:%M"),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"])
            })
        
        price = round(info.last_price, 2)
        prev_close = round(info.previous_close, 2)
        change = round(price - prev_close, 2)
        change_pct = round((change / prev_close) * 100, 2)
        sign = "+" if change >= 0 else ""
        
        return {
            "price": f"${price}",
            "change": f"{sign}{change}",
            "change_pct": f"{sign}{change_pct}%",
            "candles": candles[-20:]  # 最近20根K线
        }
    except Exception as e:
        print(f"[Stock] Error: {e}")
        return {"price": "N/A", "change": "N/A", "change_pct": "N/A", "candles": []}

def fetch_news():
    """从Google News RSS获取特斯拉新闻"""
    try:
        url = "https://news.google.com/rss/search?q=Tesla+TSLA+stock&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)
        
        excluded = ["fox news", "msnbc", "cnn opinion", "buzzfeed", "tmz"]
        news = []
        
        for entry in feed.entries[:15]:
            source = entry.get("source", {}).get("title", "Unknown").lower()
            if any(ex in source for ex in excluded):
                continue
            
            news.append({
                "title": entry.title,
                "url": entry.link,
                "source": entry.get("source", {}).get("title", "Unknown"),
                "published": entry.get("published", "")
            })
            
            if len(news) >= 6:
                break
        
        return news
    except Exception as e:
        print(f"[News] Error: {e}")
        return []

def fetch_rating_changes():
    """获取分析师评级变动"""
    try:
        tsla = yf.Ticker("TSLA")
        upgrades = tsla.upgrades_downgrades
        
        if upgrades is None or upgrades.empty:
            return []
        
        # 最近7天
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent = upgrades[upgrades.index >= cutoff]
        
        result = []
        for idx, row in recent.head(8).iterrows():
            bj_date = idx.astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
            result.append({
                "date": bj_date,
                "firm": row.get("Firm", "Unknown"),
                "from_grade": row.get("FromGrade", "—"),
                "to_grade": row.get("ToGrade", "—"),
                "action": row.get("Action", "—"),
                "url": "https://finance.yahoo.com/quote/TSLA/analysis/"
            })
        
        return result
    except Exception as e:
        print(f"[Ratings] Error: {e}")
        return []

def fetch_elon_tweets():
    """从Nitter抓取Elon推文"""
    try:
        # 尝试多个Nitter镜像
        mirrors = [
            "https://nitter.poast.org/elonmusk/rss",
            "https://nitter.privacydev.net/elonmusk/rss",
        ]
        
        for mirror in mirrors:
            try:
                feed = feedparser.parse(mirror)
                if feed.entries:
                    tweets = []
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                    
                    for entry in feed.entries[:20]:
                        published = entry.get("published_parsed")
                        if published:
                            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                            if pub_dt < cutoff:
                                continue
                        
                        # 过滤掉回复
                        title = entry.get("title", "")
                        if title.startswith("RT @") or title.startswith("@"):
                            continue
                        
                        # 清理HTML
                        summary = entry.get("summary", title)
                        import re
                        clean = re.sub(r'<[^>]+>', '', summary).strip()
                        
                        tweets.append({
                            "text": clean[:300],
                            "url": entry.get("link", "https://x.com/elonmusk"),
                            "published": entry.get("published", "")
                        })
                        
                        if len(tweets) >= 5:
                            break
                    
                    if tweets:
                        return tweets
            except:
                continue
        
        return [{"text": "暂时无法获取推文，请直接访问X查看", "url": "https://x.com/elonmusk", "published": ""}]
    except Exception as e:
        print(f"[Tweets] Error: {e}")
        return []

def fetch_earnings():
    """获取财报信息"""
    try:
        tsla = yf.Ticker("TSLA")
        cal = tsla.calendar
        
        if cal is not None and "Earnings Date" in cal:
            dates = cal["Earnings Date"]
            if hasattr(dates, '__iter__'):
                dates = list(dates)
            if dates:
                next_date = dates[0]
                return {
                    "type": "upcoming",
                    "date": str(next_date)[:10],
                    "url": "https://ir.tesla.com/#quarterly-disclosure"
                }
        
        return {
            "type": "latest",
            "date": "",
            "url": "https://ir.tesla.com/#quarterly-disclosure"
        }
    except Exception as e:
        print(f"[Earnings] Error: {e}")
        return {"type": "latest", "date": "", "url": "https://ir.tesla.com/#quarterly-disclosure"}

def refresh_data():
    """刷新所有数据"""
    global latest_data, last_updated
    print(f"[Refresh] Starting at {get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')} BJ")
    
    stock = fetch_stock_data()
    news = fetch_news()
    ratings = fetch_rating_changes()
    tweets = fetch_elon_tweets()
    earnings = fetch_earnings()
    
    latest_data = {
        "stock": stock,
        "news": news,
        "ratings": ratings,
        "tweets": tweets,
        "earnings": earnings,
    }
    last_updated = get_beijing_time().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[Refresh] Done. Updated at {last_updated} BJ")

def scheduler():
    """每天北京时间11点刷新"""
    while True:
        now = get_beijing_time()
        # 如果没有数据，立刻刷新一次
        if not latest_data:
            refresh_data()
        
        # 计算距离下一个11:00的秒数
        target = now.replace(hour=11, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        
        wait_seconds = (target - now).total_seconds()
        print(f"[Scheduler] Next refresh in {int(wait_seconds/3600)}h {int((wait_seconds%3600)/60)}m")
        time.sleep(wait_seconds)
        refresh_data()

# 启动后台调度线程
t = threading.Thread(target=scheduler, daemon=True)
t.start()

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tesla 每日简报</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
.header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
.header h1 { font-size: 20px; font-weight: 700; color: #e44d26; }
.price-block { text-align: right; }
.price { font-size: 28px; font-weight: 700; color: #e6edf3; }
.change { font-size: 15px; margin-top: 2px; }
.pos { color: #3fb950; } .neg { color: #f85149; }
.updated { font-size: 12px; color: #8b949e; margin-top: 4px; }
.container { max-width: 900px; margin: 0 auto; padding: 20px; }
.section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; margin-bottom: 20px; overflow: hidden; }
.section-title { padding: 14px 20px; font-size: 14px; font-weight: 600; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #30363d; background: #0d1117; }
.section-body { padding: 16px 20px; }
.news-item { padding: 12px 0; border-bottom: 1px solid #21262d; }
.news-item:last-child { border-bottom: none; }
.news-title a { color: #58a6ff; text-decoration: none; font-size: 14px; line-height: 1.5; }
.news-title a:hover { text-decoration: underline; }
.news-meta { font-size: 12px; color: #8b949e; margin-top: 4px; }
.rating-item { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid #21262d; font-size: 13px; }
.rating-item:last-child { border-bottom: none; }
.firm { font-weight: 600; flex: 1; }
.grade-change { display: flex; align-items: center; gap: 6px; }
.grade { padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
.grade-buy { background: #1f4a2e; color: #3fb950; }
.grade-hold { background: #3d2f00; color: #d29922; }
.grade-sell { background: #3d0e0e; color: #f85149; }
.grade-default { background: #21262d; color: #8b949e; }
.tweet-item { padding: 12px 0; border-bottom: 1px solid #21262d; font-size: 14px; line-height: 1.6; }
.tweet-item:last-child { border-bottom: none; }
.tweet-link { display: block; margin-top: 6px; font-size: 12px; color: #58a6ff; text-decoration: none; }
.earnings-box { display: flex; align-items: center; justify-content: space-between; }
.earnings-info { font-size: 14px; }
.earnings-link a { background: #1f6feb; color: #e6edf3; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 600; }
#chart { width: 100%; height: 300px; }
.loading { text-align: center; padding: 40px; color: #8b949e; font-size: 14px; }
.refresh-btn { background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.refresh-btn:hover { background: #30363d; }
.arrow { color: #8b949e; margin: 0 4px; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>⚡ Tesla 每日简报</h1>
    <div class="updated">更新时间: <span id="updated">加载中...</span></div>
  </div>
  <div style="display:flex;align-items:center;gap:16px">
    <button class="refresh-btn" onclick="loadData()">手动刷新</button>
    <div class="price-block">
      <div class="price" id="price">—</div>
      <div class="change" id="change">—</div>
    </div>
  </div>
</div>

<div class="container">
  <div id="main-content" class="loading">数据加载中，请稍候...</div>
</div>

<script>
function gradeClass(g) {
  if (!g) return 'grade-default';
  const lower = g.toLowerCase();
  if (lower.includes('buy') || lower.includes('outperform') || lower.includes('overweight') || lower.includes('positive')) return 'grade-buy';
  if (lower.includes('sell') || lower.includes('underperform') || lower.includes('underweight') || lower.includes('negative')) return 'grade-sell';
  if (lower.includes('hold') || lower.includes('neutral') || lower.includes('equal')) return 'grade-hold';
  return 'grade-default';
}

let chart, candleSeries;

function renderChart(candles) {
  const el = document.getElementById('chart');
  if (!el) return;
  if (chart) { chart.remove(); chart = null; }
  if (!candles || candles.length === 0) {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#8b949e">暂无K线数据</div>';
    return;
  }
  
  chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: 300,
    layout: { background: { color: '#161b22' }, textColor: '#8b949e' },
    grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    timeScale: { borderColor: '#30363d', timeVisible: true },
    rightPriceScale: { borderColor: '#30363d' },
  });
  
  candleSeries = chart.addCandlestickSeries({
    upColor: '#3fb950', downColor: '#f85149',
    borderUpColor: '#3fb950', borderDownColor: '#f85149',
    wickUpColor: '#3fb950', wickDownColor: '#f85149',
  });
  
  // 用索引作为时间（因为K线label是字符串）
  const data = candles.map((c, i) => ({
    time: i + 1,
    open: c.open, high: c.high, low: c.low, close: c.close
  }));
  candleSeries.setData(data);
  chart.timeScale().fitContent();
  
  // 显示时间标签
  const labels = candles.map(c => c.time);
  candleSeries.setMarkers(
    candles.filter((_, i) => i % 4 === 0).map((c, i) => ({
      time: (i * 4) + 1,
      position: 'belowBar',
      color: '#8b949e',
      shape: 'circle',
      text: c.time,
      size: 0
    }))
  );
}

async function loadData() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    
    // 价格
    const isPos = d.stock.change && !d.stock.change.startsWith('-');
    document.getElementById('price').textContent = d.stock.price;
    document.getElementById('change').innerHTML = `<span class="${isPos ? 'pos' : 'neg'}">${d.stock.change} (${d.stock.change_pct})</span>`;
    document.getElementById('updated').textContent = d.last_updated || '—';
    
    let html = '';
    
    // 新闻
    html += `<div class="section">
      <div class="section-title">📰 最新资讯</div>
      <div class="section-body">`;
    if (d.news.length === 0) {
      html += '<div style="color:#8b949e;font-size:14px">暂无新闻</div>';
    } else {
      d.news.forEach(n => {
        html += `<div class="news-item">
          <div class="news-title"><a href="${n.url}" target="_blank">${n.title}</a></div>
          <div class="news-meta">${n.source} · ${n.published ? new Date(n.published).toLocaleDateString('zh-CN') : ''}</div>
        </div>`;
      });
    }
    html += '</div></div>';
    
    // K线图
    html += `<div class="section">
      <div class="section-title">📊 近期股价走势 (${d.stock.candles?.length || 0}根K线)</div>
      <div class="section-body" style="padding:12px">
        <div id="chart"></div>
      </div>
    </div>`;
    
    // Elon推文
    html += `<div class="section">
      <div class="section-title">🐦 Elon Musk 近24小时推文</div>
      <div class="section-body">`;
    if (d.tweets.length === 0) {
      html += '<div style="color:#8b949e;font-size:14px">暂无推文</div>';
    } else {
      d.tweets.forEach(t => {
        html += `<div class="tweet-item">
          ${t.text}
          <a href="${t.url}" target="_blank" class="tweet-link">→ 查看原推文</a>
        </div>`;
      });
    }
    html += '</div></div>';
    
    // 评级变动
    html += `<div class="section">
      <div class="section-title">🏦 分析师评级变动（近7天）</div>
      <div class="section-body">`;
    if (d.ratings.length === 0) {
      html += '<div style="color:#8b949e;font-size:14px">近7天暂无评级变动</div>';
    } else {
      d.ratings.forEach(r => {
        html += `<div class="rating-item">
          <div class="firm">${r.firm}</div>
          <div class="grade-change">
            <span class="grade ${gradeClass(r.from_grade)}">${r.from_grade || '—'}</span>
            <span class="arrow">→</span>
            <span class="grade ${gradeClass(r.to_grade)}">${r.to_grade || '—'}</span>
          </div>
          <div style="color:#8b949e;font-size:12px">${r.date}</div>
          <a href="${r.url}" target="_blank" style="color:#58a6ff;font-size:12px">详情</a>
        </div>`;
      });
    }
    html += '</div></div>';
    
    // 财报
    if (d.earnings) {
      const label = d.earnings.type === 'upcoming' ? `下次财报：${d.earnings.date}` : '最新季报';
      html += `<div class="section">
        <div class="section-title">📋 财报信息</div>
        <div class="section-body">
          <div class="earnings-box">
            <div class="earnings-info">${label}</div>
            <div class="earnings-link"><a href="${d.earnings.url}" target="_blank">查看 Tesla IR →</a></div>
          </div>
        </div>
      </div>`;
    }
    
    document.getElementById('main-content').outerHTML = `<div class="container">${html}</div>`;
    
    // 渲染K线
    renderChart(d.stock.candles);
    
  } catch(e) {
    document.getElementById('main-content').innerHTML = `<div class="loading">加载失败：${e.message}<br><br><button class="refresh-btn" onclick="loadData()">重试</button></div>`;
  }
}

loadData();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/data")
def api_data():
    if not latest_data:
        # 还没加载完，同步加载一次
        refresh_data()
    return jsonify({**latest_data, "last_updated": last_updated})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    refresh_data()
    return jsonify({"ok": True, "last_updated": last_updated})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
