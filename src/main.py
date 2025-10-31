#python3
#src/main.py

from flask import Flask, request, g, jsonify, render_template_string
from html.parser import HTMLParser
from datetime import datetime
import sqlite3
import urllib.request
import urllib.parse
import re
import time
import threading
import socket

# 常量配置
DB_PATH = "sites.db"
MAX_FETCH_BYTES = 200 * 1024  # 最多读取 200KB 页面
FETCH_TIMEOUT = 8  # 秒
ALLOWED_SCHEMES = ('http', 'https')
USER_AGENT = "Mozilla/5.0 (compatible; one_file_search_engine_bot/1.0; +https://github.com/xhdndmm/one_file_search_engine)"
ROBOTS_CACHE_TTL = 3600  # robots 缓存秒数

app = Flask(__name__)

# -----------------------
# 数据库与连接
# -----------------------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
        # 启用 WAL 模式（提高并发写入性能）
        try:
            db.execute("PRAGMA journal_mode=WAL;")
            db.execute("PRAGMA synchronous = NORMAL;")
        except Exception:
            pass
    return db

def init_db():
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()

    # 主表
    c.execute("""
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY,
        url TEXT UNIQUE,
        title TEXT,
        keywords TEXT,
        description TEXT,
        snippet TEXT,
        crawled_at TEXT
    )
    """)

    # FTS5 全文表（content=sites，使用 rowid 链接）
    # 如果你的 sqlite 没有 FTS5，这句会抛错 -> 你需要安装支持 FTS5 的 sqlite
    try:
        c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS sites_fts USING fts5(
            url, title, keywords, description, snippet,
            content='sites', content_rowid='id'
        );
        """)
    except Exception as e:
        # 如果 FTS5 无法创建，打印警告但不阻止服务（运行时会回退）
        print("警告：创建 FTS5 失败（你的 SQLite 可能不支持 FTS5）。会使用回退搜索策略。错误:", e)

    # 爬取队列和日志
    c.execute("""
    CREATE TABLE IF NOT EXISTS crawl_queue (
        id INTEGER PRIMARY KEY,
        url TEXT UNIQUE,
        added_at TEXT
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS crawl_logs (
        id INTEGER PRIMARY KEY,
        url TEXT,
        status TEXT,
        detail TEXT,
        created_at TEXT
    )
    """)
    db.commit()
    db.close()

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

# -----------------------
# HTML 解析
# -----------------------
class HeadMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.title = ""
        self.meta = {}
    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        attrs = dict((k.lower(), v) for k, v in attrs)
        if t == "title":
            self.in_title = True
        if t == "meta":
            name = attrs.get("name", "").lower()
            prop = attrs.get("property", "").lower()
            content = attrs.get("content", "") or ""
            if name:
                self.meta[name] = content
            elif prop:
                self.meta[prop] = content
    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False
    def handle_data(self, data):
        if self.in_title:
            self.title += data.strip()

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
        self._skip = False
        self._skip_tags = set(['script', 'style', 'noscript'])
        self._last_tag = None
    def handle_starttag(self, tag, attrs):
        self._last_tag = tag.lower()
        if self._last_tag in self._skip_tags:
            self._skip = True
    def handle_endtag(self, tag):
        if tag.lower() in self._skip_tags:
            self._skip = False
        if tag.lower() in ('p','div','br','li','h1','h2','h3','h4','h5','h6'):
            self.result.append('\n')
    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.result.append(text + ' ')
    def get_text(self):
        s = ''.join(self.result)
        s = re.sub(r'\s+', ' ', s).strip()
        return s

# -----------------------
# robots.txt 解析与缓存
# -----------------------
robots_lock = threading.Lock()
robots_cache = {}  # host -> (fetched_at, rules)

def _parse_robots_text(txt, agent_name):
    """
    返回 dict: {'disallow': [paths], 'delay': float}
    简单实现：仅处理 User-agent, Disallow, Crawl-delay
    """
    lines = txt.splitlines()
    current_agents = []
    rules = {'disallow': [], 'delay': 0}
    for raw in lines:
        line = raw.split('#',1)[0].strip()
        if not line:
            continue
        if ':' not in line:
            continue
        k, v = line.split(':',1)
        k = k.strip().lower()
        v = v.strip()
        if k == 'user-agent':
            current_agents = [a.strip() for a in v.split()]
        elif k == 'disallow':
            if not current_agents:
                continue
            # apply only if agent matches '*' or our agent
            for a in current_agents:
                if a == '*' or a.lower() in agent_name.lower():
                    rules['disallow'].append(v or '/')
        elif k == 'crawl-delay':
            try:
                val = float(v)
                for a in current_agents:
                    if a == '*' or a.lower() in agent_name.lower():
                        rules['delay'] = max(rules.get('delay',0), val)
            except:
                pass
    return rules

def fetch_robots(host, scheme='https'):
    """
    返回 rules dict
    缓存一定时间，异常时返回空规则
    """
    key = (scheme, host)
    now = time.time()
    with robots_lock:
        entry = robots_cache.get(key)
        if entry and now - entry['fetched_at'] < ROBOTS_CACHE_TTL:
            return entry['rules']
    # fetch
    robots_url = f"{scheme}://{host}/robots.txt"
    rules = {'disallow': [], 'delay': 0}
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = resp.read(64*1024).decode('utf-8', errors='ignore')
            rules = _parse_robots_text(raw, USER_AGENT)
    except Exception:
        # 尝试 http if https failed
        if scheme == 'https':
            return fetch_robots(host, scheme='http')
    with robots_lock:
        robots_cache[key] = {'fetched_at': now, 'rules': rules}
    return rules

def is_allowed_by_robots(url):
    p = urllib.parse.urlparse(url)
    host = p.netloc
    scheme = p.scheme or 'https'
    rules = fetch_robots(host, scheme=scheme)
    path = p.path or '/'
    # 简单 prefix 匹配
    for dis in rules.get('disallow', []):
        if dis == '/':
            return False, rules
        if path.startswith(dis):
            return False, rules
    return True, rules

# -----------------------
# 抓取器（安全检查 + robots）
# -----------------------
def is_media_url(url):
    lower = url.lower()
    media_ext = ('.jpg','.jpeg','.png','.gif','.bmp','.webp','.mp4','.mp3','.ogg',
                 '.avi','.mov','.wmv','.flv','.mkv','.pdf','.zip','.rar')
    return any(lower.endswith(ext) for ext in media_ext)

def crawl_url(url):
    """
    返回 dict: {url, title, keywords, description, snippet}
    可能抛出异常（由上层处理）
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError("不允许的 URL scheme")
    if is_media_url(parsed.path):
        raise ValueError("看起来是媒体文件，跳过")
    allowed, rules = is_allowed_by_robots(url)
    if not allowed:
        raise ValueError("robots.txt 禁止抓取该路径")

    # 如果 robots 指定了 crawl-delay，短暂 sleep（谨慎遵守）
    delay = rules.get('delay', 0)
    if delay and delay > 0:
        # 最多等待 10 秒，避免过长阻塞
        time.sleep(min(delay, 10))

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if not ctype:
                raise ValueError("无法确定内容类型")
            if "text/html" not in ctype:
                raise ValueError("不是 HTML 页面，跳过（Content-Type: %s）" % ctype)
            raw = resp.read(MAX_FETCH_BYTES + 1)
            if len(raw) > MAX_FETCH_BYTES:
                raw = raw[:MAX_FETCH_BYTES]
            charset = "utf-8"
            m = re.search(r'charset=([^\s;]+)', ctype, re.I)
            if m:
                charset = m.group(1).strip(' "\'')
            try:
                text = raw.decode(charset, errors='replace')
            except Exception:
                text = raw.decode('utf-8', errors='replace')
    except urllib.error.HTTPError as he:
        raise ValueError(f"HTTP 错误: {he.code}")
    except urllib.error.URLError as ue:
        raise ValueError(f"URL 错误: {ue}")
    except socket.timeout:
        raise ValueError("请求超时")
    except Exception as e:
        raise ValueError(f"抓取失败: {e}")

    # 解析头部和正文
    headp = HeadMetaParser()
    try:
        headp.feed(text)
    except Exception:
        pass
    title = headp.title.strip() if headp.title else ""
    keywords = headp.meta.get("keywords", "")
    description = headp.meta.get("description", "") or headp.meta.get("og:description", "")
    te = TextExtractor()
    try:
        te.feed(text)
    except Exception:
        pass
    fulltext = te.get_text()
    snippet = (fulltext[:500] + "...") if len(fulltext) > 500 else fulltext
    return {
        "url": url,
        "title": title,
        "keywords": keywords,
        "description": description,
        "snippet": snippet
    }

# -----------------------
# DB 操作：upsert + FTS 同步
# -----------------------
def upsert_site(info):
    db = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO sites (url, title, keywords, description, snippet, crawled_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (info['url'], info['title'], info.get('keywords',''),
              info.get('description',''), info.get('snippet',''), now))
        rowid = cur.lastrowid
        # 同步到 FTS（如果存在）
        try:
            cur.execute("""
                INSERT INTO sites_fts(rowid, url, title, keywords, description, snippet)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (rowid, info['url'], info['title'], info.get('keywords',''),
                  info.get('description',''), info.get('snippet','')))
        except sqlite3.OperationalError:
            # 可能没有 FTS5 支持，忽略
            pass
    except sqlite3.IntegrityError:
        # 已存在 -> update
        cur.execute("""
            UPDATE sites SET title=?, keywords=?, description=?, snippet=?, crawled_at=?
            WHERE url=?
        """, (info['title'], info.get('keywords',''), info.get('description',''),
              info.get('snippet',''), now, info['url']))
        # 更新 FTS：删除并插入新的 row
        try:
            rowid = cur.execute("SELECT id FROM sites WHERE url=?", (info['url'],)).fetchone()[0]
            cur.execute("DELETE FROM sites_fts WHERE rowid=?", (rowid,))
            cur.execute("""
                INSERT INTO sites_fts(rowid, url, title, keywords, description, snippet)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (rowid, info['url'], info['title'], info.get('keywords',''),
                  info.get('description',''), info.get('snippet','')))
        except sqlite3.OperationalError:
            pass
    db.commit()

# -----------------------
# 搜索：优先 FTS5（带前缀模糊），回退策略
# -----------------------
def _fts_available(db):
    try:
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sites_fts'").fetchone()
        # 尝试 bm25 调用以确认是否可用（不抛出）
        try:
            db.execute("SELECT bm25(sites_fts) FROM sites_fts LIMIT 0")
        except Exception:
            # 即使 bm25 不可用，FTS5 可能仍能用 MATCH
            pass
        return True
    except Exception:
        return False

def search_sites(query, limit=50):
    q_raw = query.strip()
    if not q_raw:
        return []
    db = get_db()
    # 将空白分词，构建 FTS 前缀查询（如 "foo bar" -> "foo* bar*"）
    terms = [t for t in re.split(r'\s+', q_raw) if t]
    fts_query = " ".join(t + "*" for t in terms)  # 前缀匹配
    res = []
    # 优先使用 FTS5 MATCH
    try:
        # 使用 bm25 排序（越小越相关，故 ASC）
        sql = """
            SELECT s.url, s.title, s.keywords, s.description, s.snippet, s.crawled_at,
                   bm25(sites_fts) AS score
            FROM sites_fts
            JOIN sites s ON s.id = sites_fts.rowid
            WHERE sites_fts MATCH ?
            ORDER BY score ASC
            LIMIT ?
        """
        rows = db.execute(sql, (fts_query, limit)).fetchall()
        for r in rows:
            res.append({
                "url": r["url"],
                "title": r["title"] or r["url"],
                "keywords": r["keywords"] or "",
                "description": r["description"] or "",
                "snippet": r["snippet"] or "",
                "crawled_at": r["crawled_at"],
                "score": float(r["score"]) if r["score"] is not None else 0.0
            })
        if res:
            return res
    except sqlite3.OperationalError as e:
        # 可能没有 bm25 或 FTS5 支持 -> 回退到更兼容的查询
        # 回退策略：使用 MATCH 获取候选（不使用 bm25），然后 Python 侧评分排序
        try:
            rows = db.execute("""
                SELECT s.url, s.title, s.keywords, s.description, s.snippet, s.crawled_at, s.id
                FROM sites_fts
                JOIN sites s ON s.id = sites_fts.rowid
                WHERE sites_fts MATCH ?
                LIMIT 500
            """, (fts_query,)).fetchall()
            # Python 评分（尽量在候选集中进行）
            candidates = []
            lowq = q_raw.lower()
            qtoks = terms
            for r in rows:
                score = 0
                title = (r['title'] or "").lower()
                keywords = (r['keywords'] or "").lower()
                desc = (r['description'] or "").lower()
                snippet = (r['snippet'] or "").lower()
                url = (r['url'] or "").lower()
                for t in qtoks:
                    if t in title:
                        score += 3 + title.count(t)
                    if t in keywords:
                        score += 2 + keywords.count(t)
                    if t in desc:
                        score += 2 + desc.count(t)
                    if t in snippet:
                        score += 1 + snippet.count(t)
                    if t in url:
                        score += 1 + url.count(t)
                if score > 0:
                    candidates.append((score, r))
            candidates.sort(key=lambda x: (-x[0], x[1]['crawled_at'] or ""))
            for sc, r in candidates[:limit]:
                res.append({
                    "url": r["url"],
                    "title": r["title"] or r["url"],
                    "keywords": r["keywords"] or "",
                    "description": r["description"] or "",
                    "snippet": r["snippet"] or "",
                    "crawled_at": r["crawled_at"],
                    "score": sc
                })
            if res:
                return res
        except Exception:
            # 如果连 MATCH 都不行（例如没有 FTS5），回退到全表筛选（尽量减少开销）
            pass
    except Exception:
        pass

    # 最后回退：没有 FTS5 的情况下，做受限的全表扫描（limit 500），但只选出包含任一查询词的行
    rows = db.execute("SELECT * FROM sites LIMIT 1000").fetchall()
    final = []
    qtoks = [t.lower() for t in terms]
    for r in rows:
        score = 0
        title = (r['title'] or "").lower()
        keywords = (r['keywords'] or "").lower()
        desc = (r['description'] or "").lower()
        snippet = (r['snippet'] or "").lower()
        url = (r['url'] or "").lower()
        for t in qtoks:
            if t in title:
                score += 3 + title.count(t)
            if t in keywords:
                score += 2 + keywords.count(t)
            if t in desc:
                score += 2 + desc.count(t)
            if t in snippet:
                score += 1 + snippet.count(t)
            if t in url:
                score += 1 + url.count(t)
        if score > 0:
            final.append((score, r))
    final.sort(key=lambda x: (-x[0], x[1]['crawled_at'] or ""))
    for score, r in final[:limit]:
        res.append({
            "url": r["url"],
            "title": r["title"] or r["url"],
            "keywords": r["keywords"] or "",
            "description": r["description"] or "",
            "snippet": r["snippet"] or "",
            "crawled_at": r["crawled_at"],
            "score": score
        })
    return res

# -----------------------
# 重建 FTS 索引（接口）
# -----------------------
def rebuild_fts():
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM sites_fts;")
        # 将 sites 中全量导入 sites_fts
        rows = cur.execute("SELECT id, url, title, keywords, description, snippet FROM sites").fetchall()
        for r in rows:
            try:
                cur.execute("""
                    INSERT INTO sites_fts(rowid, url, title, keywords, description, snippet)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (r['id'], r['url'] or "", r['title'] or "", r['keywords'] or "", r['description'] or "", r['snippet'] or ""))
            except Exception:
                pass
        db.commit()
        return True, "FTS 重建完成"
    except Exception as e:
        return False, str(e)

# -----------------------
# 路由与前端（保留原 UI，添加 reindex/queue）
# -----------------------
TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>one_file_search_engine</title>
<style>
body{font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial; margin:0; background:#f7f7f8; color:#111}
.nav{background:#0b5cff;color:white;padding:12px 16px;display:flex;align-items:center;gap:12px}
.brand{font-weight:700}
.search-form {flex:1; display:flex; gap:8px}
input[type=text]{padding:8px 10px;border-radius:6px;border:1px solid rgba(0,0,0,0.1);width:100%}
.button{background:white;color:#0b5cff;border-radius:6px;padding:8px 10px;border:none;cursor:pointer}
.container{max-width:900px;margin:24px auto;padding:0 12px}
.card{background:white;padding:14px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.06);margin-bottom:12px}
.meta{color:#666;font-size:13px}
.small{font-size:13px;color:#666}
#msg{margin-left:8px;color:#ffd; font-size:13px}
</style>
</head>
<body>
<div class="nav">
  <div class="brand">one_file_search_engine</div>
  <form class="search-form" action="/" method="get" style="margin:0">
    <input name="q" type="text" placeholder="搜索（在标题、关键词、描述、内容中查找）" value="{{ q|default('') }}">
    <button class="button" type="submit">搜索</button>
  </form>

  <form id="submitForm" style="display:flex;gap:8px;align-items:center;" onsubmit="return submitUrl(event)">
    <input id="urlInput" type="text" placeholder="提交网址 (http(s)://...)" style="padding:6px 8px;border-radius:6px;border:none;width:260px">
    <button class="button" type="submit">提交并抓取</button>
    <span id="msg"></span>
  </form>
</div>

<div class="container">
  {% if q %}
    <div class="small">搜索结果： <strong>{{ total }}</strong> 条，关键词：<em>{{ q }}</em></div>
    <hr style="margin:12px 0 18px 0">
    {% for r in results %}
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <a href="{{ r.url }}" target="_blank" style="font-size:18px;color:#0b5cff;text-decoration:none">{{ r.title }}</a>
        <div class="meta">{{ "%.2f"|format(r.score) }} pts · {{ r.crawled_at or '' }}</div>
      </div>
      <div class="small" style="margin-top:6px">{{ r.snippet }}</div>
      <div class="small meta" style="margin-top:8px">URL: <a href="{{ r.url }}" target="_blank">{{ r.url }}</a> · 关键词: {{ r.keywords }}</div>
    </div>
    {% endfor %}
  {% else %}
    <div class="card">
      <h3>使用说明</h3>
      <ul>
        <li>在导航栏的输入框输入关键词回车或点击“搜索”。</li>
        <li>在右侧输入完整的网址（包含 http(s)://），点击“提交并抓取”将把该站点的 title、meta keywords、description 及页面文本摘录存入数据库。</li>
        <li>本示例不抓取媒体文件（图片/视频/pdf 等），并限制抓取大小以防止资源占用。</li>
        <br>
        <a href="https://github.com/xhdndmm/one_file_search_engine">源代码</a>
      </ul>
    </div>
  {% endif %}
  <div class="card">
    <h4>管理</h4>
    <div style="display:flex;gap:8px">
      <button onclick="reindex()" class="button">重建索引（/reindex）</button>
      <button onclick="viewQueue()" class="button">查看队列（/queue）</button>
    </div>
    <div id="adminMsg" class="small" style="margin-top:8px;color:#666"></div>
  </div>
</div>

<script>
async function submitUrl(ev){
  ev.preventDefault();
  const url = document.getElementById('urlInput').value.trim();
  const msg = document.getElementById('msg');
  msg.textContent = '提交中...';
  try {
    const resp = await fetch('/submit', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });
    const j = await resp.json();
    if (j.ok){
      msg.textContent = '已抓取: ' + (j.title || j.url);
      msg.style.color = '#dff';
      setTimeout(()=>{ msg.textContent = ''; }, 4000);
    }else{
      msg.textContent = '错误: ' + j.error;
      msg.style.color = '#ffd3d3';
      setTimeout(()=>{ msg.textContent = ''; }, 5000);
    }
  } catch(e){
    msg.textContent = '请求失败';
    msg.style.color = '#ffd3d3';
    setTimeout(()=>{ msg.textContent = ''; }, 4000);
  }
  return false;
}

async function reindex(){
  const msg = document.getElementById('adminMsg');
  msg.textContent = '重建中...';
  try {
    const r = await fetch('/reindex', {method:'POST'});
    const j = await r.json();
    if (j.ok) msg.textContent = '重建完成';
    else msg.textContent = '错误: ' + j.error;
  } catch(e){
    msg.textContent = '请求失败';
  }
  setTimeout(()=>{ msg.textContent = ''; }, 4000);
}

async function viewQueue(){
  const r = await fetch('/queue');
  const j = await r.json();
  alert(JSON.stringify(j, null, 2));
}
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    q = request.args.get("q", "").strip()
    results = []
    total = 0
    if q:
        results = search_sites(q)
        total = len(results)
    return render_template_string(TEMPLATE, q=q, results=results, total=total)

@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "没有提供 URL"}), 400
    # 确保带 scheme
    if not urllib.parse.urlparse(url).scheme:
        url = "http://" + url
    try:
        info = crawl_url(url)
    except Exception as e:
        # 记录日志
        try:
            db = get_db()
            db.execute("INSERT INTO crawl_logs (url, status, detail, created_at) VALUES (?, ?, ?, ?)",
                       (url, "error", str(e), datetime.utcnow().isoformat()+"Z"))
            db.commit()
        except:
            pass
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        upsert_site(info)
        # log success
        db = get_db()
        db.execute("INSERT INTO crawl_logs (url, status, detail, created_at) VALUES (?, ?, ?, ?)",
                   (url, "ok", "", datetime.utcnow().isoformat()+"Z"))
        db.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": "数据库错误: %s" % e}), 500
    return jsonify({"ok": True, "url": info['url'], "title": info.get('title')})

@app.route("/reindex", methods=["POST"])
def reindex():
    ok, msg = rebuild_fts()
    if ok:
        return jsonify({"ok": True, "msg": msg})
    else:
        return jsonify({"ok": False, "error": msg}), 500

@app.route("/queue", methods=["GET","POST"])
def queue():
    db = get_db()
    if request.method == "GET":
        rows = db.execute("SELECT url, added_at FROM crawl_queue ORDER BY added_at DESC LIMIT 200").fetchall()
        return jsonify({"ok": True, "queue": [{"url": r["url"], "added_at": r["added_at"]} for r in rows]})
    else:
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "没有提供 URL"}), 400
        if not urllib.parse.urlparse(url).scheme:
            url = "http://" + url
        try:
            db.execute("INSERT OR IGNORE INTO crawl_queue (url, added_at) VALUES (?, ?)", (url, datetime.utcnow().isoformat()+"Z"))
            db.commit()
            return jsonify({"ok": True, "url": url})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

# -----------------------
# 启动
# -----------------------
if __name__ == "__main__":
    print("one_file_search_engine v1.0")
    init_db()
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=True)
