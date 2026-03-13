from flask import Flask, jsonify, render_template_string
import feedparser
import os
import threading
import time
import requests
from datetime import datetime, timezone, timedelta
import re
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 初始化Flask应用
app = Flask(__name__)
# 时区配置
BEIJING_TZ = timezone(timedelta(hours=8))
# API密钥（备用股价源）
TWELVE_KEY = os.environ.get("TWELVE_API_KEY", "aab68d4088ec4f7d9762027839651f8b")
TWELVE_BASE = "https://api.twelvedata.com"
# 请求头（适配反爬）
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

# 全局缓存+线程锁（保证多线程安全）
cache = {
    "quote": {"price": "N/A", "change": "N/A", "change_pct": "N/A", "is_positive": True, "updated": ""},
    "candles": [],
    "news": [],
    "analyst_articles": [],
    "ratings": [],
    "tweets": [],
    "recommendation": None,
    "last_full_update": None,
}
_lock = threading.Lock()

# 请求重试适配器（解决网络波动）
session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
session.mount("http://", HTTPAdapter(max_retries=retry_strategy))

# 工具函数：获取北京时区时间
def bj():
    return datetime.now(BEIJING_TZ)

# 1. 实时股价刷新（30秒/次）
def _refresh_quote():
    fails = 0
    while True:
        ok = False
        # 主源：Yahoo Finance
        try:
            r = session.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/TSLA",
                params={"interval": "1m", "range": "1d"},
                headers=YF_HEADERS, timeout=10, verify=False
            )
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            price = round(float(meta["regularMarketPrice"]), 2)
            prev = round(float(meta.get("chartPreviousClose") or meta.get("regularMarketPreviousClose") or meta.get("previousClose") or price), 2)
            chg = round(price - prev, 2)
            pct = round(chg / prev * 100, 2) if prev else 0
            sign = "+" if chg >= 0 else ""
            with _lock:
                cache["quote"] = {
                    "price": f"${price}", "change": f"{sign}{chg}", "change_pct": f"{sign}{pct}%",
                    "is_positive": chg >= 0, "updated": bj().strftime("%H:%M:%S")
                }
            fails = 0
            ok = True
        except Exception as e:
            print(f"[Quote/YF] 拉取失败: {str(e)[:50]}")
        # 备用源：Twelve Data
        if not ok:
            try:
                r = session.get(f"{TWELVE_BASE}/quote",
                                params={"symbol": "TSLA", "apikey": TWELVE_KEY},
                                timeout=10, verify=False)
                r.raise_for_status()
                d = r.json()
                price = round(float(d["close"]), 2)
                prev = round(float(d.get("previous_close", price)), 2)
                chg = round(price - prev, 2)
                pct = round(chg / prev * 100, 2) if prev else 0
                sign = "+" if chg >= 0 else ""
                with _lock:
                    cache["quote"] = {
                        "price": f"${price}", "change": f"{sign}{chg}", "change_pct": f"{sign}{pct}%",
                        "is_positive": chg >= 0, "updated": bj().strftime("%H:%M:%S")
                    }
                fails = 0
                ok = True
            except Exception as e:
                print(f"[Quote/TD] 备用源失败: {str(e)[:50]}")
                fails += 1
        # 失败后指数退避，成功则30秒刷新
        time.sleep(30 if ok else min(60 * fails, 300))

# 2. K线图刷新（5分钟/次）
def _refresh_candles():
    fails = 0
    while True:
        ok = False
        try:
            r = session.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/TSLA",
                params={"interval": "1m", "range": "1d", "includePrePost": "true"},
                headers=YF_HEADERS, timeout=15, verify=False
            )
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            ts_list = res.get("timestamp", [])
            q = res["indicators"]["quote"][0]
            opens, highs, lows, closes, vols = q.get("open", []), q.get("high", []), q.get("low", []), q.get("close", []), q.get("volume", [])
            candles = []
            for i, ts in enumerate(ts_list):
                try:
                    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                    if None in (o, h, l, c): continue
                    v = vols[i] if i < len(vols) else 0
                    dt = datetime.fromtimestamp(ts, tz=BEIJING_TZ)
                    candles.append({
                        "time": dt.strftime("%m/%d %H:%M"),
                        "open": round(float(o), 2), "high": round(float(h), 2),
                        "low": round(float(l), 2), "close": round(float(c), 2),
                        "volume": int(v) if v else 0
                    })
                except Exception:
                    continue
            with _lock:
                cache["candles"] = candles
            print(f"[Candles] 拉取成功，共{len(candles)}根K线")
            fails = 0
            ok = True
        except Exception as e:
            print(f"[Candles/YF] 拉取失败: {str(e)[:50]}")
            fails += 1
        time.sleep(300 if ok else min(120 * fails, 600))

# 3. 客观/非主流媒体新闻刷新（15分钟/次）
def _refresh_news():
    while True:
        try:
            # 精选客观/非主流媒体源
            sources = [
                "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSLA&region=US&lang=en-US",
                "https://electrek.co/feed/",  # 新能源客观媒体
                "https://www.teslarati.com/feed/",  # 特斯拉垂直客观媒体
                "https://feeds.bloomberg.com/energy/rss.xml",  # 彭博能源（客观）
                "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",  # 路透财经（客观）
                "https://arstechnica.com/feed/"  # 非主流科技媒体（客观）
            ]
            # 剔除流量/八卦媒体
            excluded = ["fox news", "msnbc", "buzzfeed", "tmz", "daily mail",
                        "new york post", "breitbart", "cnn business", "forbes"]
            # 优先客观媒体
            preferred = ["electrek", "teslarati", "reuters", "bloomberg", "ars technica",
                        "seeking alpha", "marketwatch", "benzinga"]
            keywords = ["tesla", "tsla", "elon musk", "ev", "electric vehicle", "tesla stock"]
            seen, items = set(), []

            for url in sources:
                try:
                    feed = feedparser.parse(url)
                    for e in feed.entries[:15]:
                        title = (e.get("title") or "").strip().lower()
                        if not title or title in seen: continue
                        if not any(k in title for k in keywords): continue
                        seen.add(title)
                        # 提取媒体源
                        src = (e.get("source", {}).get("title") or feed.feed.get("title") or "Unknown").lower()
                        if any(x in src for x in excluded): continue
                        # 组装新闻数据
                        items.append({
                            "title": e.get("title", "").strip(),
                            "url": e.get("link", "#"),
                            "source": src.title(),
                            "published": e.get("published", ""),
                            "pref": any(p in src for p in preferred)
                        })
                except Exception as ex:
                    print(f"[News/{url.split('/')[2]}] 源拉取失败: {str(ex)[:30]}")
            # 优先展示核心客观媒体，取前8条
            items.sort(key=lambda x: not x["pref"])
            result = [{k: v for k, v in x.items() if k != "pref"} for x in items[:8]]
            with _lock:
                cache["news"] = result
            print(f"[News] 刷新成功，共{len(result)}条客观新闻")
        except Exception as e:
            print(f"[News] 全局失败: {str(e)[:50]}")
        time.sleep(900)  # 15分钟刷新

# 4. 北美投行分析文章刷新（15分钟/次）
def _refresh_analyst_articles():
    while True:
        try:
            # 投行核心源
            sources = [
                "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSLA&region=US&lang=en-US",
                "https://seekingalpha.com/api/sa/combined/TSLA.xml",  # 投行研报核心平台
                "https://www.teslarati.com/feed/",
                "https://feeds.bloomberg.com/markets/rss.xml",  # 彭博投行研报
                "https://www.reuters.com/rssFeed/topic/37519"  # 路透特斯拉财经
            ]
            # 北美顶级投行关键词
            top_banks = [
                "morgan stanley", "goldman sachs", "jpmorgan", "wedbush", "barclays",
                "ubs", "bank of america", "citi", "deutsche bank", "canaccord genuity",
                "piper sandler", "bernstein", "jefferies", "cowen", "raymond james"
            ]
            analyst_kws = [
                "analyst", "price target", "rating", "forecast", "outlook", "overweight",
                "underweight", "upgrade", "downgrade", "initiates", "raises", "cuts", "research note"
            ]
            all_kws = top_banks + analyst_kws
            seen, articles = set(), []

            for url in sources:
                try:
                    feed = feedparser.parse(url)
                    for e in feed.entries[:20]:
                        title = (e.get("title") or "").strip().lower()
                        if not title or title in seen: continue
                        seen.add(title)
                        # 筛选含投行/分析术语的文章
                        if any(k in title for k in all_kws):
                            src = (e.get("source", {}).get("title") or feed.feed.get("title") or "Unknown").title()
                            articles.append({
                                "title": e.get("title", "").strip(),
                                "url": e.get("link", "#"),
                                "source": src,
                                "published": e.get("published", "")
                            })
                except Exception as ex:
                    print(f"[Analyst/{url.split('/')[2]}] 源拉取失败: {str(ex)[:30]}")
            # 取前6条投行分析文章
            with _lock:
                cache["analyst_articles"] = articles[:6]
            print(f"[Analyst] 刷新成功，共{len(articles)}条投行分析文章")
        except Exception as e:
            print(f"[Analyst] 全局失败: {str(e)[:50]}")
        time.sleep(900)  # 15分钟刷新

# 5. 投行评级变动刷新（1小时/次）
def _refresh_ratings():
    fails = 0
    while True:
        try:
            r = session.get(
                "https://query1.finance.yahoo.com/v10/finance/quoteSummary/TSLA",
                params={"modules": "upgradeDowngradeHistory"},
                headers=YF_HEADERS, timeout=10, verify=False
            )
            r.raise_for_status()
            # 解析投行评级历史
            history = r.json().get("quoteSummary", {}).get("result", [{}])[0].get("upgradeDowngradeHistory", {}).get("history", [])
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)  # 近7天评级
            result = []
            for item in history:
                try:
                    epoch = item.get("epochGradeDate", 0)
                    if not epoch: continue
                    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
                    if dt < cutoff: continue
                    # 筛选北美顶级投行评级
                    firm = item.get("firm", "Unknown").lower()
                    if any(bank in firm for bank in ["morgan stanley", "goldman", "jpmorgan", "wedbush", "ubs", "bofa", "citi"]):
                        result.append({
                            "date": dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d"),
                            "firm": item.get("firm", "Unknown").title(),
                            "from_grade": item.get("fromGrade") or "—",
                            "to_grade": item.get("toGrade") or "—",
                            "url": "https://finance.yahoo.com/quote/TSLA/analysis/"
                        })
                    if len(result) >= 8: break
                except Exception:
                    continue
            with _lock:
                cache["ratings"] = result
            print(f"[Ratings] 刷新成功，近7天共{len(result)}条投行评级变动")
            fails = 0
            time.sleep(3600)  # 1小时刷新
        except Exception as e:
            print(f"[Ratings] 拉取失败: {str(e)[:50]}")
            fails += 1
            time.sleep(min(120 * fails, 600))

# 6. 马斯克推文刷新（30分钟/次）
def _refresh_tweets():
    # 高可用Nitter镜像
    nitter_mirrors = [
        "https://nitter.poast.org/elonmusk/rss",
        "https://nitter.privacydev.net/elonmusk/rss",
        "https://nitter.nixnet.services/elonmusk/rss",
        "https://nitter.1d4.us/elonmusk/rss"
    ]
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            tweets = []
            # 遍历镜像，拉取成功则停止
            for url in nitter_mirrors:
                try:
                    feed = feedparser.parse(url, timeout=10)
                    if not feed.entries: continue
                    for e in feed.entries[:30]:
                        # 过滤转发推文
                        title = e.get("title", "")
                        if title.startswith(("RT @", "R to @", "Retweet")): continue
                        # 过滤24小时外的推文
                        pp = e.get("published_parsed")
                        if pp:
                            pub_dt = datetime(*pp[:6], tzinfo=timezone.utc)
                            if pub_dt < cutoff: continue
                        # 清洗推文内容
                        raw = e.get("summary", title)
                        text = re.sub(r"<[^>]+>", "", raw).strip()
                        text = re.sub(r"\s+", " ", text)
                        tweets.append({
                            "text": text[:400],
                            "url": e.get("link", "https://x.com/elonmusk"),
                            "published": e.get("published", "")
                        })
                        if len(tweets) >= 5: break  # 取前5条
                    if tweets:
                        print(f"[Tweets] 从{url}拉取成功，共{len(tweets)}条")
                        break
                except Exception as ex:
                    print(f"[Tweets/{url.split('/')[2]}] 镜像失败: {str(ex)[:30]}")
            with _lock:
                cache["tweets"] = tweets
        except Exception as e:
            print(f"[Tweets] 全局失败: {str(e)[:50]}")
        time.sleep(1800)  # 30分钟刷新

# 7. 投行共识评分刷新（1小时/次）
def _refresh_recommendation():
    fails = 0
    while True:
        try:
            r = session.get(
                "https://query1.finance.yahoo.com/v10/finance/quoteSummary/TSLA",
                params={"modules": "recommendationTrend"},
                headers=YF_HEADERS, timeout=10, verify=False
            )
            r.raise_for_status()
            trends = r.json().get("quoteSummary", {}).get("result", [{}])[0].get("recommendationTrend", {}).get("trend", [])
            rec = None
            if trends:
                t = trends[0]
                # 解析分析师评分
                sb, b, h, s, ss = map(int, [t.get(k, 0) for k in ["strongBuy", "buy", "hold", "sell", "strongSell"]])
                tot = sb + b + h + s + ss
                if tot > 0:
                    rec = {
                        "buy": min(round((sb * 10 + b * 7) / tot, 1), 10),
                        "hold": min(round(h * 10 / tot, 1), 10),
                        "sell": min(round((s * 7 + ss * 10) / tot, 1), 10),
                        "num_analysts": tot,
                        "mean": round((sb*1 + b*2 + h*3 + s*4 + ss*5) / tot, 2),
                    }
            with _lock:
                cache["recommendation"] = rec
                cache["last_full_update"] = bj().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[Rec] 投行共识评分刷新: {rec}")
            fails = 0
            time.sleep(3600)
        except Exception as e:
            print(f"[Rec] 拉取失败: {str(e)[:50]}")
            fails += 1
            time.sleep(min(120 * fails, 600))

# 后台线程启动（错开5秒，避免同时请求）
def _run_worker(fn, delay):
    time.sleep(delay)
    fn()

# 注册所有工作线程
workers = [
    _refresh_quote, _refresh_candles, _refresh_news,
    _refresh_analyst_articles, _refresh_ratings,
    _refresh_tweets, _refresh_recommendation
]
for idx, fn in enumerate(workers):
    threading.Thread(target=_run_worker, args=(fn, idx * 5), daemon=True).start()

# 前端HTML（完全保留原UI）
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
    <h1>Tesla小助手</h1>
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
    <div class="sec-title">🔬 分析师分析文章</div>
    <div class="sec-body" id="analyst-body"><div class="empty">加载中...</div></div>
  </div>
  <div class="section">
    <div class="sec-title">📊 近期股价走势 <span id="candle-count" style="color:#555;font-weight:400;font-size:11px"></span></div>
    <div style="padding:12px 12px 0"><div id="chart"></div></div>
    <div style="padding:6px 18px 10px;font-size:11px;color:#555">含盘前盘后 · 1分钟K线 · 每5分钟更新</div>
  </div>
  <div class="section">
    <div class="sec-title">🐦 ELON MUSK 近24小时推文</div>
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
let chart, candleSeries;
function gc(g){
  if(!g) return 'gd';
  const l = g.toLowerCase();
  if(l.includes('buy')||l.includes('outperform')||l.includes('overweight')||l.includes('positive')) return 'gb';
  if(l.includes('sell')||l.includes('underperform')||l.includes('underweight')||l.includes('negative')) return 'gs';
  if(l.includes('hold')||l.includes('neutral')||l.includes('equal')||l.includes('market perform')) return 'gh';
  return 'gd';
}
function renderChart(candles){
  const el = document.getElementById('chart');
  if(!el) return;
  if(chart){ chart.remove(); chart = null; }
  document.getElementById('candle-count').textContent = candles.length ? `(${candles.length}根)` : '';
  if(!candles.length){
    el.innerHTML = '<div style="text-align:center;padding:60px;color:#8b949e;font-size:13px">暂无K线数据（数据加载中）</div>';
    return;
  }
  chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: 320,
    layout:{ background:{color:'#161b22'}, textColor:'#8b949e' },
    grid:{ vertLines:{color:'#21262d'}, horzLines:{color:'#21262d'} },
    timeScale:{ borderColor:'#30363d', timeVisible:true },
    rightPriceScale:{ borderColor:'#30363d' },
    crosshair:{ mode:1 }
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:'#3fb950', downColor:'#f85149',
    borderUpColor:'#3fb950', borderDownColor:'#f85149',
    wickUpColor:'#3fb950', wickDownColor:'#f85149'
  });
  candleSeries.setData(candles.map((c,i) => ({
    time: i+1, open: c.open, high: c.high, low: c.low, close: c.close
  })));
  chart.timeScale().fitContent();
  window.addEventListener('resize', () => { if(chart) chart.resize(el.clientWidth, 320); });
}
async function refreshQuote(){
  try{
    const d = await (await fetch('/api/quote')).json();
    document.getElementById('price').textContent = d.price;
    document.getElementById('price').className = 'price-big ' + (d.is_positive ? 'pos' : 'neg');
    document.getElementById('change').innerHTML =
      `<span class="${d.is_positive?'pos':'neg'}">${d.change} (${d.change_pct})</span>`;
    document.getElementById('quote-time').textContent = d.updated || '—';
  }catch(e){}
}
async function refreshCandles(){
  try{
    const d = await (await fetch('/api/candles')).json();
    renderChart(d.candles || []);
  }catch(e){}
}
async function loadFull(){
  try{
    const d = await (await fetch('/api/full')).json();
    // 新闻渲染
    const nb = document.getElementById('news-body');
    nb.innerHTML = d.news.length
      ? d.news.map(n=>`<div class="news-item">
          <div class="news-title"><a href="${n.url}" target="_blank">${n.title}</a></div>
          <div class="news-meta">${n.source} · ${n.published ? new Date(n.published).toLocaleDateString('zh-CN') : ''}</div>
        </div>`).join('')
      : '<div class="empty">暂无新闻</div>';
    // 推文渲染
    const tb = document.getElementById('tweets-body');
    tb.innerHTML = d.tweets.length
      ? d.tweets.map(t=>`<div class="tweet-item">${t.text}
          <a href="${t.url}" target="_blank" class="tweet-link">→ 查看原推文</a>
        </div>`).join('')
      : '<div class="empty">暂时无法获取推文 · <a href="https://x.com/elonmusk" target="_blank" style="color:#58a6ff">前往X查看</a></div>';
    // 评级渲染
    const rb = document.getElementById('ratings-body');
    rb.innerHTML = d.ratings.length
      ? d.ratings.map(r=>`<div class="rating-row">
          <div class="firm">${r.firm}</div>
          <span class="grade ${gc(r.from_grade)}">${r.from_grade||'—'}</span>
          <span class="arrow">→</span>
          <span class="grade ${gc(r.to_grade)}">${r.to_grade||'—'}</span>
          <span style="color:#8b949e;font-size:11px;margin-left:auto">${r.date}</span>
          <a href="${r.url}" target="_blank" style="color:#58a6ff;font-size:11px">详情</a>
        </div>`).join('')
      : '<div class="empty">近7天暂无评级变动</div>';
    // 评分渲染
    const rec = d.recommendation;
    const eb  = document.getElementById('rec-body');
    const badge = document.getElementById('verdict-badge');
    if(!rec){
      eb.innerHTML = '<div class="empty">暂无分析师共识数据</div>';
      badge.innerHTML = '';
    } else {
      const top = [{k:'buy',v:rec.buy},{k:'hold',v:rec.hold},{k:'sell',v:rec.sell}]
                    .sort((a,b)=>b.v-a.v)[0];
      badge.innerHTML = `<span class="verdict v-${top.k}">${top.k.toUpperCase()}</span>`;
      eb.innerHTML = `<div class="score-grid">
        <div class="score-card score-buy">
          <div class="score-label">BUY</div>
          <div class="score-num">${rec.buy}</div>
          <div class="score-bar-wrap"><div class="score-bar" style="width:${rec.buy*10}%"></div></div>
        </div>
        <div class="score-card score-hold">
          <div class="score-label">HOLD</div>
          <div class="score-num">${rec.hold}</div>
          <div class="score-bar-wrap"><div class="score-bar" style="width:${rec.hold*10}%"></div></div>
        </div>
        <div class="score-card score-sell">
          <div class="score-label">SELL</div>
          <div class="score-num">${rec.sell}</div>
          <div class="score-bar-wrap"><div class="score-bar" style="width:${rec.sell*10}%"></div></div>
        </div>
      </div>
      <div class="score-meta">基于 <strong>${rec.num_analysts}</strong> 位分析师共识 · 均值 ${rec.mean}/5.0 ·
        <a href="https://finance.yahoo.com/quote/TSLA/analysis/" target="_blank">查看详情</a>
      </div>`;
    }
    // 分析师文章渲染
    const ab = document.getElementById('analyst-body');
    if(ab){
      ab.innerHTML = (d.analyst_articles && d.analyst_articles.length)
        ? d.analyst_articles.map(n=>`<div class="news-item">
            <div class="news-title"><a href="${n.url}" target="_blank">${n.title}</a></div>
            <div class="news-meta">${n.source} · ${n.published ? new Date(n.published).toLocaleDateString('zh-CN') : ''}</div>
          </div>`).join('')
        : '<div class="empty">暂无分析师文章</div>';
    }
    document.getElementById('full-time').textContent = d.last_full_update || '—';
  }catch(e){ console.error(e); }
}
// 初始化加载
refreshQuote();
refreshCandles();
loadFull();
// 定时刷新
setInterval(refreshQuote,   30000);
setInterval(refreshCandles, 300000);
setInterval(loadFull,       3600000);
</script>
</body>
</html>"""

# API接口路由
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/quote")
def api_quote():
    with _lock:
        return jsonify(cache["quote"])

@app.route("/api/candles")
def api_candles():
    with _lock:
        return jsonify({"candles": cache["candles"]})

@app.route("/api/full")
def api_full():
    with _lock:
        return jsonify({
            "news": cache["news"], "analyst_articles": cache["analyst_articles"],
            "tweets": cache["tweets"], "ratings": cache["ratings"],
            "recommendation": cache["recommendation"], "last_full_update": cache["last_full_update"]
        })

# 启动服务（适配Render环境）
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)