#!/usr/bin/env python3
import json, os, datetime, pathlib
from openai import OpenAI

# ── パス設定 ──────────────────────────────
BASE      = pathlib.Path(__file__).resolve().parent.parent
POSTS_DIR = BASE / "posts" / "sabou"
IDEA_FILE = BASE / "data" / "ideas.json"

# ── 共通関数 ──────────────────────────────
def load_ideas() -> list[dict]:
    """ideas.json をロードして返す"""
    with IDEA_FILE.open(encoding="utf-8") as f:
        return json.load(f)

def generate_article(client: OpenAI, idea: dict) -> str:
    """OpenAI で本文を生成して返す"""
        system_prompt = (
            "あなたはチームマネジメントや育成に詳しい編集者です。\n"
            "以下のルールで、課題に共感しながら具体的な解決策を提案する記事を1200〜1500字で書いてください：\n"
            "・導入は読者の『あるある悩み』から入り、チームに共通する課題として提示する\n"
            "・本文は h2(##) 見出しを3〜4個使って構成し、それぞれに具体的な事例やポイントを含める\n"
            "・文体はフレンドリーだが、行動につながる専門性を感じさせる語り口で\n"
            "・最後に自然な形で読者に問いかけるCTA（行動喚起）を入れる\n"
            "・想定読者は、学生・社会人問わず、3人以上のチーム活動に関わっている人\n"
        )
        user_prompt = (
            f"記事タイトル: {idea['title']}\n"
            f"出発点となるお題・課題: {idea['prompt']}\n"
            "この課題に対して、共感を引き出しながら、チームで改善していく流れで記事化してください。"
        )

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
    today   = datetime.date.today().isoformat()

    for idea in load_ideas():
        slug    = idea["slug"]
        md_path = POSTS_DIR / f"{slug}.md"
        if md_path.exists():       # 重複防止
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
