import os, json, time, tempfile, re, hashlib, glob
from pathlib import Path
from html import unescape

import yaml, requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html2md
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ====== 基本設定（CWD非依存）======
BASE = Path(__file__).parent.resolve()
POSTED = BASE / "posted.json"

def load_cfg():
    with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_posted_map():
    if POSTED.exists():
        return json.loads(POSTED.read_text(encoding="utf-8"))
    return {}  # { "absolute/or/resolved/path/to/post.md": "sha1" }

def save_posted_map(d):
    POSTED.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

# ====== MDユーティリティ ======
def split_frontmatter(md_text: str):
    """先頭の --- ... --- を frontmatter と本文に分離"""
    if md_text.startswith('---'):
        parts = md_text.split('\n', 1)[1].split('\n---', 1)
        if len(parts) == 2:
            fm = yaml.safe_load(parts[0]) or {}
            body = parts[1]
            if body.startswith('\n'):
                body = body[1:]
            return fm, body
    return {}, md_text

def md_body_from_file(path: Path):
    raw = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    title = fm.get("title") or path.stem
    # frontmatter に "slug","tags","lang","date","canonical" 等があれば後で利用
    return title, body, fm

def sha1_of_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ====== note ログイン/投稿 ======
def login(page, email, password):
    """
    /login に行ってメール/パスを入れてログイン完了まで。
    placeholder 固定に依存しない“頑丈版”。
    """
    page.goto("https://note.com/login", timeout=60000, wait_until="domcontentloaded")
    page.wait_for_load_state("domcontentloaded")

    # Cookie 同意など出ていれば潰す
    for label in ["同意", "同意する", "OK", "Accept", "許可", "わかった"]:
        try:
            page.get_by_role("button", name=re.compile(label)).click(timeout=800)
        except Exception:
            pass

    # 中継ボタンがあるパターンにも対応
    for label in ["メールアドレスでログイン", "メールでログイン"]:
        try:
            page.get_by_role("button", name=re.compile(label)).click(timeout=1000)
            break
        except Exception:
            pass

    # フォーム入力欄を“柔らかい”セレクタで拾う
    email_sel = "input[type='email'], input[name='email'], input[autocomplete='username'], input[placeholder*='メール'], input[placeholder*='note ID']"
    pass_sel  = "input[type='password'], input[name='password'], input[autocomplete='current-password'], input[placeholder*='パスワード']"

    try:
        page.wait_for_selector(email_sel, timeout=12000)
        page.locator(email_sel).first.fill(email)
        page.locator(pass_sel).first.fill(password)
    except Exception:
        # 最悪 form から強引に
        form = page.locator("form").first
        form.locator("input").nth(0).fill(email)
        form.locator("input[type='password']").first.fill(password)

    # 送信（ナビが起きるなら待つ。起きなくても続行）
    navigated = False
    try:
        with page.expect_navigation(wait_until="load", timeout=8000):
            page.get_by_role("button", name=re.compile("ログイン|Sign in")).click(timeout=1500)
        navigated = True
    except Exception:
        try:
            with page.expect_navigation(wait_until="load", timeout=8000):
                page.keyboard.press("Enter")
            navigated = True
        except Exception:
            pass

    # ネットワーク静止まで待機してから成功判定
    page.wait_for_load_state("networkidle")

    success_selectors = [
        "a[href*='/home']",
        "a[href*='/notifications']",
        "a[href^='/me']",
        "a[href*='/new']",
        "img[alt*='アイコン'], img[alt*='プロフィール']",
    ]
    ok = False
    for _ in range(24):  # 最大 ~12秒
        try:
            if "/home" in page.url or page.url.rstrip("/") == "https://note.com":
                ok = True
                break
            if any(page.locator(sel).count() > 0 for sel in success_selectors):
                ok = True
                break
        except Exception:
            pass
        page.wait_for_timeout(500)

    if not ok:
        raise RuntimeError(f"Login might have failed. current url={page.url}, navigated={navigated}")

def open_new_editor(page):
    """
    プロフ→投稿と同等。/new を経由して /notes/<id>/edit へ。
    """
    page.goto("https://editor.note.com/new/", timeout=60000)
    page.wait_for_url("**/edit/**", timeout=60000)
    # エディタ準備（タイトル/本文エリアのどちらかを待つ）
    ok = False
    for _ in range(20):
        if page.locator("textarea[placeholder='記事タイトル'], [placeholder='記事タイトル']").count() > 0:
            ok = True; break
        if page.locator('[contenteditable="true"]').count() > 0:
            ok = True; break
        page.wait_for_timeout(300)
    if not ok:
        raise RuntimeError("エディタが開けませんでした（タイトル/本文エリア検出失敗）")

def publish_flow(page):
    """
    右上の『公開に進む』→ publish 画面 → 『投稿する』まで。
    """
    page.get_by_role("button", name=re.compile("公開に進む")).click(timeout=10000)
    page.wait_for_url("**/publish/**", timeout=60000)
    # タグなどはスキップ
    page.get_by_role("button", name=re.compile("投稿する")).click(timeout=10000)
    # 遷移安定待ち
    page.wait_for_load_state("networkidle")


# --- 見出し抽出 & 挿入ユーティリティ -----------------
def _iter_blocks_for_note(md: str):
    """
    md を行ごとに走査して ('h2', text) or ('p', text) を返す。
    # と ## はどちらも h2 として扱う。
    """
    lines = md.splitlines()
    buf = []

    def flush_para():
        if buf:
            yield ('p', '\n'.join(buf).strip())
            buf.clear()

    for line in lines:
        m = re.match(r'^(#{1,2})\s+(.*)$', line.strip())
        if m:
            # 直前段落を出力
            for b in flush_para(): 
                yield b
            yield ('h2', m.group(2).strip())
        else:
            buf.append(line)

    for b in flush_para():
        yield b


def _insert_h2_block(page, text: str):
    """
    エディタで / → 大見出し を選び、見出しテキストを入力。
    メニュー取得失敗時は段落として継続（落とさない）。
    """
    editor = page.locator('[contenteditable="true"]').first
    editor.click()
    try:
        page.keyboard.press("/")
        page.keyboard.insert_text("h2")
        try:
            page.get_by_role("menuitem", name=re.compile("大見出し")).first.click(timeout=1200)
        except Exception:
            page.locator("text=大見出し").first.click(timeout=1200)
    except Exception:
        # フォールバック：段落として入れる
        _insert_paragraph(page, text)
        return

    page.keyboard.insert_text(text)
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")


def _insert_paragraph(page, text: str):
    editor = page.locator('[contenteditable="true"]').first
    editor.click()
    if text:
        page.keyboard.insert_text(text)
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")

# --- 投稿 -----------------
def create_post(page, author_id, title, body_md, footer_md=None, canonical_link=None, tags=None):
    open_new_editor(page)

    # タイトル
    try:
        title_box = page.locator("textarea[placeholder='記事タイトル'], [placeholder='記事タイトル']").first
        title_box.click()
    except Exception:
        pass
    page.keyboard.type(title)

    # 本文: # / ## を「大見出し(h2)」として挿入、それ以外は段落
    editor = page.locator('[contenteditable="true"]').first
    editor.click()

    for kind, text in _iter_blocks_for_note(body_md or ""):
        if kind == 'h2':
            _insert_h2_block(page, text)
        else:
            _insert_paragraph(page, text)

    # footer は入れない（要望どおり無視）

    # 公開
    publish_flow(page)

# ====== メイン ======
def run_once():
    cfg = load_cfg()
    posted_map = load_posted_map()
    email = os.environ["NOTE_EMAIL"]
    password = os.environ["NOTE_PASSWORD"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        login(page, email, password)

        changed = False

        for src in cfg.get("sources", []):
            repo_dir = Path(src["repo_dir"]).resolve()
            pattern = src.get("glob", "**/*.md")
            max_per_run = int(src.get("max_per_run", 1))
            footer_tpl = src.get("footer", "")

            files = sorted(repo_dir.glob(pattern))
            pushed = 0

            for f in files:
                rel = str(f.resolve())
                md_title, md_body, fm = md_body_from_file(f)
                link = fm.get("canonical") or fm.get("link") or fm.get("url") or ""
                body_for_hash = md_body
                curr_sha = sha1_of_text(body_for_hash)

                prev_sha = posted_map.get(rel)
                if prev_sha == curr_sha:
                    continue  # 変更なしはスキップ

                footer = footer_tpl.format(title=md_title, link=link or "")

                create_post(
                    page=page,
                    author_id=cfg["note"]["author_id"],
                    title=md_title,
                    body_md=md_body,
                    footer_md=footer,
                    canonical_link=link,
                    tags=fm.get("tags", []),
                )

                posted_map[rel] = curr_sha
                changed = True
                pushed += 1
                if pushed >= max_per_run:
                    break

        if changed:
            save_posted_map(posted_map)

        ctx.close(); browser.close()

if __name__ == "__main__":
    run_once()
