# note-auto/post_to_note.py
import os, json, time, tempfile, re, hashlib, subprocess
from pathlib import Path
from html import unescape

import yaml, requests
from bs4 import BeautifulSoup
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
    return {}  # { "<abs path to md>": "sha1" }

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
            if body.startswith('\n'):  # 先頭の改行を1つ食う
                body = body[1:]
            return fm, body
    return {}, md_text

def md_body_from_file(path: Path):
    raw = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    title = (fm.get("title") or path.stem).strip()
    return title, body, fm

def sha1_of_text(s: str)->str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def git_last_commit_ts(repo_root: Path, path: Path) -> int:
    """
    ファイルの最終コミットUNIX時刻を取得（git が無理なら mtime）
    """
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%ct", "--", str(path)],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out:
            return int(out)
    except Exception:
        pass
    try:
        return int(path.stat().st_mtime)
    except Exception:
        return 0

# ====== noteログイン/投稿 ======
def login(page, email, password):
    # /login → 入力 → /notes（マイ記事一覧）まで到達
    page.goto("https://note.com/login", timeout=60000, wait_until="domcontentloaded")
    page.get_by_placeholder("メールアドレス または note ID").fill(email)
    page.get_by_placeholder("パスワード").fill(password)
    page.get_by_role("button", name="ログイン").click()
    # ログイン後の到達先は安定しないので /notes へ誘導
    page.wait_for_load_state("networkidle")
    page.goto("https://note.com/notes", timeout=60000, wait_until="domcontentloaded")

def open_new_editor(page):
    # /new を経由して /notes/<id>/edit/ に来る
    page.goto("https://editor.note.com/new/", timeout=60000)
    page.wait_for_url("**/edit/**", timeout=60000)
    page.wait_for_selector("text=記事タイトル", timeout=20000)

def publish_flow(page):
    # 右上「公開に進む」→ publish画面 → 「投稿する」
    page.get_by_role("button", name=re.compile("公開に進む")).click(timeout=10000)
    page.wait_for_url("**/publish/**", timeout=60000)
    page.get_by_role("button", name=re.compile("投稿する")).click(timeout=10000)
    page.wait_for_load_state("networkidle")

def paste_markdown(page, text: str):
    """
    クリップボード経由で貼り付け（note のMD→リッチ変換を発火させやすい）
    """
    # クリップボード権限を与える（context 側で付与済み想定だが保険でtry）
    try:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://editor.note.com")
    except Exception:
        pass
    # クリップボードに書いて貼り付け
    page.evaluate("txt => navigator.clipboard.writeText(txt)", text)
    page.keyboard.press("Control+V")  # UbuntuランナーなのでCtrlでOK

def create_post(page, title, body_md):
    open_new_editor(page)

    # タイトル
    page.get_by_placeholder("記事タイトル").click()
    page.keyboard.type(title)

    # 本文：エディタにフォーカス→MDを貼り付け（note側で整形させる）
    editor = page.locator('[contenteditable="true"]').first
    editor.click()
    paste_markdown(page, body_md.strip() + "\n")

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
        # クリップボード権限
        try:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://editor.note.com")
        except Exception:
            pass

        page = ctx.new_page()
        login(page, email, password)

        changed = False

        for src in cfg.get("sources", []):
            repo_dir = Path(src["repo_dir"]).resolve()
            pattern = src.get("glob", "**/*.md")
            max_per_run = int(src.get("max_per_run", 1))

            files = sorted(
                repo_dir.glob(pattern),
                key=lambda f: git_last_commit_ts(repo_dir, f),
                reverse=True,
            )

            pushed = 0
            for f in files:
                # 1本だけ最新を優先したいときは、未投稿・差分ありの先頭でbreak
                rel_abs = str(f.resolve())
                md_title, md_body, fm = md_body_from_file(f)

                # 末尾の「出典」「カノニカル」等は不要、本文そのままを使う
                body_for_hash = md_body
                curr_sha = sha1_of_text(body_for_hash)

                prev_sha = posted_map.get(rel_abs)
                if prev_sha == curr_sha:
                    continue  # 変更なし

                # 投稿
                create_post(
                    page=page,
                    title=md_title,
                    body_md=md_body,
                )

                posted_map[rel_abs] = curr_sha
                changed = True
                pushed += 1
                if pushed >= max_per_run:
                    break  # この source の分は終了

        if changed:
            save_posted_map(posted_map)

        ctx.close(); browser.close()

if __name__ == "__main__":
    run_once()
