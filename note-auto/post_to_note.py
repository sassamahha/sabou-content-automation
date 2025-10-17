# note-auto/post_to_note.py

import os, json, re, hashlib, subprocess, random
from pathlib import Path
from html import unescape
from typing import Optional

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ====== 基本設定 ======
BASE = Path(__file__).parent.resolve()
POSTED = BASE / "posted.json"

def load_cfg():
    with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_posted_map():
    if POSTED.exists():
        return json.loads(POSTED.read_text(encoding="utf-8"))
    return {}

def save_posted_map(d):
    POSTED.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

# ====== MDユーティリティ ======
def split_frontmatter(md_text: str):
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

    # 行頭0〜3スペース + #1個をH1とする
    m = re.search(r'(?m)^\s{0,3}#(?!#)\s+(.+?)\s*#*\s*$', body)
    if m:
        title = unescape(m.group(1)).strip()
        start, end = m.span()
        body = (body[:start] + body[end:]).lstrip('\n')
    else:
        title = fm.get("title") or path.stem

    return title, body, fm

def sha1_of_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ====== Git 最終コミット時刻 ======
def git_last_commit_ts(repo_root: Path, file_path: Path) -> int:
    try:
        rel = str(file_path.relative_to(repo_root))
    except ValueError:
        rel = str(file_path)
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%ct", "--", rel],
            cwd=str(repo_root),
        ).decode("utf-8").strip()
        return int(out)
    except Exception:
        return int(file_path.stat().st_mtime)

# ====== Playwrightユーティリティ ======
def safe_click(page, locator_expr: str, timeout: int = 3000) -> bool:
    try:
        page.locator(locator_expr).first.click(timeout=timeout)
        return True
    except Exception:
        return False

def login(page, email, password):
    page.goto("https://note.com/login", timeout=60000, wait_until="domcontentloaded")

    # Cookie等のモーダル掃除
    for label in ["同意", "同意する", "OK", "Accept", "許可", "わかった", "閉じる", "×", "スキップ"]:
        try:
            page.get_by_role("button", name=re.compile(label)).click(timeout=800)
        except Exception:
            pass

    # 「メールでログイン」系
    for label in ["メールアドレスでログイン", "メールでログイン"]:
        try:
            page.get_by_role("button", name=re.compile(label)).click(timeout=1000)
            break
        except Exception:
            pass

    email_sel = "input[type='email'], input[name='email'], input[autocomplete='username'], input[placeholder*='メール'], input[placeholder*='note ID']"
    pass_sel  = "input[type='password'], input[name='password'], input[autocomplete='current-password'], input[placeholder*='パスワード']"

    page.wait_for_selector(email_sel, timeout=15000)
    page.locator(email_sel).first.fill(email)
    page.locator(pass_sel).first.fill(password)

    # 送信
    try:
        with page.expect_navigation(wait_until="load", timeout=12000):
            page.get_by_role("button", name=re.compile("ログイン|Sign in")).click(timeout=1500)
    except Exception:
        try:
            with page.expect_navigation(wait_until="load", timeout=12000):
                page.keyboard.press("Enter")
        except Exception:
            pass

    # 成功判定
    for _ in range(24):
        if "/home" in page.url or page.url.rstrip("/") == "https://note.com":
            break
        for sel in ["a[href*='/home']","a[href^='/me']","img[alt*='アイコン']","a[href*='/new']"]:
            if page.locator(sel).count() > 0:
                break
        page.wait_for_timeout(500)

    # ホームを一度踏む（Cookie/ドメイン跨ぎ安定）
    try:
        page.goto("https://note.com/", timeout=30000, wait_until="domcontentloaded")
    except Exception:
        pass

def open_editor_and_get_note_id(page) -> Optional[str]:
    """
    /new → /notes/<id>/edit へ到達。id を返す。
    """
    # 明示フロー：ホーム → new
    page.goto("https://note.com/", timeout=60000, wait_until="domcontentloaded")
    page.goto("https://editor.note.com/new", timeout=60000, wait_until="domcontentloaded")

    # /notes/<id>/edit/ への遷移を待つ（SPAでもURLが変わる想定）
    note_id = None
    for _ in range(80):  # 最大 ~24s
        m = re.search(r"/notes/([^/]+)/edit/?", page.url)
        if m:
            note_id = m.group(1)
            break
        # UIだけ先に出る場合もあるので、出現チェック
        if page.locator("[contenteditable='true']").count() > 0 or \
           page.locator("textarea[placeholder*='記事タイトル']").count() > 0:
            # まだURLが変わってなくても続行（IDは後で拾う）
            pass
        page.wait_for_timeout(300)

    # 念のためIDが取れてない場合は、編集画面内リンクから拾う
    if not note_id:
        try:
            hrefs = page.locator("a[href*='/notes/']").all()
            for h in hrefs:
                href = h.get_attribute("href") or ""
                m2 = re.search(r"/notes/([^/]+)/", href)
                if m2:
                    note_id = m2.group(1); break
        except Exception:
            pass

    # 最低限、エディタUIがあることは確認
    for _ in range(40):
        if page.locator("[contenteditable='true']").count() > 0 or \
           page.locator("textarea[placeholder*='記事タイトル']").count() > 0:
            break
        page.wait_for_timeout(300)

    return note_id

def paste_markdown(page, text: str):
    page.evaluate("async (t) => await navigator.clipboard.writeText(t)", text)
    # ランナーはLinuxなので基本Ctrl+VでOK
    page.keyboard.press("Control+V")

def save_draft(page):
    """
    下書き保存を確実に踏む。
    """
    # ボタンで保存
    for pat in [r"下書き保存", r"保存", r"Save draft"]:
        try:
            page.get_by_role("button", name=re.compile(pat)).first.click(timeout=3000)
            break
        except Exception:
            continue

    # トースト or 状態安定待ち
    for _ in range(30):
        toast = page.get_by_text(re.compile(r"保存しました|保存完了|保存されました|Saved")).count()
        if toast > 0:
            break
        page.wait_for_timeout(300)

def go_to_publish(page, note_id: Optional[str]):
    """
    『公開に進む』をクリック → /publish に到達する。
    note_id が取れていればURL直遷移フォールバックも持つ。
    """
    # まずはボタンで遷移
    try:
        page.get_by_role("button", name=re.compile(r"公開に進む")).click(timeout=8000)
    except Exception:
        pass

    # /publish/ を待つ（ボタン→SPA遷移想定）
    ok = False
    for _ in range(40):
        if "/publish/" in page.url:
            ok = True; break
        page.wait_for_timeout(300)

    # だめならURL直叩き
    if not ok and note_id:
        page.goto(f"https://editor.note.com/notes/{note_id}/publish/", timeout=60000, wait_until="domcontentloaded")

    # 最終確認
    if "/publish/" not in page.url:
        raise RuntimeError("publish画面へ遷移できませんでした")

def publish_flow(page):
    """
    publish画面で『投稿』確定。
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # 「無料」チェック（あればON）
    try:
        page.locator("label:has-text('無料')").first.click(timeout=800)
    except Exception:
        pass

    # 投稿/公開ボタン
    post_btn = page.get_by_role("button", name=re.compile(r"(投稿する|公開する|投稿を予約)")).first
    post_btn.wait_for(state="visible", timeout=20000)
    try:
        page.wait_for_function("el => !el.disabled", arg=post_btn, timeout=15000)
    except Exception:
        pass

    try:
        post_btn.click(timeout=15000)
    except Exception:
        try:
            handle = post_btn.element_handle()
            if handle:
                handle.scroll_into_view_if_needed(timeout=2000)
                page.evaluate("(el)=>el.click()", handle)
        except Exception:
            page.keyboard.press("Enter")

    # publish から抜けるまで待つ
    for _ in range(40):
        if "/publish" not in page.url:
            break
        page.wait_for_timeout(300)

# --- 投稿本体 ---
def create_post(page, author_id, title, body_md, footer_md=None, canonical_link=None, tags=None):
    # エディタを開く → note_id取得
    note_id = open_editor_and_get_note_id(page)

    # タイトル
    try:
        title_box = page.locator("textarea[placeholder*='記事タイトル'], [placeholder*='記事タイトル']").first
        title_box.click()
        page.keyboard.type(title)
    except Exception:
        pass

    # 本文貼付（Markdown）
    editor = page.locator('[contenteditable="true"]').first
    editor.click()
    paste_markdown(page, (body_md or "").strip())

    # 下書き保存 → 公開に進む → publish
    save_draft(page)
    go_to_publish(page, note_id)
    publish_flow(page)

# ====== メイン ======
def run_once():
    cfg = load_cfg()
    posted_map = load_posted_map()
    email = os.environ["NOTE_EMAIL"]
    password = os.environ["NOTE_PASSWORD"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        login(page, email, password)

        changed = False

        for src in cfg.get("sources", []):
            repo_dir = Path(src["repo_dir"]).resolve()
            pattern = src.get("glob", "**/*.md")
            max_per_run = int(src.get("max_per_run", 1))

            candidates = list(repo_dir.glob(pattern))
            files = sorted(candidates, key=lambda f: git_last_commit_ts(repo_dir, f), reverse=True)

            pushed = 0
            for f in files:
                rel = str(f.resolve())
                md_title, md_body, fm = md_body_from_file(f)
                link = fm.get("canonical") or fm.get("link") or fm.get("url") or ""
                curr_sha = sha1_of_text(md_body)
                prev_sha = posted_map.get(rel)
                if prev_sha == curr_sha:
                    continue

                try:
                    create_post(
                        page=page,
                        author_id=cfg["note"]["author_id"],
                        title=md_title,
                        body_md=md_body,
                        footer_md=None,
                        canonical_link=link,
                        tags=fm.get("tags", []),
                    )
                    posted_map[rel] = curr_sha
                    changed = True
                    pushed += 1
                except Exception as e:
                    # 壊れても他処理は続ける（保守コスト最小化）
                    print(f"[WARN] note post skipped: {rel} -> {e}")

                if pushed >= max_per_run:
                    break

        if changed:
            save_posted_map(posted_map)

        ctx.close(); browser.close()

if __name__ == "__main__":
    run_once()
