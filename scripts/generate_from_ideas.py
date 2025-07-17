#!/usr/bin/env python3
import json, os, datetime, pathlib, openai, markdown
BASE = pathlib.Path(__file__).resolve().parents[1]
POSTS_DIR = BASE / "posts" / "bonfillet"
IDEA_FILE = BASE / "data" / "ideas.json"

def main():
    openai.api_key = os.environ["OPENAI_API_KEY"]
    ideas = json.loads(IDEA_FILE.read_text())

    for idea in ideas:
        slug = idea["slug"]
        md_path = POSTS_DIR / f"{slug}.md"
        if md_path.exists():           # すでに生成済みなら skip
            continue

        today = datetime.date.today().isoformat()
        system_prompt = (
            "あなたはマーケティング向けの編集者です。"
            "以下の制約で 1200〜1500 文字の記事を書いてください：\n"
            "・見出しは h2 (##) で 3〜4 個\n"
            "・最後に CTA を入れる\n"
            "・語調はフレンドリーだが専門性を示す\n"
        )
        user_prompt = f"タイトル: {idea['title']}\nお題: {idea['prompt']}"

        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt}
            ],
            temperature=0.8
        )
        body = resp.choices[0].message.content.strip()

        frontmatter = (
            f"---\n"
            f"title: \"{idea['title']}\"\n"
            f"date: {today}\n"
            f"slug: {slug}\n"
            f"tags: [bonfillet]\n"
            f"lang: ja\n"
            f"---\n\n"
        )
        md_path.write_text(frontmatter + body)
        print(f"✅ {slug}.md generated")

if __name__ == "__main__":
    main()
