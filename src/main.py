#python3.x
#src/main.py

try:
    from flask import Flask,request,g,jsonify,render_template_string
    from html.parser import HTMLParser
    from datetime import datetime
    import sqlite3
    import urllib.request
    import urllib.parse
    import re
except Exception as e:
    print("无法导入全部库",e)

DB_PATH = "sites.db"
MAX_FETCH_BYTES = 200 * 1024  # 最多读取 200KB 页面（防止大文件）
FETCH_TIMEOUT = 8  # 秒
ALLOWED_SCHEMES = ('http', 'https')
USER_AGENT = "Mozilla/5.0 (compatible; one_file_search_engine_bot/1.0; +https://github.com/xhdndmm/one_file_search_engine)"

app = Flask(__name__)

# -----------------------
# 数据库相关
# -----------------------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
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
    db.commit()
    db.close()

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

# -----------------------
# HTML 解析器：提取 title/meta
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
            content = attrs.get("content", "")
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

# -----------------------
# HTML -> 可见文本提取（去脚本、样式）
# -----------------------
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
        # add a small separator for block tags
        if tag.lower() in ('p','div','br','li','h1','h2','h3','h4','h5','h6'):
            self.result.append('\n')
    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.result.append(text + ' ')
    def get_text(self):
        s = ''.join(self.result)
        # collapse whitespace
        s = re.sub(r'\s+', ' ', s).strip()
        return s

# -----------------------
# 爬虫：抓取页面并提取信息（防媒体/大文件）
# -----------------------
def is_media_url(url):
    # 根据扩展名简单判断
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
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        # 检查 content-type
        ctype = resp.headers.get("Content-Type", "")
        if not ctype:
            raise ValueError("无法确定内容类型")
        if "text/html" not in ctype:
            raise ValueError("不是 HTML 页面，跳过（Content-Type: %s）" % ctype)
        raw = resp.read(MAX_FETCH_BYTES + 1)
        if len(raw) > MAX_FETCH_BYTES:
            raw = raw[:MAX_FETCH_BYTES]
        # 尝试 decode（优先 charset）
        charset = "utf-8"
        m = re.search(r'charset=([^\s;]+)', ctype, re.I)
        if m:
            charset = m.group(1).strip(' "\'')
        try:
            text = raw.decode(charset, errors='replace')
        except Exception:
            text = raw.decode('utf-8', errors='replace')
    # 解析头部和文本
    headp = HeadMetaParser()
    try:
        headp.feed(text)
    except Exception:
        pass
    title = headp.title.strip() if headp.title else ""
    keywords = headp.meta.get("keywords", "")
    description = headp.meta.get("description", "") or headp.meta.get("og:description", "")
    # 提取可见文本 snippet
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
# DB 操作：添加/更新条目
# -----------------------
def upsert_site(info):
    db = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    try:
        db.execute("""
            INSERT INTO sites (url, title, keywords, description, snippet, crawled_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (info['url'], info['title'], info.get('keywords',''), info.get('description',''), info.get('snippet',''), now))
        db.commit()
    except sqlite3.IntegrityError:
        # 已存在 -> update
        db.execute("""
            UPDATE sites SET title=?, keywords=?, description=?, snippet=?, crawled_at=?
            WHERE url=?
        """, (info['title'], info.get('keywords',''), info.get('description',''), info.get('snippet',''), now, info['url']))
        db.commit()

# -----------------------
# 简单搜索：在 Python 端评分排序（多词）
# -----------------------
def search_sites(query, limit=50):
    q = query.strip().lower()
    terms = [t for t in re.split(r'\s+', q) if t]
    db = get_db()
    rows = db.execute("SELECT * FROM sites").fetchall()
    results = []
    for r in rows:
        score = 0
        title = (r['title'] or "").lower()
        keywords = (r['keywords'] or "").lower()
        desc = (r['description'] or "").lower()
        snippet = (r['snippet'] or "").lower()
        url = (r['url'] or "").lower()
        # 权重：title 3, keywords 2, description 2, snippet 1, url 1
        for t in terms:
            if t in title:
                score += 3
                score += title.count(t)  # 多次出现略加分
            if t in keywords:
                score += 2
                score += keywords.count(t)
            if t in desc:
                score += 2
                score += desc.count(t)
            if t in snippet:
                score += 1
                score += snippet.count(t)
            if t in url:
                score += 1
                score += url.count(t)
        if score > 0:
            results.append((score, r))
    results.sort(key=lambda x: (-x[0], x[1]['crawled_at'] or ""),)
    out = []
    for score, row in results[:limit]:
        out.append({
            "url": row["url"],
            "title": row["title"] or row["url"],
            "keywords": row["keywords"] or "",
            "description": row["description"] or "",
            "snippet": row["snippet"] or "",
            "crawled_at": row["crawled_at"],
            "score": score
        })
    return out

# -----------------------
# 路由：页面、提交 URL、搜索 API
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
        <div class="meta">{{ r.score }} pts · {{ r.crawled_at or '' }}</div>
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
      // 清空 input
      // document.getElementById('urlInput').value = '';
      // 简单闪一下
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
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        upsert_site(info)
    except Exception as e:
        return jsonify({"ok": False, "error": "数据库错误: %s" % e}), 500
    return jsonify({"ok": True, "url": info['url'], "title": info.get('title')})

# -----------------------
# 启动
# -----------------------
if __name__ == "__main__":
    print("one_file_search_engine v1.0")
    init_db()
    app.run(host='0.0.0.0', port=5000, threaded=True,debug=False)
