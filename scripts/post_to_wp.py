#!/usr/bin/env python3
"""
posts/sabou/ 内の Markdown から未投稿 1 本を WordPress へ公開し、
アイキャッチとカテゴリを自動付与するスクリプト。

必須環境変数（GitHub Secrets 推奨）:
  WP_URL         : 例 https://sabou.jp   (末尾スラッシュ無し)
  WP_USER        : WP ユーザー名
  WP_APP_PASS    : Application Password
"""
import base64, json, os, random, glob, datetime
from pathlib import Path

import markdown, frontmatter, requests

# ──────────────────────────
# WP 接続情報
# ──────────────────────────
WP_URL      = os.getenv("WP_URL")
WP_USER     = os.getenv("WP_USER")
WP_APP_PASS = os.getenv("WP_APP_PASS")

# ──────────────────────────
# 投稿対象フォルダ & タクソノミー
# ──────────────────────────
POST_DIR = Path("posts/sabou")
CATEGORY_MAP = {
    "communication": 88,
    "momentum":      89,
    "ritual":        87,
    "vision":        86,
}
TAG_IDS = []  # 必要に応じて

# ──────────────────────────
# アイキャッチ画像 ID プール
# ──────────────────────────
MEDIA_IDS = [
    1949, 1946, 1942, 1929  # ←空き時間に追加していく
]

POOL_FILE = Path("tmp/media_pool.json")


def _load_pool() -> list[int]:
    if POOL_FILE.exists():
        return json.loads(POOL_FILE.read_text())
    return []


def _save_pool(pool: list[int]) -> None:
    POOL_FILE.parent.mkdir(exist_ok=True)
    POOL_FILE.write_text(json.dumps(pool))


def next_media_id() -> int:
    """MEDIA_IDS を 1 周するまで重複させない"""
    pool = _load_pool()
    if not pool:
        pool = MEDIA_IDS[:]
        random.shuffle(pool)
    media_id = pool.pop()
    _save_pool(pool)
    return media_id


# ──────────────────────────
# WordPress 連携ヘルパ
# ──────────────────────────
def _basic_auth() -> str:
    token = f"{WP_USER}:{WP_APP_PASS}"
    return base64.b64encode(token.encode()).decode()


HEADERS = {
    "Authorization": f"Basic {_basic_auth()}",
    "Content-Type":  "application/json",
}


def post_article(title: str, html: str, category_id: int, media_id: int) -> str:
    """記事を公開してアイキャッチを付与"""
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title":       title,
        "content":     html,
        "status":      "publish",          # draft にしたい場合はここを変更
        "categories":  [category_id],
        "tags":        TAG_IDS,
        "lang":        "ja",
        "date":        datetime.datetime.utcnow().isoformat(),
    }
    r = requests.post(url, headers=HEADERS, json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"❌ Post failed: {r.status_code}: {r.text}")
    post_id = r.json()["id"]
    print("✅ Posted:", r.json()["link"])

    # アイキャッチを紐付け
    r2 = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        headers=HEADERS,
        json={"featured_media": media_id},
    )
    msg = "📷 アイキャッチ追加" if r2.ok else "⚠️ アイキャッチ追加失敗"
    print(msg, r2.status_code)
    return r.json()["link"]


# ──────────────────────────
# メイン処理
# ──────────────────────────
def detect_category(slug: str) -> int:
    """slug からカテゴリ ID を推定（マッチしない場合は vision を既定）"""
    key = slug.split("-")[0].lower()
    return CATEGORY_MAP.get(key, CATEGORY_MAP["vision"])


def find_unsubmitted_md() -> tuple[Path, frontmatter.Post] | None:
    mds = sorted(POST_DIR.glob("*.md"), key=os.path.getmtime, reverse=True)
    for md in mds:
        post = frontmatter.load(md)
        if not post.get("submitted"):      # フラグ無ければ未投稿
            return md, post
    return None


def main() -> None:
    target = find_unsubmitted_md()
    if not target:
        print("❌ No articles to post.")
        return

    md_path, fm_post = target
    slug  = fm_post.get("slug") or md_path.stem
    title = fm_post.get("title") or slug
    html  = markdown.markdown(fm_post.content)

    category_id = detect_category(slug)
    media_id    = next_media_id()
    print("🎯 slug:", slug, "/ category_id:", category_id)
    print("🎲 selected featured_media ID:", media_id)

    link = post_article(title, html, category_id, media_id)

    # フロントマターに submitted フラグを追加
    fm_post["submitted"] = True
    updated = frontmatter.dumps(fm_post) 
    md_path.write_text(updated, encoding="utf-8")
    print("📝 frontmatter updated:", md_path)
    print("🎉 All done →", link)


if __name__ == "__main__":
    main()
