# note-auto/post_to_note.py
# --------------------------------------------
# ・GitHub Actions/CI でも固まらないように設計
# ・state.json / NOTE_STATE_B64 でログイン状態を再利用
# ・タイムアウト/スクショ/トレースで原因を特定しやすく
# ・content-repo/posts/{sabou|studyriver} から投稿対象を選ぶ
# ・デフォは「下書き保存」。公開したいときは NOTE_PUBLISH=true
#
# 期待するfrontmatter例:
# ---
# title: "【保存版】BonfiletチームOEMの最短チェックリスト"
# channel: "sabou"             # sabou | studyriver
# lang: "ja"
# teaser_pct: 0.35
# cta_primary: "bonfilet"      # bonfilet | rt2112 | newsletter
# cta_url_ja: "https://example.com/ja/..."
# cta_url_en: "https://example.com/en/..."
# cta_bonfilet: "https://bonfilet.jp/..."
# updated: "2025-10-17"
# tags: ["OEM","Bonfilet"]
# ---
#
# 必要な環境変数:
# - NOTE_EMAIL
# - NOTE_PASSWORD
# - NOTE_STATE_B64 (任意) base64のstorage state
# - NOTE_CHANNEL    (任意) sabou|studyriver（frontmatterが無ければこれで絞る）
# - NOTE_PUBLISH    (任意) trueで公開。未設定/falseは下書き保存
# --------------------------------------------

from __future__ import annotations
import os, sys, re, json, hashlib, base64, traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple, Dict, Any, Optional, List

import yaml
from markdownify import markdownify as html2md
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ===== パス =====
BASE = Path(__file__).parent.resolve()
ROOT = BASE.parent.resolve()
CONTENT_DIR = ROOT / "content-repo" / "posts"
POSTED = BASE / "posted.json"                 # 投稿済み管理（sha1）
SS_DIR = BASE / "screenshots"
TR_DIR = BASE / "traces"
for d in (SS_DIR, TR_DIR): d.mkdir(parents=True, exist_ok=True)

# ===== 環境変数 =====
NOTE_EMAIL = os.getenv("NOTE_EMAIL", "")
NOTE_PASSWORD = os.getenv("NOTE_PASSWORD", "")
NOTE_STATE_B64 = os.getenv("NOTE_STATE_B64", "")
NOTE_CHANNEL_ENV = os.getenv("NOTE_CHANNEL", "").strip()  # sabou|studyriver
NOTE_PUBLISH = os.getenv("NOTE_PUBLISH", "").lower() == "true"

# ===== ユーティリティ =====
def now_utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def load_posted_map() -> Dict[str, str]:
    if POSTED.exists():
        try:
            return json.loads(POSTED.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_posted_map(d: Dict[str, str]):
    POSTED.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def split_frontmatter(md_text: str) -> Tuple[Dict[str, Any], str]:
    """--- frontmatter --- をyamlとして読む"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", md_text, re.S)
    if not m:
        return {}, md_text
    fm = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    return fm, body

def strip_md_for_note(md: str) -> str:
    """note側に貼って崩れにくいシンプルMDへ（不要HTML除去→MD化→軽整形）"""
    # MD内にHTMLがある場合に備えて軽く正規化
    html = md
    # HTML→MD（乱れ軽減）
    _md = html2md(html, strip=["script", "style"])
    # 連続改行の正規化
    _md = re.sub(r"\n{3,}", "\n\n", _md).strip()
    return _md

def build_teaser(fm: Dict[str, Any], body_md: str) -> Tuple[str, str]:
    """note投稿用の title, content を作る（ティザー＋CTA）"""
    title = fm.get("title") or "【今日の実用】アップデートメモ"
    updated = fm.get("updated") or datetime.now().strftime("%Y-%m-%d")
    teaser_pct = float(fm.get("teaser_pct", 0.35))
    cta_primary = (fm.get("cta_primary") or "bonfilet").lower()

    # 本文ティザー切り出し（文字数ベース）
    body_plain = strip_md_for_note(body_md)
    cut = max(120, int(len(body_plain) * teaser_pct))
    preview = body_plain[:cut].rstrip()

    # 末尾CTAリンク決定
    url_ja = fm.get("cta_url_ja") or fm.get("url_ja") or fm.get("canonical") or "https://example.com/"
    url_en = fm.get("cta_url_en") or fm.get("url_en") or ""
    url_bf = fm.get("cta_bonfilet") or "https://bonfilet.jp/"

    # UTM付与（最低限）
    def with_utm(u: str, campaign: str) -> str:
        if not u: return u
        sep = "&" if "?" in u else "?"
        return f"{u}{sep}utm_source=note&utm_medium=teaser&utm_campaign={campaign}"

    # チャンネル名（計測に使う）
    ch = (fm.get("channel") or NOTE_CHANNEL_ENV or "sabou").lower()
    campaign = "sabou" if ch == "sabou" else "studyriver"

    url_ja = with_utm(url_ja, campaign)
    if url_en: url_en = with_utm(url_en, campaign)
    url_bf = with_utm(url_bf, campaign)

    # タイトルに更新日
    title_out = f"{title}（更新: {updated}）"

    # note本文テンプレ
    lines: List[str] = []
    # 先頭に軽いリード（frontmatterに要約があれば採用）
    if fm.get("summary"):
        lines.append(fm["summary"].strip())
        lines.append("")
    # プレビュー本文
    lines.append(preview)
    lines.append("")
    lines.append("---")
    lines.append("▼ 続き・図版・DL素材")
    lines.append(f"→ 正本（日本語）: {url_ja}")
    if url_en:
        lines.append(f"→ English: {url_en}")
    # CTA優先
    if cta_primary == "bonfilet":
        lines.append(f"→ Bonfilet（OEM相談）: {url_bf}")
    elif cta_primary == "rt2112":
        # 代替リンクがfrontmatterに無い場合は日本語URLを流用
        lines.append(f"→ Rt2112 一覧/読み方: {url_en or url_ja}")
    elif cta_primary == "newsletter":
        lines.append(f"→ 更新通知（無料）: {url_ja}")
    lines.append("---")
    content_out = "\n".join(lines).strip()

    return title_out, content_out

def pick_target_file(channel_hint: str = "") -> Path:
    """
    投稿対象の.mdを1本選ぶ。
    ルール:
      - channel_hint (sabou|studyriver) があればその配下を優先
      - 更新日時が新しいものを優先
      - posted.json に同一sha1が載っているものはスキップ
    """
    posted = load_posted_map()
    candidates: List[Path] = []

    def collect(dirpath: Path):
        if dirpath.exists():
            for p in sorted(dirpath.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
                candidates.append(p)

    if channel_hint:
        collect(CONTENT_DIR / channel_hint)
    else:
        collect(CONTENT_DIR / "sabou")
        collect(CONTENT_DIR / "studyriver")

    for p in candidates:
        text = p.read_text(encoding="utf-8")
        digest = sha1_text(text)
        key = str(p.resolve())
        if posted.get(key) == digest:
            continue
        return p  # 未投稿/未更新が見つかったらそれを返す

    # すべて既投稿なら最新を返す（再投稿にならないが下書き更新には使える）
    return candidates[0] if candidates else None

def decode_or_keep_state_file() -> Optional[Path]:
    """NOTE_STATE_B64 があれば state.json を復元して返す"""
    state_path = BASE / "state.json"
    if NOTE_STATE_B64:
        try:
            raw = base64.b64decode(NOTE_STATE_B64.encode("utf-8"))
            state_path.write_bytes(raw)
            return state_path
        except Exception:
            pass
    if state_path.exists():  # 以前保存したもの
        return state_path
    return None

# ===== Playwright 操作 =====
def playwright_post(title: str, content_md: str) -> str:
    """
    noteに投稿（デフォ下書き保存）
    戻り値: 作成/更新したノートのURL（判定できなければ空文字）
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        # ストレージステートの再利用
        state_path = decode_or_keep_state_file()
        if state_path:
            context = browser.new_context(storage_state=str(state_path))
        else:
            context = browser.new_context()

        # 全体のデフォルトタイムアウト
        context.set_default_timeout(15_000)
        page = context.new_page()
        page.set_default_timeout(15_000)

        # トレース
        context.tracing.start(screenshots=True, snapshots=True, sources=True)

        def snap(tag: str):
            page.screenshot(path=SS_DIR / f"{now_utc_stamp()}-{tag}.png", full_page=True)

        note_url = ""

        try:
            # ログイン状態でなければログイン
            page.goto("https://note.com/", wait_until="domcontentloaded")
            # ヘッダーの「投稿」ボタンがあればログイン済みと見なす
            logged_in = False
            try:
                page.get_by_role("button", name=re.compile("投稿|書く")).wait_for(timeout=4000)
                logged_in = True
            except PWTimeout:
                logged_in = False

            if not logged_in:
                page.goto("https://note.com/login", wait_until="domcontentloaded")
                # ラベルは日本語UI想定（英語UIでもaria-label/nameでだいたい拾える）
                page.get_by_label(re.compile("メール|Email", re.I)).fill(NOTE_EMAIL)
                page.get_by_label(re.compile("パスワード|Password", re.I)).fill(NOTE_PASSWORD)
                page.get_by_role("button", name=re.compile("ログイン|Sign in", re.I)).click()
                # 投稿ボタンが見えればログイン成功
                page.get_by_role("button", name=re.compile("投稿|書く")).wait_for(timeout=20_000)
                # ストレージ保存
                context.storage_state(path=str(BASE / "state.json"))

            # 新規作成画面へ（URL直叩きのほうが安定しやすい）
            page.goto("https://note.com/notes/new", wait_until="domcontentloaded")

            # タイトル入力（プレースホルダやaria-labelに反応）
            # 例）「タイトルを入力」「Enter a title」などを想定
            title_box = None
            try:
                title_box = page.get_by_placeholder(re.compile("タイトル|title", re.I))
                title_box.fill(title)
            except Exception:
                # 代替：最初のh1/textarea
                page.locator("h1[contenteditable], textarea, [data-testid='title']").first.fill(title)

            # 本文エリアへペースト
            # よくある: div[contenteditable="true"] が本文
            try:
                editor = page.locator("div[contenteditable='true']").first
                editor.click()
                page.keyboard.insert_text(content_md)
            except Exception:
                # 代替：textarea/editor汎用セレクタ
                page.locator("textarea, [role='textbox']").first.fill(content_md)

            snap("filled")

            if NOTE_PUBLISH:
                # 公開系ボタン（UI変化に強めな名称パターン）
                try:
                    page.get_by_role("button", name=re.compile("公開", re.I)).click()
                except Exception:
                    # メニュー→公開
                    try:
                        page.get_by_role("button", name=re.compile("設定|メニュー|…")).click()
                        page.get_by_role("menuitem", name=re.compile("公開", re.I)).click()
                    except Exception:
                        pass

                # 2段階目の「公開する」
                try:
                    page.get_by_role("button", name=re.compile("公開する|Publish", re.I)).click()
                except Exception:
                    pass
            else:
                # 下書き保存
                saved = False
                for sel in [
                    ("button", r"下書き保存"),
                    ("button", r"保存"),
                    ("button", r"Save draft"),
                ]:
                    try:
                        page.get_by_role(sel[0], name=re.compile(sel[1], re.I)).click(timeout=3000)
                        saved = True
                        break
                    except Exception:
                        continue
                if not saved:
                    # 代替：Ctrl/Cmd+S（効くUIもある）
                    with page.expect_event("dialog", timeout=4000) as maybe_dlg:
                        page.keyboard.press("ControlOrMeta+S")
                    try:
                        dlg = maybe_dlg.value
                        dlg.accept()
                    except Exception:
                        pass

            snap("after-submit")

            # 完了後のURLを拾う（エディタ→ノートURL遷移のとき）
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
                note_url = page.url
            except Exception:
                note_url = page.url

            return note_url

        except Exception:
            snap("error")
            raise
        finally:
            context.tracing.stop(path=str(TR_DIR / "trace.zip"))
            context.close()
            browser.close()

def main():
    if not NOTE_EMAIL or not NOTE_PASSWORD:
        print("ERROR: NOTE_EMAIL / NOTE_PASSWORD が未設定", file=sys.stderr)
        sys.exit(1)

    # 投稿対象の.mdを決定
    target = pick_target_file(NOTE_CHANNEL_ENV)
    if not target:
        print("INFO: 投稿対象の.mdが見つかりませんでした。", file=sys.stderr)
        sys.exit(0)

    raw = target.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    title, content = build_teaser(fm, body)

    print(f"Target: {target}")
    print(f"Title : {title}")

    # 投稿実行
    url = playwright_post(title=title, content_md=content)
    print(f"Result URL: {url}")

    # 成功扱いの判定はゆるく（URLがエディタでもOKにする）
    posted = load_posted_map()
    digest = sha1_text(raw)
    posted[str(target.resolve())] = digest
    save_posted_map(posted)

if __name__ == "__main__":
    try:
        main()
    except PWTimeout as e:
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
