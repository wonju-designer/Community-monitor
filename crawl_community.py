"""
아이즈모바일 커뮤니티 모니터링 — 주간 리포트
- 수집: 디시인사이드, 뽐뿌, 네이버 블로그/카페
- 분석: Groq (감성 분석 + 트렌드 비교)
- 발송: Gmail 자동 발송 (매주 월요일)
"""

import os
import re
import json
import asyncio
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

# ── 환경 변수 ──────────────────────────────────────────────
GROQ_API_KEY        = os.environ["GROQ_API_KEY"]
GMAIL_USER          = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
REPORT_TO           = os.environ["REPORT_TO"]
NAVER_CLIENT_ID     = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

KEYWORDS = ["아이즈모바일", "아이즈"]

# 제외 키워드 (제목에 포함되면 수집하지 않음)
EXCLUDE_KEYWORDS = [
    "아이즈원",        # 걸그룹
    "퍼스널아이즈",    # 라식 클리닉
    "라식", "라섹",    # 안과 시술
    "스마트아이즈",    # 다른 브랜드
    "프라이빗아이즈",  # 다른 브랜드
    "아이즈코리아",    # 다른 브랜드
]

def is_excluded(title: str) -> bool:
    """제외 키워드가 제목에 있는지 확인"""
    title_norm = title.replace(" ", "")
    return any(ex.replace(" ", "") in title_norm for ex in EXCLUDE_KEYWORDS)

WEEKLY_DATA_FILE = "weekly_data.json"

# ── 부정 판단 기준 ─────────────────────────────────────────
NEGATIVE_CRITERIA = """
부정글 판단 기준 (아래 중 하나라도 해당하면 부정):
1. 서비스 불만 직접 표현: 최악, 별로, 실망, 민원 제기, 도둑놈, 사기, 쓰레기
2. 해지/환불 문제: 환불 거부, 해지 안됨, 위약금 분쟁
3. 오류/장애 경험: 개통 오류, 데이터 안됨, 통화 불가, 앱 오류, 시스템 장애

제외 (부정 아님):
- 단순 정보 문의
- 중립적 사용 후기
- 요금제 비교 (우열 없는 단순 비교)
- 고객센터 전화번호 등 정보성 글
"""


# ── 날짜 유틸 ──────────────────────────────────────────────
def get_week_label():
    today = datetime.date.today()
    last_monday = today - datetime.timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + datetime.timedelta(days=6)
    week_num = (last_monday.day - 1) // 7 + 1
    return f"{last_monday.year}년 {last_monday.month}월 {week_num}주차 ({last_monday.month}/{last_monday.day} – {last_sunday.month}/{last_sunday.day})"

def get_prev_week_label():
    today = datetime.date.today()
    prev_monday = today - datetime.timedelta(days=today.weekday() + 14)
    prev_sunday = prev_monday + datetime.timedelta(days=6)
    week_num = (prev_monday.day - 1) // 7 + 1
    return f"{prev_monday.year}년 {prev_monday.month}월 {week_num}주차"

def is_within_week(date_str: str) -> bool:
    if not date_str:
        return True
    today = datetime.date.today()
    since = today - datetime.timedelta(days=7)
    formats = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
        "%Y.%m.%d %H:%M:%S", "%Y.%m.%d",
        "%y/%m/%d", "%m/%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(date_str.strip()[:19], fmt)
            if fmt == "%m/%d":
                parsed = parsed.replace(year=today.year)
            return since <= parsed.date() <= today
        except ValueError:
            continue
    return True

def parse_date_for_sort(date_str: str) -> str:
    if not date_str or date_str == "-":
        return "0000-00-00"
    return date_str.replace(".", "-")[:10]


# ── 주차별 데이터 저장/로드 ────────────────────────────────
def load_weekly_data() -> dict:
    try:
        if Path(WEEKLY_DATA_FILE).exists():
            with open(WEEKLY_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_weekly_data(week_label: str, stats: dict):
    data = load_weekly_data()
    data[week_label] = stats
    # 최근 8주만 유지
    if len(data) > 8:
        oldest = sorted(data.keys())[0]
        del data[oldest]
    try:
        with open(WEEKLY_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[데이터 저장] 오류: {e}")


# ── 디시인사이드 수집 ──────────────────────────────────────
async def crawl_dcinside(page, keyword: str) -> list[dict]:
    results = []
    try:
        encoded = quote(keyword)
        url = f"https://gall.dcinside.com/mgallery/board/lists?id=mvnogallery&s_type=search_subject_memo&s_keyword={encoded}"
        print(f"    [디시] '{keyword}' 검색 중...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        rows = await page.query_selector_all("tr.ub-content")
        if not rows:
            rows = await page.query_selector_all("tbody tr")

        for row in rows[:30]:
            try:
                notice = await row.get_attribute("class") or ""
                if "notice" in notice:
                    continue
                title_el = await row.query_selector("td.gall_tit a:first-child, .gall_tit a")
                if not title_el:
                    continue
                title = (await title_el.inner_text()).strip()
                if not title or len(title) < 2:
                    continue
                if not any(k in title for k in ["아이즈모바일", "아이즈"]):
                    continue
                # 제외 키워드 확인 (아이즈원, 퍼스널아이즈 등)
                if is_excluded(title):
                    print(f"    [디시] 제외: {title[:40]}")
                    continue
                href = await title_el.get_attribute("href") or ""
                full_url = f"https://gall.dcinside.com{href}" if href.startswith("/") else href
                date_el = await row.query_selector("td.gall_date, span.gall_date")
                date_text = (await date_el.get_attribute("title") or await date_el.inner_text()).strip() if date_el else ""
                if not is_within_week(date_text):
                    continue
                view_el = await row.query_selector("td.gall_count")
                view_text = (await view_el.inner_text()).strip() if view_el else "0"
                results.append({
                    "site": "디시인사이드(알뜰폰갤)",
                    "title": title, "url": full_url,
                    "date": date_text, "view_count": view_text,
                    "reply_count": "", "comments": [],
                })
            except Exception:
                continue

        print(f"    [디시] '{keyword}' 결과: {len(results)}건")

        def parse_view(v):
            try: return int(v.replace(",", "").strip())
            except: return 0

        for item in sorted(results, key=lambda x: parse_view(x["view_count"]), reverse=True)[:3]:
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                for c in await page.query_selector_all("p.usertxt.ub-word, .cmt_txtbox p"):
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"    [디시] 오류: {e}")
    return results


# ── 뽐뿌 수집 ─────────────────────────────────────────────
_ppomppu_done = False

async def crawl_ppomppu(page, keyword: str) -> list[dict]:
    global _ppomppu_done
    if _ppomppu_done:
        return []
    _ppomppu_done = True

    results = []
    seen_nos = set()

    try:
        for kw in KEYWORDS:
            encoded = quote(kw.encode("euc-kr"))
            url = f"https://www.ppomppu.co.kr/search_bbs.php?page_size=20&bbs_cate=2&keyword={encoded}&order_type=date&search_type=sub_memo"
            print(f"    [뽐뿌] '{kw}' 검색 중...")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            links = await page.query_selector_all("a[href]")
            print(f"    [뽐뿌] '{kw}' 링크 {len(links)}개")

            for link in links:
                try:
                    href = await link.get_attribute("href") or ""
                    if "view.php" not in href:
                        continue
                    no_match = re.search(r"no=(\d+)", href)
                    if not no_match:
                        continue
                    no = no_match.group(1)
                    if no in seen_nos:
                        continue
                    title = await page.evaluate("""el => {
                        const clone = el.cloneNode(true);
                        const font = clone.querySelector('font.comment-cnt');
                        if (font) font.remove();
                        return (clone.innerText || clone.textContent || '').trim();
                    }""", link)
                    if not title or len(title) < 5:
                        continue
                    title_clean = title.replace("[","").replace("]","")
                    if not any(k in title_clean for k in KEYWORDS):
                        continue
                    # 제외 키워드 확인 (퍼스널아이즈, 아이즈원 등)
                    if is_excluded(title):
                        print(f"    [뽐뿌] 제외: {title[:40]}")
                        continue
                    seen_nos.add(no)
                    full_url = f"https://www.ppomppu.co.kr{href}" if href.startswith("/") else href
                    full_url = re.sub(r"&keyword=[^&]*", "", full_url)
                    parent = await link.evaluate_handle(
                        "el => el.closest('li') || el.closest('tr') || el.closest('div') || el.parentElement"
                    )
                    parent_text = await page.evaluate("el => el.innerText || el.textContent || ''", parent)
                    date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", parent_text)
                    view_match = re.search(r"조회수[:\s]*(\d+)", parent_text)
                    reply_el = await link.query_selector("font.comment-cnt")
                    date_text = date_match.group(1) if date_match else ""
                    view_text = view_match.group(1) if view_match else "0"
                    reply_text = (await reply_el.inner_text()).strip() if reply_el else "0"
                    if not is_within_week(date_text):
                        continue
                    results.append({
                        "site": "뽐뿌", "title": title,
                        "url": full_url, "date": date_text,
                        "view_count": view_text, "reply_count": reply_text,
                        "comments": [],
                    })
                    print(f"    [뽐뿌] 수집: {title[:40]}...")
                except Exception:
                    continue
            await asyncio.sleep(1)

        print(f"    [뽐뿌] 최종: {len(results)}건")

        for item in results[:3]:
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                for c in await page.query_selector_all("td.comment_contents, .comment_text"):
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"    [뽐뿌] 오류: {e}")
    return results


# ── 네이버 수집 ────────────────────────────────────────────
async def crawl_naver() -> dict:
    results = {"blog": [], "cafe": []}
    apis = {
        "blog": "https://openapi.naver.com/v1/search/blog.json",
        "cafe": "https://openapi.naver.com/v1/search/cafearticle.json",
    }
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    since = datetime.date.today() - datetime.timedelta(days=7)

    async with httpx.AsyncClient(timeout=15) as client:
        for api_type, url in apis.items():
            try:
                resp = await client.get(url, headers=headers, params={
                    "query": "아이즈모바일", "display": 30, "sort": "date",
                })
                if resp.status_code != 200:
                    continue
                for item in resp.json().get("items", []):
                    pub_date = item.get("pubDate", "") or item.get("postdate", "")
                    try:
                        if "," in pub_date:
                            parsed = datetime.datetime.strptime(pub_date.strip(), "%a, %d %b %Y %H:%M:%S %z")
                            item_date = parsed.date()
                        elif len(pub_date) == 8:
                            item_date = datetime.datetime.strptime(pub_date, "%Y%m%d").date()
                        else:
                            item_date = datetime.date.today()
                    except Exception:
                        item_date = datetime.date.today()
                    if item_date < since:
                        continue
                    title = re.sub(r'<[^>]+>', '', item.get("title", "")).strip()
                    desc  = re.sub(r'<[^>]+>', '', item.get("description", "")).strip()
                    if "아이즈모바일" not in title.replace(" ", ""):
                        continue
                    if is_excluded(title):
                        continue
                    results[api_type].append({
                        "title": title, "description": desc[:200],
                        "url": item.get("link", "") or item.get("url", ""),
                        "date": str(item_date),
                        "source": item.get("bloggername", "") or item.get("cafename", ""),
                    })
                print(f"    [네이버 {api_type}] 수집: {len(results[api_type])}건")
                for item in results[api_type]:
                    print(f"      - [{item['date']}] {item['title']}")
            except Exception as e:
                print(f"    [네이버 {api_type}] 오류: {e}")
    return results


# ── Groq: 네이버 부정글 분류 ──────────────────────────────
async def classify_naver_negative(naver_data: dict) -> dict:
    filtered = {"blog": [], "cafe": []}
    type_names = {"blog": "블로그", "cafe": "카페"}

    for api_type, items in naver_data.items():
        if not items:
            continue
        type_name = type_names[api_type]

        prompt = f"""아래 '아이즈모바일' 관련 네이버 {type_name} 글 목록입니다.

{NEGATIVE_CRITERIA}

글 목록:
"""
        for i, item in enumerate(items):
            prompt += f"{i}. 제목: {item['title']}\n   내용: {item.get('description','')[:100]}\n"
        prompt += '\nJSON만 응답: {"negative_indices": [0기반 인덱스 번호들]}'

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 200, "temperature": 0.1},
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    match = re.search(r'\{.*\}', text, re.DOTALL)
                    if match:
                        indices = json.loads(match.group()).get("negative_indices", [])
                        for idx in indices:
                            if isinstance(idx, int) and 0 <= idx < len(items):
                                filtered[api_type].append(items[idx])
            print(f"    [Groq] 네이버 {type_name} 부정: {len(filtered[api_type])}건")
        except Exception as e:
            print(f"    [Groq] 분류 오류: {e}")

    return filtered


# ── Groq: 주간 리포트 생성 ────────────────────────────────
async def generate_weekly_report(
    all_posts: list[dict],
    week_label: str,
    naver_negative: dict,
    prev_stats: dict,
    current_stats: dict,
) -> str:

    site_summary = {}
    for post in all_posts:
        site = post["site"]
        if site not in site_summary:
            site_summary[site] = []
        site_summary[site].append(post)

    # 트렌드 비교 텍스트 생성
    trend_text = ""
    if prev_stats:
        prev_total = prev_stats.get("total", 0)
        prev_pos   = prev_stats.get("positive", 0)
        prev_neg   = prev_stats.get("negative", 0)
        prev_neu   = prev_stats.get("neutral", 0)
        curr_total = current_stats.get("total", 0)
        curr_pos   = current_stats.get("positive", 0)
        curr_neg   = current_stats.get("negative", 0)
        curr_neu   = current_stats.get("neutral", 0)

        def diff_str(curr, prev):
            d = curr - prev
            if d > 0:
                return f"{curr}건 (지난주 {prev}건 → +{d}건 증가)"
            elif d < 0:
                return f"{curr}건 (지난주 {prev}건 → {d}건 감소)"
            else:
                return f"{curr}건 (지난주 {prev}건 → 동일)"

        trend_text = f"""
지난 주 ({prev_stats.get('week', '이전 주')}) 통계:
- 총 언급: {prev_total}건
- 긍정: {prev_pos}건 / 부정: {prev_neg}건 / 중립: {prev_neu}건
- 주요 이슈: {prev_stats.get('main_issue', '없음')}

이번 주 ({week_label}) 통계:
- 총 언급: {diff_str(curr_total, prev_total)}
- 긍정: {diff_str(curr_pos, prev_pos)}
- 부정: {diff_str(curr_neg, prev_neg)}
- 중립: {diff_str(curr_neu, prev_neu)}
"""

    prompt = f"""당신은 브랜드 평판 분석 전문가입니다.
아래는 이번 주 커뮤니티에서 수집된 '아이즈모바일' 관련 게시글 데이터입니다.

기간: {week_label}
총 수집: {len(all_posts)}건

"""
    for site, posts in site_summary.items():
        prompt += f"\n## {site} ({len(posts)}건)\n"
        for i, p in enumerate(posts, 1):
            prompt += f"{i}. [{p['date']}] {p['title']} (조회 {p['view_count']})\n"
            if p.get("comments"):
                for c in p["comments"][:2]:
                    prompt += f"   댓글: {c}\n"

    naver_total = sum(len(v) for v in naver_negative.values())
    if naver_total > 0:
        prompt += f"\n\n## 네이버 부정 언급 ({naver_total}건)\n"
        for api_type, items in naver_negative.items():
            if items:
                type_name = {"blog": "블로그", "cafe": "카페"}[api_type]
                prompt += f"### {type_name}\n"
                for item in items:
                    prompt += f"- [{item['date']}] {item['title']}\n"

    if trend_text:
        prompt += f"\n\n## 주차별 트렌드\n{trend_text}"

    prompt += f"""

아래 형식으로 리포트를 작성해주세요.
각 항목은 반드시 새 줄에서 시작하고 앞뒤로 빈 줄을 넣어주세요.

## 1. 이번 주 핵심 요약
(3줄 이내)

## 2. 사이트별 언급 현황
- 디시인사이드: X건 (주요 토픽)
- 뽐뿌: X건 (주요 토픽)

## 3. 감성 분석
- 긍정: X건 (주요 내용)
- 부정: X건 (주요 내용)
- 중립: X건
- 감성 점수: X/10

## 4. 주요 이슈 및 키워드
- 이슈1
- 이슈2

## 5. 주차별 트렌드
(반드시 위 "주차별 트렌드" 데이터의 구체적인 숫자를 그대로 인용할 것. "증가/감소"만 쓰지 말고 "X건에서 Y건으로 +Z건 증가" 형태로 작성)
- 총 언급: 지난주 X건 → 이번주 Y건 (+Z건 증가/감소)
- 긍정 언급: 지난주 X건 → 이번주 Y건 (+Z건 증가/감소)
- 부정 언급: 지난주 X건 → 이번주 Y건 (+Z건 증가/감소)
- 중립 언급: 지난주 X건 → 이번주 Y건 (+Z건 증가/감소)
- 새로 등장한 이슈 (있을 경우)

## 6. 네이버 부정 언급 현황
- 블로그 부정글: X건 (주요 내용)
- 카페 부정글: X건 (주요 내용)

한국어로 간결하게 작성해주세요.

⚠️ 매우 중요 - 다음 규칙을 반드시 지키세요:
1. 게시글에 적힌 내용만 그대로 보고하세요. 추측, 추론, 확장 해석 금지.
2. 사용자가 쓰지 않은 단어를 만들어내지 마세요.
   예) "고객센터 연결 안됨" → "고객센터 연결 지연 불만" (O)
       "고객센터 연결 안됨" → "고객센터 폐지" (X, 추측 금지)
3. 대응 제언, 권고사항, 추천사항, 대응방안 등은 절대 포함하지 마세요.
4. "~필요함", "~해야 함", "~검토 필요" 같은 행동 권고 표현 사용 금지.
5. 회사 정책이나 운영 현황을 추측해서 쓰지 마세요. 사실만 보고하세요.
6. 게시글 원문에 없는 원인 분석을 하지 마세요. 사용자 발언만 인용하세요."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 2000, "temperature": 0.3},
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[Groq] 리포트 생성 오류: {e}")
    return "리포트 생성 실패"


# ── Groq: 감성 통계 집계 ──────────────────────────────────
async def get_sentiment_stats(all_posts: list[dict]) -> dict:
    if not all_posts:
        return {"positive": 0, "negative": 0, "neutral": 0, "main_issue": ""}

    titles = [p["title"] for p in all_posts[:20]]
    prompt = f"""아래 '아이즈모바일' 관련 게시글 제목들의 감성을 분류해주세요.

{NEGATIVE_CRITERIA}

게시글:
{chr(10).join(f"{i}. {t}" for i, t in enumerate(titles))}

JSON만 응답:
{{"positive": 긍정수, "negative": 부정수, "neutral": 중립수, "main_issue": "주요 이슈 한 줄"}}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 200, "temperature": 0.1},
            )
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"]
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    return json.loads(match.group())
    except Exception as e:
        print(f"[Groq] 통계 오류: {e}")
    return {"positive": 0, "negative": 0, "neutral": 0, "main_issue": ""}


# ── 이메일 HTML 생성 ───────────────────────────────────────
def format_report_html(report: str) -> str:
    lines = report.split("\n")
    html_parts = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        elif line.startswith("## "):
            title = line.replace("## ", "")
            html_parts.append(
                f'<div style="margin-top:24px; padding:10px 14px; background:#f0f4ff; '
                f'border-left:4px solid #1a73e8; border-radius:4px;">'
                f'<strong style="font-size:14px; color:#1a73e8;">{title}</strong></div>'
            )
        elif line.startswith("- "):
            text = line[2:]
            html_parts.append(f'<div style="padding:4px 14px 4px 28px; color:#444; font-size:13px;">• {text}</div>')
        else:
            html_parts.append(f'<div style="padding:4px 14px; color:#333; font-size:13px;">{line}</div>')
    return "\n".join(html_parts)


def build_community_table(all_posts: list[dict]) -> str:
    site_groups = {}
    for post in all_posts:
        site = post["site"]
        if site not in site_groups:
            site_groups[site] = []
        site_groups[site].append(post)

    html = ""
    for site, posts in site_groups.items():
        posts_sorted = sorted(posts, key=lambda x: parse_date_for_sort(x["date"]), reverse=True)
        html += f"""
<h3 style="font-size:14px; font-weight:500; margin:24px 0 8px; color:#111;
  border-left:3px solid #1a73e8; padding-left:8px;">
  {site} <span style="font-size:12px; color:#999;">({len(posts)}건)</span>
</h3>
<table style="width:100%; border-collapse:collapse; font-size:12px;">
  <thead><tr style="background:#f8f8f8;">
    <th style="text-align:left; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:65%;">제목</th>
    <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:20%;">날짜</th>
    <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:15%;">조회</th>
  </tr></thead><tbody>"""
        for post in posts_sorted:
            title_html = f'<a href="{post["url"]}" style="color:#1a73e8; text-decoration:none;">{post["title"]}</a>' if post.get("url") else post["title"]
            html += f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:7px 10px;">{title_html}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{post["date"]}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{post["view_count"]}</td>
    </tr>"""
        html += "</tbody></table>"
    return html


def build_naver_table(naver_data: dict, color: str = "#34a853", label: str = "네이버") -> str:
    type_names = {"blog": "블로그", "cafe": "카페"}
    html = ""
    for api_type, items in naver_data.items():
        if not items:
            continue
        type_name = type_names[api_type]
        html += f"""
<h3 style="font-size:14px; font-weight:500; margin:24px 0 8px; color:#111;
  border-left:3px solid {color}; padding-left:8px;">
  {label} {type_name} <span style="font-size:12px; color:#999;">({len(items)}건)</span>
</h3>
<table style="width:100%; border-collapse:collapse; font-size:12px;">
  <thead><tr style="background:#f8f8f8;">
    <th style="text-align:left; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:55%;">제목</th>
    <th style="text-align:left; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:25%;">출처</th>
    <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:15%;">날짜</th>
  </tr></thead><tbody>"""
        for item in items:
            title_html = f'<a href="{item["url"]}" style="color:{color}; text-decoration:none;">{item["title"]}</a>' if item.get("url") else item["title"]
            html += f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:7px 10px;">{title_html}</td>
      <td style="padding:7px 10px; color:#666; font-size:11px;">{item.get("source","")[:30]}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{item.get("date","")}</td>
    </tr>"""
        html += "</tbody></table>"
    return html


def send_weekly_email(subject: str, report: str, all_posts: list[dict], naver_data: dict, naver_negative: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"]   = REPORT_TO

    report_html     = format_report_html(report)
    community_html  = build_community_table(all_posts)
    naver_all_html  = build_naver_table(naver_data, "#34a853", "네이버")
    naver_neg_html  = build_naver_table(naver_negative, "#e53e3e", "네이버 부정글")
    naver_total     = sum(len(v) for v in naver_data.values())
    naver_neg_total = sum(len(v) for v in naver_negative.values())

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:720px; margin:0 auto; padding:24px;">
  <h2 style="font-size:18px; font-weight:500; border-bottom:1px solid #eee; padding-bottom:12px;">{subject}</h2>
  <p style="font-size:12px; color:#999; margin-bottom:20px;">
    커뮤니티 {len(all_posts)}건 · 네이버 {naver_total}건 수집 (부정 {naver_neg_total}건)
  </p>
  <div style="font-size:14px; line-height:1.8; border:1px solid #eee; border-radius:8px; padding:8px 0; margin-bottom:24px;">
    {report_html}
  </div>
  <h2 style="font-size:16px; font-weight:500; margin-top:36px; margin-bottom:4px;
    border-bottom:1px solid #eee; padding-bottom:10px;">수집 게시글 목록</h2>
  {community_html}
  {naver_all_html}
  {('<h2 style="font-size:16px; font-weight:500; margin-top:36px; margin-bottom:4px; border-bottom:1px solid #eee; padding-bottom:10px;">네이버 부정 언급 목록</h2>' + naver_neg_html) if naver_neg_html.strip() else ""}
  <hr style="border:none; border-top:1px solid #eee; margin-top:32px;">
  <p style="font-size:11px; color:#bbb;">아이즈모바일 · 커뮤니티 모니터링 AI 에이전트 · 매주 월요일 자동 발송</p>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"[Gmail] 정기 리포트 발송 완료 → {REPORT_TO}")


# ── 메인 ───────────────────────────────────────────────────
async def main():
    global _ppomppu_done
    _ppomppu_done = False

    week_label = get_week_label()
    print(f"[시작] {week_label} 주간 리포트")

    # ① 커뮤니티 수집
    all_posts = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}, locale="ko-KR",
        )
        page = await context.new_page()

        for keyword in KEYWORDS:
            print(f"\n[검색] 키워드: {keyword}")
            all_posts.extend(await crawl_dcinside(page, keyword))
            await asyncio.sleep(2)
            all_posts.extend(await crawl_ppomppu(page, keyword))
            await asyncio.sleep(2)

        await browser.close()

    # 중복 제거 (URL의 쿼리스트링 무시 + 제목 정규화)
    def dedup_key(post):
        url = post.get("url", "")
        if url:
            # 디시: ?id=mvnogallery&no=12345 형태에서 no= 값만 추출
            no_match = re.search(r"[?&]no=(\d+)", url)
            if no_match:
                return f"dci:{no_match.group(1)}"
            # 뽐뿌도 동일
            no_match2 = re.search(r"no=(\d+)", url)
            if no_match2:
                return f"pp:{no_match2.group(1)}"
            return url.split("?")[0]
        return post.get("title", "")

    seen = set()
    unique_posts = []
    for post in all_posts:
        key = dedup_key(post)
        if key and key not in seen:
            seen.add(key)
            unique_posts.append(post)

    print(f"\n[수집 완료] 총 {len(unique_posts)}건")

    # ② 네이버 수집
    print("\n[네이버] 수집 중...")
    naver_data = await crawl_naver()

    # ③ 네이버 부정글 분류
    print("\n[Groq] 네이버 부정글 분류 중...")
    naver_negative = await classify_naver_negative(naver_data)

    # ④ 감성 통계 집계
    print("\n[Groq] 감성 통계 집계 중...")
    sentiment = await get_sentiment_stats(unique_posts)
    naver_neg_total = sum(len(v) for v in naver_negative.values())

    current_stats = {
        "week": week_label,
        "total": len(unique_posts),
        "positive": sentiment.get("positive", 0),
        "negative": sentiment.get("negative", 0) + naver_neg_total,
        "neutral": sentiment.get("neutral", 0),
        "main_issue": sentiment.get("main_issue", ""),
    }

    # ⑤ 지난 주 데이터 로드
    weekly_data = load_weekly_data()
    prev_week_label = get_prev_week_label()
    prev_stats = weekly_data.get(prev_week_label, {})
    print(f"[트렌드] 지난 주 데이터: {'있음' if prev_stats else '없음 (첫 실행)'}")

    # ⑥ 리포트 생성
    print("\n[Groq] 주간 리포트 생성 중...")
    report = await generate_weekly_report(unique_posts, week_label, naver_negative, prev_stats, current_stats)
    print(report)

    # ⑦ 이번 주 데이터 저장
    save_weekly_data(week_label, current_stats)
    print(f"\n[데이터] {week_label} 통계 저장 완료")

    # ⑧ 이메일 발송
    print("\n[발송] 주간 리포트 전송 중...")
    send_weekly_email(
        subject=f"[커뮤니티 모니터링] 아이즈모바일 {week_label}",
        report=report,
        all_posts=unique_posts,
        naver_data=naver_data,
        naver_negative=naver_negative,
    )
    print("[완료]")


if __name__ == "__main__":
    asyncio.run(main())
