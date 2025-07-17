#!/usr/bin/env python3
import json, os, datetime, pathlib
from openai import OpenAI

# ── パス設定 ──────────────────────────────
BASE      = pathlib.Path(__file__).resolve().parent.parent
POSTS_DIR = BASE / "posts" / "bonfillet"
IDEA_FILE = BASE / "data" / "ideas.json"

# ── 共通関数 ──────────────────────────────
def load_ideas() -> list[dict]:
    """ideas.json をロードして返す"""
    with IDEA_FILE.open(encoding="utf-8") as f:
        return json.load(f)

def generate_article(client: OpenAI, idea: dict) -> str:
    """OpenAI で本文を生成して返す"""
    system_prompt = (
        "あなたはマーケティング向けの編集者です。"
        "以下の制約で 1200〜1500 文字の記事を書いてください：\n"
        "・見出しは h2 (##) を 3〜4 個\n"
        "・最後に CTA を入れる\n"
        "・語調はフレンドリーだが専門性を示す\n"
    )
    user_prompt = f"タイトル: {idea['title']}\nお題: {idea['prompt']}"

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt}
        ],
        temperature=0.8
    )
    return resp.choices[0].message.content.strip()

# ── メイン処理 ─────────────────────────────
def main() -> None:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)  # 無ければ作成

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    today  = datetime.date.today().isoformat()

    for idea in load_ideas():
        slug    = idea["slug"]
        md_path = POSTS_DIR / f"{slug}.md"
        if md_path.exists():               # 重複防止
            continue

        body = generate_article(client, idea)

        frontmatter = (
            f"---\n"
            f'title: "{idea["title"]}"\n'
            f"date: {today}\n"
            f"slug: {slug}\n"
            f"tags: [bonfillet]\n"
            f"lang: ja\n"
            f"---\n\n"
        )
        md_path.write_text(frontmatter + body, encoding="utf-8")
        print(f"✅ generated {md_path.relative_to(BASE)}")

if __name__ == "__main__":
    main()
