import os, json, time, tempfile, re, hashlib, glob
from pathlib import Path
from html import unescape

import yaml, requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html2md
from playwright.sync_api import sync_playwright

# ====== 基本設定（CWD非依存）======
BASE = Path(__file__).parent.resolve()
POSTED = BASE / "posted.json"

def load_cfg():
    with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_posted_map():
    if POSTED.exists():
        return json.loads(POSTED.read_text(encoding="utf-8"))
    return {}  # { "content-repo/posts/sabou/xxx.md": "sha1" }

def save_posted_map(d):
    POSTED.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

# ====== MDユーティリティ ======
def split_frontmatter(md_text: str):
    """先頭の --- ... --- をfrontmatterと本文に分離"""
    if md_text.startswith('---'):
        parts = md_text.split('\n', 1)[1].split('\n---', 1)
        if len(parts) == 2:
            fm = yaml.safe_load(parts[0]) or {}
            body = parts[1]
            # 先頭の改行を食う
            if body.startswith('\n'): body = body[1:]
            return fm, body
    return {}, md_text

def md_body_from_file(path: Path):
    raw = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    title = fm.get("title") or path.stem
    # frontmatter内に "slug", "tags", "lang", "date" 等あれば後で使える
    return title, body, fm

def sha1_of_text(s: str)->str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ====== noteログイン/投稿 ======
def login(page, email, password):
    # /login → 入力 → /notes（マイ記事一覧）まで到達
    page.goto("https://note.com/login", timeout=60000, wait_until="domcontentloaded")
    page.get_by_placeholder("メールアドレス または note ID").fill(email)
    page.get_by_placeholder("パスワード").fill(password)
    page.get_by_role("button", name="ログイン").click()
    page.wait_for_url("**/notes**", timeout=60000)

def open_new_editor(page):
    # プロフの「投稿」押下と同等：/new を経由して /notes/<id>/edit/ に来る
    page.goto("https://editor.note.com/new/", timeout=60000)
    page.wait_for_url("**/edit/**", timeout=60000)
    # エディタの準備待ち
    page.wait_for_selector("text=記事タイトル", timeout=20000)

def publish_flow(page):
    # 右上「公開に進む」→ publish画面 → 「投稿する」
    page.get_by_role("button", name=re.compile("公開に進む")).click(timeout=10000)
    page.wait_for_url("**/publish/**", timeout=60000)
    # （タグ等は任意。今回は何もしない）
    page.get_by_role("button", name=re.compile("投稿する")).click(timeout=10000)
    # 成功すると /@user/nID?app_launch=false の下書き/公開画面に遷移
    page.wait_for_load_state("networkidle")

def create_post(page, author_id, title, body_md, footer_md, canonical_link=None, tags=None):
    open_new_editor(page)

    # タイトル
    page.get_by_placeholder("記事タイトル").click()
    page.keyboard.type(title)

    # 本文（+ 追記フッター）
    editor = page.locator('[contenteditable="true"]').first
    editor.click()
    page.keyboard.insert_text(body_md.strip() + "\n\n" + (footer_md or "").strip() + "\n")

    # 公開フロー
    publish_flow(page)

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
                rel = str(f) if f.is_absolute() else str(f.resolve())
                md_title, md_body, fm = md_body_from_file(f)
                link = fm.get("canonical") or fm.get("link") or fm.get("url") or ""  # あれば使う
                body_for_hash = md_body  # 本文内容で重複判定
                curr_sha = sha1_of_text(body_for_hash)

                prev_sha = posted_map.get(rel)
                if prev_sha == curr_sha:
                    continue  # 変更なし

                # footer 成形
                footer = footer_tpl.format(title=md_title, link=link or "")
                # いざ投稿
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
