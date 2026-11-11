#!/usr/bin/env python3
"""GeekNews RSS 관심 주제 다이제스트 생성기 (누적 방식).

사용법:
  python3 digest.py                       # 새 후보 고르기 + 요약 + 누적 저장 + 페이지 생성
  python3 digest.py --candidates          # 오늘 아직 없는 새 후보만 출력 (요약 작성용)
  python3 digest.py --summaries s.json    # 직접 쓴 요약 파일 사용
  python3 digest.py --build-only          # 피드는 건드리지 않고 정적 페이지만 다시 생성

저장 구조 (digest/ 폴더):
  data/YYYY-MM-DD.json   날짜별 누적 데이터 (실행할 때마다 새 기사만 덧붙임, 중복 id 제외)
  YYYY-MM-DD.md          누적 데이터로 다시 만든 마크다운
  index.html             모든 날짜를 보여 주는 정적 페이지

요약 방식:
  1) ANTHROPIC_API_KEY 가 있으면 Claude API 로 피드의 제목·요약을 다시 써서 요약
  2) 없으면 --summaries 로 넘긴 JSON 파일({"기사 id": {"summary": "..."}, "_overview": [...]})을 사용
  기사 본문 페이지는 절대 열지 않고, 피드에 실린 제목/요약만 사용한다.
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = "https://news.hada.io/rss/news"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIGEST_DIR = os.path.join(BASE_DIR, "digest")
DATA_DIR = os.path.join(DIGEST_DIR, "data")
MAX_ITEMS = 5  # 한 번 실행에서 새로 추가하는 최대 기사 수
MODEL = "claude-sonnet-5-5"

# 관심 주제: AI 에이전트, 업무 자동화, 클로드
KEYWORDS = {
    "에이전트": 3, "agent": 3, "mcp": 2,
    "자동화": 3, "automation": 3, "워크플로": 2, "회의록": 2, "업무": 1,
    "클로드": 3, "claude": 3, "anthropic": 2,
}
MIN_SCORE = 2
NS = {"a": "http://www.w3.org/2005/Atom"}


# ---------- 피드 ----------
def fetch_feed():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "rss-digest/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for e in root.findall("a:entry", NS):
        eid = e.findtext("a:id", default="", namespaces=NS)
        items.append({
            "id": eid.split("id=")[-1],
            "title": clean(e.findtext("a:title", default="", namespaces=NS)),
            "link": eid,
            "summary": clean(e.findtext("a:content", default="", namespaces=NS))[:1200],
        })
    return items


def score(item):
    # 'User-Agent' 같은 HTTP 용어가 'agent'로 오인되지 않게 제거
    title, body = (re.sub(r"user[- ]agent", "", t.lower()) for t in (item["title"], item["summary"]))
    s = 0
    for kw, w in KEYWORDS.items():
        if kw in title:
            s += w * 2
        elif kw in body:
            s += w
    return s


def select(items, exclude_ids):
    scored = [(score(i), n, i) for n, i in enumerate(items) if i["id"] not in exclude_ids]
    picked = [(s, n, i) for s, n, i in scored if s >= MIN_SCORE]
    picked.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, _, i in picked[:MAX_ITEMS]]


# ---------- 요약 ----------
def summarize_with_api(items, existing):
    key = os.environ["ANTHROPIC_API_KEY"]
    payload = [{"id": i["id"], "title": i["title"], "feed_summary": i["summary"]} for i in items]
    earlier = [{"title": i["title"], "summary": i["summary"]} for i in existing]
    prompt = (
        "아래는 RSS 피드의 기사 제목과 피드 요약이다. 관심 주제는 'AI 에이전트, 업무 자동화, 클로드'.\n"
        "각 새 기사를 한국어 두 문장으로, 원문 문장을 길게 베끼지 말고 자기 말로 요약하라. "
        "그리고 오늘 앞서 정리한 기사까지 포함해 전체를 아우르는 '오늘의 세 줄 요약' 3줄을 써라.\n"
        'JSON만 출력: {"_overview": ["..","..",".."], "<id>": {"summary": "두 문장"}}\n\n'
        "오늘 앞서 정리한 기사: " + json.dumps(earlier, ensure_ascii=False) + "\n\n"
        "새 기사: " + json.dumps(payload, ensure_ascii=False)
    )
    body = json.dumps({
        "model": MODEL, "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = json.load(r)["content"][0]["text"]
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


# ---------- 누적 저장 ----------
def load_day(day):
    path = os.path.join(DATA_DIR, f"{day}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"date": day, "overview": [], "items": []}


def save_day(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, f"{data['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def render_md(data):
    if not data["items"]:
        return "해당 없음\n"
    lines = [f"# 관심 주제 다이제스트 — {data['date']}", "", "## 오늘의 세 줄 요약", ""]
    lines += [f"{n}. {s}" for n, s in enumerate(data["overview"][:3], 1)]
    lines.append("")
    for i in data["items"]:
        lines += [f"## {i['title']}", "", i["summary"], "", f"원문: {i['link']}", ""]
    return "\n".join(lines)


# ---------- 정적 페이지 ----------
# 단일 강조색(blue-500), cool grey 중성색, 큰 라운드, 1px 헤어라인, 평면 카드
PAGE_CSS = """
:root{
  --blue-500:#3182F6;--blue-50:#E8F3FF;
  --grey-900:#191F28;--grey-700:#4E5968;--grey-600:#6B7684;--grey-500:#8B95A1;
  --grey-200:#E5E8EB;--grey-100:#F2F4F6;--white:#FFFFFF;
  --bg:var(--grey-100);--surface:var(--white);--text:var(--grey-900);--text-2:var(--grey-700);
  --text-3:var(--grey-500);--line:var(--grey-200);--accent:var(--blue-500);--accent-weak:var(--blue-50);
  --chip-active-bg:var(--grey-900);--chip-active-fg:var(--white);
  --shadow-2:0 4px 12px rgba(2,32,71,.06),0 1px 2px rgba(2,32,71,.04);
  --ease:cubic-bezier(.22,.61,.36,1);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#17171C;--surface:#202027;--text:#FFFFFF;--text-2:#C3C3C6;--text-3:#7E7E87;
  --line:#3C3C47;--accent:#3485FA;--accent-weak:#202C4D;
  --chip-active-bg:#FFFFFF;--chip-active-fg:#17171C;--shadow-2:none;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:"Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;
  font-size:15px;line-height:1.5;letter-spacing:-.005em;-webkit-font-smoothing:antialiased}
main{max-width:640px;margin:0 auto;padding:48px 16px 80px}
.hero{padding:0 8px 24px}
.hero h1{margin:0 0 8px;font-size:28px;line-height:1.3;font-weight:700;letter-spacing:-.02em}
.hero p{margin:0;color:var(--text-2);font-size:15px}
.stats{display:flex;gap:8px;margin-top:16px;font-variant-numeric:tabular-nums}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:6px 14px;
  font-size:13px;font-weight:600;color:var(--text-2)}
.stat b{color:var(--text);font-weight:700}
nav{display:flex;gap:8px;overflow-x:auto;padding:0 8px 8px;margin-bottom:16px;scrollbar-width:none}
nav a{flex:none;height:34px;display:inline-flex;align-items:center;padding:0 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--surface);color:var(--text);text-decoration:none;
  font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;transition:background .2s var(--ease)}
nav a:first-child{background:var(--chip-active-bg);color:var(--chip-active-fg);border-color:transparent}
section.day{margin-bottom:40px;scroll-margin-top:16px}
.dayhead{display:flex;justify-content:space-between;align-items:center;padding:0 8px;margin-bottom:12px}
.dayhead h2{margin:0;font-size:20px;font-weight:700;letter-spacing:-.015em}
.dayhead h2 span{color:var(--text-3);font-weight:600;font-size:15px;margin-left:6px;font-variant-numeric:tabular-nums}
.textbtn{color:var(--accent);font-size:13px;font-weight:600;text-decoration:none;padding:10px 4px}
.overview{background:var(--surface);border-radius:24px;padding:24px;margin-bottom:12px}
.overview h3{margin:0 0 12px;font-size:17px;font-weight:600;letter-spacing:-.01em}
.overview ol{margin:0;padding:0;list-style:none;counter-reset:o}
.overview li{counter-increment:o;display:flex;gap:12px;padding:8px 0;color:var(--text-2)}
.overview li::before{content:counter(o);flex:none;width:22px;height:22px;border-radius:6px;
  background:var(--accent-weak);color:var(--accent);font-size:12px;font-weight:700;
  display:flex;align-items:center;justify-content:center;margin-top:1px}
.cards{display:flex;flex-direction:column;gap:12px}
a.card{display:block;position:relative;background:var(--surface);border-radius:20px;padding:20px 24px;
  color:inherit;text-decoration:none;border:1px solid transparent;
  transition:box-shadow .2s var(--ease),border-color .2s var(--ease)}
a.card:hover{box-shadow:var(--shadow-2);border-color:var(--line)}
a.card:active::after{content:"";position:absolute;inset:0;border-radius:inherit;background:rgba(0,0,0,.06)}
.meta{display:flex;align-items:center;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.tag{height:22px;display:inline-flex;align-items:center;padding:0 8px;border-radius:6px;
  font-size:12px;font-weight:600;background:var(--accent-weak);color:var(--accent)}
.tag.n{background:var(--bg);color:var(--text-2)}
.time{font-size:12px;color:var(--text-3);font-weight:500;font-variant-numeric:tabular-nums}
.card h4{margin:0 0 8px;font-size:18px;line-height:1.45;font-weight:600;letter-spacing:-.01em;word-break:keep-all}
.card p{margin:0 0 16px;color:var(--text-2);word-break:keep-all}
.more{display:flex;align-items:center;justify-content:space-between;padding-top:14px;border-top:1px solid var(--line);
  font-size:13px;font-weight:600;color:var(--text-3)}
.more svg{width:16px;height:16px;transition:transform .2s var(--ease)}
a.card:hover .more{color:var(--text)}
a.card:hover .more svg{transform:translateX(2px)}
.empty{background:var(--surface);border-radius:20px;padding:32px 24px;text-align:center;color:var(--text-3)}
@media (max-width:480px){main{padding-top:32px}.overview{padding:20px}a.card{padding:18px 20px}}
"""

TOPICS = [
    ("AI 에이전트", ("에이전트", "agent", "mcp")),
    ("업무 자동화", ("자동화", "automation", "워크플로", "회의록", "업무")),
    ("클로드", ("클로드", "claude", "anthropic")),
]
ARROW = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
         'stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>')


def topics_of(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return [name for name, kws in TOPICS if any(k in text for k in kws)]


def day_label(d):
    y, m, dd = d.split("-")
    wd = "월화수목금토일"[dt.date(int(y), int(m), int(dd)).weekday()]
    return f"{int(m)}월 {int(dd)}일 {wd}요일"


def build_site():
    days = sorted((os.path.basename(p)[:-5] for p in glob.glob(os.path.join(DATA_DIR, "*.json"))),
                  reverse=True)
    esc = html.escape
    total = 0
    nav = "".join(f'<a href="#d{d}">{int(d[5:7])}월 {int(d[8:])}일</a>' for d in days)
    sections = []
    for d in days:
        data = load_day(d)
        n = len(data["items"])
        total += n
        head = (f'<div class="dayhead"><h2>{day_label(d)}<span>{n}건</span></h2>'
                f'<a class="textbtn" href="{d}.md" download>마크다운 받기</a></div>')
        if not n:
            sections.append(f'<section class="day" id="d{d}">{head}'
                            f'<div class="empty">관심 주제에 맞는 소식이 없었어요</div></section>')
            continue
        ov = "".join(f"<li>{esc(s)}</li>" for s in data["overview"][:3])
        cards = []
        for i in data["items"]:
            tags = topics_of(i)
            tag_html = "".join(f'<span class="tag">{t}</span>' for t in tags[:1])
            tag_html += "".join(f'<span class="tag n">{t}</span>' for t in tags[1:])
            added = i.get("added_at", "")[11:16]
            cards.append(
                f'<a class="card" href="{esc(i["link"])}" target="_blank" rel="noopener">'
                f'<div class="meta">{tag_html}<span class="time">GeekNews'
                f'{" · " + added + " 추가" if added else ""}</span></div>'
                f'<h4>{esc(i["title"])}</h4><p>{esc(i["summary"])}</p>'
                f'<div class="more">원문 보기{ARROW}</div></a>')
        sections.append(f'<section class="day" id="d{d}">{head}'
                        f'<div class="overview"><h3>오늘의 세 줄 요약</h3><ol>{ov}</ol></div>'
                        f'<div class="cards">{"".join(cards)}</div></section>')
    body = "".join(sections) or '<div class="empty">아직 모은 소식이 없어요</div>'
    page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>관심 뉴스 다이제스트</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>{PAGE_CSS}</style></head>
<body><main>
<header class="hero">
<h1>관심 뉴스 다이제스트</h1>
<p>GeekNews에서 AI 에이전트, 업무 자동화, 클로드 소식만 골라 요약했어요.</p>
<div class="stats"><span class="stat"><b>{len(days)}</b>일 모음</span><span class="stat">기사 <b>{total}</b>건</span></div>
</header>
<nav>{nav}</nav>
{body}
</main></body></html>
"""
    with open(os.path.join(DIGEST_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


# ---------- 실행 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", action="store_true")
    ap.add_argument("--summaries")
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    os.makedirs(DIGEST_DIR, exist_ok=True)
    if args.build_only:
        build_site()
        print(f"페이지 생성: {os.path.join(DIGEST_DIR, 'index.html')}")
        return

    today = dt.date.today().isoformat()
    data = load_day(today)
    known = {i["id"] for i in data["items"]}
    new = select(parse(fetch_feed()), known)

    if args.candidates:
        print(json.dumps(new, ensure_ascii=False, indent=2))
        return

    if new:
        if args.summaries:
            with open(args.summaries, encoding="utf-8") as f:
                summaries = json.load(f)
        elif os.environ.get("ANTHROPIC_API_KEY"):
            summaries = summarize_with_api(new, data["items"])
        else:
            sys.exit("요약 수단이 없습니다: ANTHROPIC_API_KEY 를 설정하거나 --summaries 파일을 넘겨 주세요.")
        missing = [i["id"] for i in new if i["id"] not in summaries]
        if missing:
            sys.exit(f"요약이 빠진 기사 id: {missing}")
        now = dt.datetime.now().isoformat(timespec="seconds")
        for i in new:
            data["items"].append({"id": i["id"], "title": i["title"], "link": i["link"],
                                  "summary": summaries[i["id"]]["summary"], "added_at": now})
        if summaries.get("_overview"):
            data["overview"] = summaries["_overview"][:3]

    save_day(data)
    out = os.path.join(DIGEST_DIR, f"{today}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_md(data))
    build_site()
    print(f"저장: {out} / 새로 추가 {len(new)}건, 오늘 누적 {len(data['items'])}건")


if __name__ == "__main__":
    main()
