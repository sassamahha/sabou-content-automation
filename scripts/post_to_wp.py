#!/usr/bin/env python3
"""
posts/sabou/ 内の Markdown から“未投稿 1 本だけ”WordPress へ公開

"""

import base64, datetime, glob, json, os, random, sys
from pathlib import Path

import frontmatter, markdown, requests

# ────────────────────────── WordPress 接続情報
WP_URL      = os.getenv("WP_URL")
WP_USER     = os.getenv("WP_USER")
WP_APP_PASS = os.getenv("WP_APP_PASS")

if not all((WP_URL, WP_USER, WP_APP_PASS)):
    sys.exit("❌  WP_URL / WP_USER / WP_APP_PASS が未設定です")

# ────────────────────────── 投稿対象フォルダ & タクソノミー
POST_DIR = Path("posts/sabou")
CATEGORY_MAP = {
    "communication": 88,
    "momentum":      89,
    "ritual":        87,
    "vision":        86,
}
TAG_IDS: list[int] = []     # 必要なら設定

# ────────────────────────── アイキャッチ画像 ID プール
MEDIA_IDS = [1942, 1943, 1944, 1945]  # ←自サイトのメディア ID に置換
POOL_FILE = Path("tmp/media_pool.json")


def _load_pool() -> list[int]:
    return json.loads(POOL_FILE.read_text()) if POOL_FILE.exists() else []


def _save_pool(pool: list[int]) -> None:
    POOL_FILE.parent.mkdir(exist_ok=True)
    POOL_FILE.write_text(json.dumps(pool))


def next_media_id() -> int:
    """MEDIA_IDS を 1 周使い切るまで重複しない ID を返す"""
    pool = _load_pool()
    if not pool:
        pool = MEDIA_IDS[:]
        random.shuffle(pool)
    media_id = pool.pop()
    _save_pool(pool)
    return media_id


# ────────────────────────── WordPress REST ヘルパ
def _basic_auth() -> str:
    token = f"{WP_USER}:{WP_APP_PASS}"
    return base64.b64encode(token.encode()).decode()


HEADERS = {
    "Authorization": f"Basic {_basic_auth()}",
    "Content-Type":  "application/json",
}


def wp_post_exists(slug: str) -> bool:
    """同じ slug の WP 投稿が存在するか"""
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    r = requests.get(url, headers=HEADERS, params={"slug": slug, "status": "any"})
    return r.ok and len(r.json()) > 0


def post_article(title: str, html: str, category_id: int, media_id: int) -> str:
    """記事を公開し、アイキャッチを紐付けてリンクを返す"""
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title":      title,
        "content":    html,
        "status":     "publish",          # 必要なら draft / future
        "categories": [category_id],
        "tags":       TAG_IDS,
        "lang":       "ja",
        "date":       datetime.datetime.utcnow().isoformat(),
    }
    r = requests.post(url, headers=HEADERS, json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"❌ Post failed: {r.status_code}: {r.text}")

    post_id = r.json()["id"]
    print("✅ Posted:", r.json()["link"])

    # アイキャッチ付与
    r2 = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        headers=HEADERS,
        json={"featured_media": media_id},
    )
    msg = "📷 アイキャッチ追加" if r2.ok else "⚠️ アイキャッチ追加失敗"
    print(msg, r2.status_code)
    return r.json()["link"]


# ────────────────────────── Markdown 検出ユーティリティ
def detect_category(slug: str) -> int:
    """slug 先頭のキーワードでカテゴリ判定。見つからない時は vision"""
    key = slug.split("-")[0].lower()
    return CATEGORY_MAP.get(key, CATEGORY_MAP["vision"])


def find_unsubmitted_md() -> tuple[Path, frontmatter.Post] | None:
    """frontmatter に submitted フラグが無い最新 Markdown を 1 本返す"""
    for md_path in sorted(POST_DIR.glob("*.md"), key=os.path.getmtime, reverse=True):
        post = frontmatter.load(md_path)
        if not post.get("submitted"):
            return md_path, post
    return None


# ────────────────────────── メイン処理
def main() -> None:
    target = find_unsubmitted_md()
    if not target:
        print("❌ 未投稿の記事がありません")
        return

    md_path, fm_post = target
    slug   = fm_post.get("slug") or md_path.stem
    title  = fm_post.get("title") or slug
    html   = markdown.markdown(fm_post.content)
    cat_id = detect_category(slug)

    # WP 側に既に同 slug がある場合はスキップ
    if wp_post_exists(slug):
        print(f"🚫 Skip: slug '{slug}' already exists on WordPress")
        fm_post["submitted"] = True
        md_path.write_text(frontmatter.dumps(fm_post), encoding="utf-8")
        return

    media_id = next_media_id()
    print(f"🎯 slug: {slug} / category_id: {cat_id}")
    print("🎲 selected featured_media ID:", media_id)

    link = post_article(title, html, cat_id, media_id)

    # 成功したら submitted フラグを true に書き戻し
    fm_post["submitted"] = True
    md_path.write_text(frontmatter.dumps(fm_post), encoding="utf-8")
    print("📝 frontmatter updated:", md_path)
    print("🎉 All done →", link)


if __name__ == "__main__":
    main()
