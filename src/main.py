#python3
#src/main.py

from flask import Flask, request, g, jsonify, render_template_string, redirect, url_for, session
from html.parser import HTMLParser
from datetime import datetime
import sqlite3
import urllib.request
import urllib.parse
import re
import time
import threading
import socket
import json
import os
import ipaddress
import secrets
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

# -----------------------
# 配置文件与常量
# -----------------------
DEFAULT_CONFIG = {
    "db_path": "sites.db",
    "max_fetch_bytes": 200 * 1024,
    "fetch_timeout": 8,
    "allowed_schemes": ["http", "https"],
    "user_agent": "Mozilla/5.0 (compatible; one_file_search_engine_bot/1.0; +https://github.com/xhdndmm/one_file_search_engine)",
    "robots_cache_ttl": 3600,
    "admin_user": "admin",
    "admin_password_hash": None,
    "secret_key": None,
    "disallow_private_networks": True
}
CONFIG_PATH = "config.json"

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception as e:
            print("读取 config.json 失败，使用默认配置。错误：", e)
    else:
        # 生成默认并保存（包括随机 secret_key 和默认 admin password hash）
        cfg["secret_key"] = secrets.token_hex(32)
        # 生成 hash for default 'admin' password
        cfg["admin_password_hash"] = generate_password_hash("admin")
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            print(f"已生成默认 {CONFIG_PATH}")
        except Exception as e:
            print("无法写入默认 config.json：", e)
    # 填补 secret / password hash
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
    if not cfg.get("admin_password_hash"):
        cfg["admin_password_hash"] = generate_password_hash("admin")
    return cfg

cfg = load_config()

DB_PATH = cfg.get("db_path", "sites.db")
MAX_FETCH_BYTES = cfg.get("max_fetch_bytes", 200 * 1024)
FETCH_TIMEOUT = cfg.get("fetch_timeout", 8)
ALLOWED_SCHEMES = tuple(cfg.get("allowed_schemes", ["http", "https"]))
USER_AGENT = cfg.get("user_agent", DEFAULT_CONFIG["user_agent"])
ROBOTS_CACHE_TTL = cfg.get("robots_cache_ttl", 3600)
DISALLOW_PRIVATE = cfg.get("disallow_private_networks", True)

app = Flask(__name__)
app.secret_key = cfg.get("secret_key") or secrets.token_hex(32)

# -----------------------
# 数据库与连接
# -----------------------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL;")
            db.execute("PRAGMA synchronous = NORMAL;")
        except Exception:
            pass
    return db

def init_db():
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
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
    try:
        c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS sites_fts USING fts5(
            url, title, keywords, description, snippet,
            content='sites', content_rowid='id'
        );
        """)
    except Exception as e:
        print("警告：创建 FTS5 失败（你的 SQLite 可能不支持 FTS5）。会使用回退搜索策略。错误:", e)
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
    key = (scheme, host)
    now = time.time()
    with robots_lock:
        entry = robots_cache.get(key)
        if entry and now - entry['fetched_at'] < ROBOTS_CACHE_TTL:
            return entry['rules']
    robots_url = f"{scheme}://{host}/robots.txt"
    rules = {'disallow': [], 'delay': 0}
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = resp.read(64*1024).decode('utf-8', errors='ignore')
            rules = _parse_robots_text(raw, USER_AGENT)
    except Exception:
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
    for dis in rules.get('disallow', []):
        if dis == '/':
            return False, rules
        if path.startswith(dis):
            return False, rules
    return True, rules

# -----------------------
# SSRF 防护：拒绝私网 IP
# -----------------------
def host_is_private(hostname):
    """
    解析 hostname（域名或 IP），判断是否落入私有/回环范围
    返回 True 表示为私有（应拒绝），False 表示公开
    如果无法解析或出错，返回 True（更保守的策略）
    """
    try:
        # strip possible port
        if ':' in hostname:
            hostname = hostname.split(':', 1)[0]
        # If hostname is already IP
        try:
            ip = ipaddress.ip_address(hostname)
            return ip.is_private or ip.is_loopback or ip.is_reserved
        except ValueError:
            pass
        # resolve all addresses
        infos = socket.getaddrinfo(hostname, None)
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
                if ip.is_private or ip.is_loopback or ip.is_reserved:
                    return True
            except Exception:
                # if parsing fails, be conservative
                return True
        return False
    except Exception:
        # 无法解析时更保守：认为私有/不可访问
        return True

# -----------------------
# 抓取器（安全检查 + robots）
# -----------------------
def is_media_url(url):
    lower = url.lower()
    media_ext = ('.jpg','.jpeg','.png','.gif','.bmp','.webp','.mp4','.mp3','.ogg',
                 '.avi','.mov','.wmv','.flv','.mkv','.pdf','.zip','.rar')
    return any(lower.endswith(ext) for ext in media_ext)

def validate_url_for_fetch(url):
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError("不允许的 URL scheme")
    # 禁止本地回环或私有地址（SSRF 防护）
    host = parsed.hostname or ''
    if DISALLOW_PRIVATE and host:
        if host_is_private(host):
            raise ValueError("拒绝抓取私有或回环地址（为防止 SSRF）")
    if is_media_url(parsed.path):
        raise ValueError("看起来是媒体/二进制文件，跳过")
    return True

def crawl_url(url):
    """
    返回 dict: {url, title, keywords, description, snippet}
    可能抛出异常（由上层处理）
    """
    parsed = urllib.parse.urlparse(url)
    validate_url_for_fetch(url)

    allowed, rules = is_allowed_by_robots(url)
    if not allowed:
        raise ValueError("robots.txt 禁止抓取该路径")

    delay = rules.get('delay', 0)
    if delay and delay > 0:
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
        try:
            cur.execute("""
                INSERT INTO sites_fts(rowid, url, title, keywords, description, snippet)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (rowid, info['url'], info['title'], info.get('keywords',''),
                  info.get('description',''), info.get('snippet','')))
        except sqlite3.OperationalError:
            pass
    except sqlite3.IntegrityError:
        cur.execute("""
            UPDATE sites SET title=?, keywords=?, description=?, snippet=?, crawled_at=?
            WHERE url=?
        """, (info['title'], info.get('keywords',''), info.get('description',''),
              info.get('snippet',''), now, info['url']))
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
        try:
            db.execute("SELECT bm25(sites_fts) FROM sites_fts LIMIT 0")
        except Exception:
            pass
        return True
    except Exception:
        return False

def search_sites(query, limit=50):
    q_raw = query.strip()
    if not q_raw:
        return []
    db = get_db()
    terms = [t for t in re.split(r'\s+', q_raw) if t]
    fts_query = " ".join(t + "*" for t in terms)
    res = []
    try:
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
    except sqlite3.OperationalError:
        try:
            rows = db.execute("""
                SELECT s.url, s.title, s.keywords, s.description, s.snippet, s.crawled_at, s.id
                FROM sites_fts
                JOIN sites s ON s.id = sites_fts.rowid
                WHERE sites_fts MATCH ?
                LIMIT 500
            """, (fts_query,)).fetchall()
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
            pass
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
# 管理认证（session）
# -----------------------
def admin_login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get("admin_logged_in"):
            return f(*args, **kwargs)
        return redirect(url_for("admin_login", next=request.path))
    return wrapped

# -----------------------
# 前端模板（保留原 UI），以及 Admin 模板
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

ADMIN_LOGIN_TMPL = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Admin Login</title></head>
<body>
  <h2>管理面板登录</h2>
  {% if error %}<p style="color:red">{{ error }}</p>{% endif %}
  <form method="post">
    <label>用户名: <input name="username" value="{{ username or '' }}"></label><br>
    <label>密码: <input name="password" type="password"></label><br>
    <button type="submit">登录</button>
  </form>
  <p><a href="/">返回首页</a></p>
</body>
</html>
"""

# 管理页面前端模板
ADMIN_DASHBOARD_TMPL = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Admin Dashboard</title></head>
<body>
  <h2>管理面板</h2>
  <p>欢迎，{{ user }}. <a href="{{ url_for('admin_logout') }}">登出</a></p>
  <h3>统计</h3>
  <ul>
    <li>站点总数: {{ stats.sites_count }}</li>
    <li>队列长度: {{ stats.queue_len }}</li>
    <li>日志条数 (最近 50): {{ stats.logs_count }}</li>
    <li>robots 缓存条目: {{ stats.robots_cache }}</li>
  </ul>

  <h3>操作</h3>
  <form method="post" action="{{ url_for('admin_reindex') }}">
    <button type="submit">重建 FTS 索引</button>
  </form>

  <h3>站点列表</h3>
  <table border="1" cellpadding="6">
    <tr><th>id</th><th>url</th><th>title</th><th>crawled_at</th><th>操作</th></tr>
    {% for s in sites %}
    <tr>
      <td>{{ s.id }}</td>
      <td><a href="{{ s.url }}" target="_blank">{{ s.url }}</a></td>
      <td>{{ s.title }}</td>
      <td>{{ s.crawled_at }}</td>
      <td>
        <form style="display:inline" method="post" action="{{ url_for('admin_recrawl') }}">
          <input type="hidden" name="url" value="{{ s.url }}">
          <button type="submit">重新抓取</button>
        </form>
        <form style="display:inline" method="post" action="{{ url_for('admin_delete_site') }}">
          <input type="hidden" name="url" value="{{ s.url }}">
          <button type="submit" onclick="return confirm('确认删除？')">删除</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>

  <h3>抓取队列（最近 50）</h3>
  <ul>
    {% for q in queue %}
      <li>{{ q.added_at }} - {{ q.url }}</li>
    {% endfor %}
  </ul>

  <h3>抓取日志（最近 50）</h3>
  <ul>
    {% for l in logs %}
      <li>{{ l.created_at }} - {{ l.status }} - {{ l.url }} - {{ l.detail }}</li>
    {% endfor %}
  </ul>

  <h3>修改管理员密码</h3>
  {% if pwd_msg %}<p style="color:green">{{ pwd_msg }}</p>{% endif %}
  {% if pwd_err %}<p style="color:red">{{ pwd_err }}</p>{% endif %}
  <form method="post" action="{{ url_for('admin_change_password') }}">
    <label>当前密码: <input name="current_password" type="password"></label><br>
    <label>新密码: <input name="new_password" type="password"></label><br>
    <button type="submit">修改密码</button>
  </form>

  <p><a href="/">返回首页</a></p>
</body>
</html>
"""

# -----------------------
# 路由：前端与提交
# -----------------------
@app.route("/", methods=["GET"])
def index():
    q = request.args.get("q", "").strip()
    results = []
    total = 0
    if q:
        results = search_sites(q)
        total = len(results)
    # 为了代码块清晰，把原先的 TEMPLATE 直接使用（确保你把上面 TEMPLATE 恢复为原字符串）
    return render_template_string(TEMPLATE, q=q, results=results, total=total)

@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "没有提供 URL"}), 400
    if not urllib.parse.urlparse(url).scheme:
        url = "http://" + url
    try:
        info = crawl_url(url)
    except Exception as e:
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
        db = get_db()
        db.execute("INSERT INTO crawl_logs (url, status, detail, created_at) VALUES (?, ?, ?, ?)",
                   (url, "ok", "", datetime.utcnow().isoformat()+"Z"))
        db.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": "数据库错误: %s" % e}), 500
    return jsonify({"ok": True, "url": info['url'], "title": info.get('title')})

# -----------------------
# 管理面板路由
# -----------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username != cfg.get("admin_user"):
            error = "用户名或密码错误"
        else:
            if check_password_hash(cfg.get("admin_password_hash",""), password):
                session["admin_logged_in"] = True
                session["admin_user"] = username
                next_url = request.args.get("next") or url_for("admin_dashboard")
                return redirect(next_url)
            else:
                error = "用户名或密码错误"
    return render_template_string(ADMIN_LOGIN_TMPL, error=error, username=username)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    session.pop("admin_user", None)
    return redirect(url_for("index"))

@app.route("/admin/dashboard", methods=["GET"])
@admin_login_required
def admin_dashboard():
    db = get_db()
    sites = db.execute("SELECT id, url, title, crawled_at FROM sites ORDER BY crawled_at DESC LIMIT 200").fetchall()
    queue = db.execute("SELECT url, added_at FROM crawl_queue ORDER BY added_at DESC LIMIT 50").fetchall()
    logs = db.execute("SELECT url, status, detail, created_at FROM crawl_logs ORDER BY created_at DESC LIMIT 50").fetchall()
    stats = {
        "sites_count": db.execute("SELECT COUNT(1) as c FROM sites").fetchone()["c"],
        "queue_len": db.execute("SELECT COUNT(1) as c FROM crawl_queue").fetchone()["c"],
        "logs_count": db.execute("SELECT COUNT(1) as c FROM crawl_logs").fetchone()["c"],
        "robots_cache": len(robots_cache)
    }
    return render_template_string(ADMIN_DASHBOARD_TMPL, user=session.get("admin_user"), sites=sites, queue=queue, logs=logs, stats=stats, pwd_msg=None, pwd_err=None)

@app.route("/admin/reindex", methods=["POST"])
@admin_login_required
def admin_reindex():
    ok, msg = rebuild_fts()
    if ok:
        return redirect(url_for("admin_dashboard"))
    else:
        return jsonify({"ok": False, "error": msg}), 500

@app.route("/admin/recrawl", methods=["POST"])
@admin_login_required
def admin_recrawl():
    url = (request.form.get("url") or "").strip()
    if not url:
        return "没有提供 url", 400
    try:
        info = crawl_url(url)
        upsert_site(info)
        db = get_db()
        db.execute("INSERT INTO crawl_logs (url, status, detail, created_at) VALUES (?, ?, ?, ?)",
                   (url, "ok", "admin_recrawl", datetime.utcnow().isoformat()+"Z"))
        db.commit()
    except Exception as e:
        db = get_db()
        db.execute("INSERT INTO crawl_logs (url, status, detail, created_at) VALUES (?, ?, ?, ?)",
                   (url, "error", str(e), datetime.utcnow().isoformat()+"Z"))
        db.commit()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/delete_site", methods=["POST"])
@admin_login_required
def admin_delete_site():
    url = (request.form.get("url") or "").strip()
    if not url:
        return "没有提供 url", 400
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("DELETE FROM sites WHERE url=?", (url,))
        try:
            # 删除 fts 对应 row
            cur.execute("DELETE FROM sites_fts WHERE url=?", (url,))
        except Exception:
            pass
        db.commit()
    except Exception as e:
        return str(e), 500
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/change_password", methods=["POST"])
@admin_login_required
def admin_change_password():
    current = request.form.get("current_password") or ""
    newpwd = request.form.get("new_password") or ""
    pwd_msg = None
    pwd_err = None
    if not check_password_hash(cfg.get("admin_password_hash",""), current):
        pwd_err = "当前密码不正确"
    elif not newpwd or len(newpwd) < 4:
        pwd_err = "新密码太短（至少 4 个字符）"
    else:
        # 更新 config.json 中的 hash
        cfg["admin_password_hash"] = generate_password_hash(newpwd)
        try:
            # 尝试持久化到 config.json
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                curcfg = json.load(f)
            curcfg.update(cfg)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(curcfg, f, indent=2)
            pwd_msg = "密码已更新"
        except Exception as e:
            pwd_err = "无法写入 config.json: " + str(e)
    # 重新渲染 dashboard 并显示消息
    db = get_db()
    sites = db.execute("SELECT id, url, title, crawled_at FROM sites ORDER BY crawled_at DESC LIMIT 200").fetchall()
    queue = db.execute("SELECT url, added_at FROM crawl_queue ORDER BY added_at DESC LIMIT 50").fetchall()
    logs = db.execute("SELECT url, status, detail, created_at FROM crawl_logs ORDER BY created_at DESC LIMIT 50").fetchall()
    stats = {
        "sites_count": db.execute("SELECT COUNT(1) as c FROM sites").fetchone()["c"],
        "queue_len": db.execute("SELECT COUNT(1) as c FROM crawl_queue").fetchone()["c"],
        "logs_count": db.execute("SELECT COUNT(1) as c FROM crawl_logs").fetchone()["c"],
        "robots_cache": len(robots_cache)
    }
    return render_template_string(ADMIN_DASHBOARD_TMPL, user=session.get("admin_user"), sites=sites, queue=queue, logs=logs, stats=stats, pwd_msg=pwd_msg, pwd_err=pwd_err)

# -----------------------
# 队列接口（受保护的 POST，GET 可公开查看）
# -----------------------
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
    print("one_file_search_engine v1.1-admin")
    init_db()
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=True)
