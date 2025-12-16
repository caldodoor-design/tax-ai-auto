import os
import re
import time
import json
import hashlib
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup


# =========================
# 設定（GitHub Actions向けに修正済み）
# =========================
OUTPUT_ROOT = "data"  # ← ここを "data" に変更しました
RATE_LIMIT_SECONDS = 1.2
TIMEOUT = (10, 30)
RETRIES = 3
INCLUDE_KAISEI = False

# MAX制限なし（＝全部）
MAX_PAGES = None

TARGETS = [
    {
        "name": "法人税基本通達",
        "start_url": "https://www.nta.go.jp/law/tsutatsu/kihon/hojin/01.htm",
        "allow_prefixes": ["https://www.nta.go.jp/law/tsutatsu/kihon/hojin/"],
    },
    {
        "name": "所得税基本通達",
        "start_url": "https://www.nta.go.jp/law/tsutatsu/kihon/shotoku/01.htm",
        "allow_prefixes": ["https://www.nta.go.jp/law/tsutatsu/kihon/shotoku/"],
    },
    {
        "name": "消費税法基本通達",
        "start_url": "https://www.nta.go.jp/law/tsutatsu/kihon/shohi/01.htm",
        "allow_prefixes": ["https://www.nta.go.jp/law/tsutatsu/kihon/shohi/"],
    },
    {
        "name": "財産評価基本通達",
        "start_url": "https://www.nta.go.jp/law/tsutatsu/kihon/sisan/hyoka_new/01.htm",
        "allow_prefixes": ["https://www.nta.go.jp/law/tsutatsu/kihon/sisan/hyoka_new/"],
    },
]


# =========================
# ユーティリティ
# =========================
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180]

def url_to_filename(url: str) -> str:
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    tail = "_".join(parts[-4:]) if parts else "root"
    tail = sanitize_filename(tail)
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    return f"{tail}_{h}.json"

def normalize_url(base_url: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("#"):
        return None
    if href.lower().startswith(("javascript:", "mailto:", "tel:")):
        return None
    abs_url = urljoin(base_url, href)
    abs_url, _ = urldefrag(abs_url)
    return abs_url

def is_allowed(url: str, allow_prefixes: list[str]) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return False

    if u.scheme not in ("http", "https"):
        return False

    # NTAドメイン限定
    if u.netloc != "www.nta.go.jp":
        return False

    # prefix配下のみ
    if not any(url.startswith(p) for p in allow_prefixes):
        return False

    path_lower = u.path.lower()
    # 拡張子で除外
    if any(path_lower.endswith(ext) for ext in (
        ".pdf", ".zip", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".mp3", ".mp4"
    )):
        return False

    if (not INCLUDE_KAISEI) and ("/kaisei/" in path_lower):
        return False

    return True


# =========================
# 取得・抽出
# =========================
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tax-circulars-scraper/1.0",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    })
    return s

def fetch_html(session: requests.Session, url: str) -> str | None:
    for i in range(RETRIES):
        try:
            time.sleep(RATE_LIMIT_SECONDS)
            r = session.get(url, timeout=TIMEOUT)
            # 429/503対策：軽くバックオフ
            if r.status_code in (429, 503):
                time.sleep(2.5 * (i + 1))
                continue
            r.raise_for_status()
            r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:
            if i == RETRIES - 1:
                print(f"⚠️ fetch failed: {url}\n    {e}")
            time.sleep(1.5 * (i + 1))
    return None

def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return "Untitled"

def pick_main_container(soup: BeautifulSoup):
    # ありそうな順に拾う（ページにより構造が違うため）
    for sel in ["main", "div#main", "div.col-sm-12", "td.valign-top", "article", "div.contents", "body"]:
        node = soup.select_one(sel)
        if node:
            return node
    return soup.body

def clean_container(node):
    for sel in ["script", "style", "noscript", "header", "footer", "nav", "aside", "form"]:
        for t in node.select(sel):
            t.decompose()
    for cls in ["breadcrumb", "pankuzu", "topicpath", "global-nav", "local-nav", "sidemenu"]:
        for t in node.select(f".{cls}"):
            t.decompose()

def extract_text(soup: BeautifulSoup) -> str:
    node = pick_main_container(soup)
    if not node:
        return ""
    clean_container(node)
    text = node.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def extract_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    out = []
    for a in soup.select("a[href]"):
        u = normalize_url(page_url, a.get("href"))
        if u:
            out.append(u)
    return out


# =========================
# クロール → 1ファイル結合
# =========================
def crawl_and_merge(target: dict):
    name = target["name"]
    start_url = target["start_url"]
    allow_prefixes = target["allow_prefixes"]

    out_dir = os.path.join(OUTPUT_ROOT, sanitize_filename(name))
    pages_dir = os.path.join(out_dir, "pages_json")
    ensure_dir(pages_dir)

    state_path = os.path.join(out_dir, "state.json")
    merged_md_path = os.path.join(out_dir, f"{sanitize_filename(name)}_FULL.md")

    # 再開
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        visited = set(state.get("visited", []))
        order = state.get("order", [])
        queue = deque(state.get("queue", []))
        print(f"🔁 再開: {name} (visited={len(visited)}, queue={len(queue)})")
    else:
        visited = set()
        order = []
        queue = deque([start_url])
        print(f"🚀 開始: {name}")

    session = make_session()
    fetched = 0

    while queue:
        if MAX_PAGES is not None and fetched >= MAX_PAGES:
            break

        url = queue.popleft()
        if url in visited:
            continue
        if not is_allowed(url, allow_prefixes):
            continue

        html = fetch_html(session, url)
        visited.add(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "lxml")
        title = extract_title(soup)
        text = extract_text(soup)
        links = extract_links(soup, url)

        # ページ内容をJSONで保存（あとで順番どおりに結合するため）
        fn = url_to_filename(url)
        with open(os.path.join(pages_dir, fn), "w", encoding="utf-8") as f:
            json.dump({"url": url, "title": title, "text": text}, f, ensure_ascii=False)

        order.append(url)
        fetched += 1
        print(f"  ✅ ({len(order)}) {title[:60]}")

        # 次をキューに
        for u in links:
            if u not in visited and is_allowed(u, allow_prefixes):
                queue.append(u)

        # stateをこまめに保存（落ちても再開できる）
        if len(order) % 25 == 0:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({
                    "visited": sorted(list(visited)),
                    "order": order,
                    "queue": list(queue),
                }, f, ensure_ascii=False, indent=2)

    # 最終state保存
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({
            "visited": sorted(list(visited)),
            "order": order,
            "queue": list(queue),
        }, f, ensure_ascii=False, indent=2)

    # 1ファイルに結合（order順）
    with open(merged_md_path, "w", encoding="utf-8") as out:
        out.write(f"# {name}\n\n")
        out.write(f"- Start URL: {start_url}\n")
        out.write(f"- Saved pages: {len(order)}\n")
        out.write(f"- INCLUDE_KAISEI: {INCLUDE_KAISEI}\n\n")
        out.write("---\n\n")

        for i, url in enumerate(order, 1):
            fn = url_to_filename(url)
            p = os.path.join(pages_dir, fn)
            if not os.path.exists(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f)

            title = obj.get("title") or "Untitled"
            text = obj.get("text") or ""

            out.write(f"## {i}. {title}\n\n")
            out.write(f"- URL: {obj.get('url')}\n\n")
            out.write(text + "\n\n")
            out.write("---\n\n")

    print(f"🎉 完了: {merged_md_path}")
    return merged_md_path


# =========================
# 実行
# =========================
if __name__ == "__main__":
    ensure_dir(OUTPUT_ROOT)

    merged_files = []
    for t in TARGETS:
        merged_files.append(crawl_and_merge(t))

    print("\n✅ 生成されたFULL.md:")
    for p in merged_files:
        print(" -", p)
