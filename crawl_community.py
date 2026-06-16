"""
아이즈모바일 커뮤니티 모니터링 AI 에이전트
- 수집: 디시인사이드, 뽐뿌, 네이버 블로그/카페
- 1차 판단: Groq (부정글 분류 + 심각도 판단)
- 2차 행동: Gemini (추가 수집 키워드 결정 + 대응 초안 작성)
- 심각도 높을 때: 긴급 알림 발송
- 매주 월요일: 정기 리포트 발송
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

import httpx
from playwright.async_api import async_playwright

# ── 환경 변수 ──────────────────────────────────────────────
GROQ_API_KEY        = os.environ["GROQ_API_KEY"]
GEMINI_API_KEY      = os.environ["GEMINI_API_KEY"]
GMAIL_USER          = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
REPORT_TO           = os.environ["REPORT_TO"]
NAVER_CLIENT_ID     = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

KEYWORDS = ["아이즈모바일", "아이즈"]

# ── 날짜 유틸 ──────────────────────────────────────────────
def get_week_label():
    today = datetime.date.today()
    last_monday = today - datetime.timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + datetime.timedelta(days=6)
    week_num = (last_monday.day - 1) // 7 + 1
    return f"{last_monday.year}년 {last_monday.month}월 {week_num}주차 ({last_monday.month}/{last_monday.day} – {last_sunday.month}/{last_sunday.day})"

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


# ── FM코리아 수집 ──────────────────────────────────────────
async def crawl_fmkorea(page, keyword: str) -> list[dict]:
    results = []
    try:
        encoded = quote(keyword)
        url = f"https://www.fmkorea.com/search.php?act=IS&is_keyword={encoded}&mid=home&where=document&page=1"
        print(f"    [FM코리아] '{keyword}' 검색 중...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        links = await page.query_selector_all("a[href]")
        seen_urls = set()
        for link in links:
            try:
                href = await link.get_attribute("href") or ""
                path = href.split("?")[0].strip("/")
                if not path.isdigit():
                    continue
                full_url = f"https://www.fmkorea.com/{path}"
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                title = await page.evaluate("el => (el.innerText || el.textContent || '').trim()", link)
                if not title or len(title) < 5:
                    continue
                parent = await link.evaluate_handle("el => el.closest('li') || el.closest('div') || el.parentElement")
                date_el = await parent.query_selector(".regdate, time, .date")
                view_el = await parent.query_selector(".readCount, .hit")
                date_text = (await date_el.inner_text()).strip() if date_el else ""
                view_text = (await view_el.inner_text()).strip() if view_el else "0"
                if date_text and not is_within_week(date_text):
                    continue
                results.append({
                    "site": "FM코리아", "title": title,
                    "url": full_url, "date": date_text,
                    "view_count": view_text, "reply_count": "0",
                    "comments": [],
                })
            except Exception:
                continue
        print(f"    [FM코리아] '{keyword}' 결과: {len(results)}건")
    except Exception as e:
        print(f"    [FM코리아] 오류: {e}")
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
                    title_norm = title.replace(" ", "")
                    if "아이즈모바일" not in title_norm:
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


# ── Groq: 부정글 분류 + 심각도 판단 ──────────────────────
async def groq_analyze(all_posts: list[dict], naver_data: dict) -> dict:
    """부정글 분류 + 심각도 판단"""

    # 네이버 부정글 분류
    naver_negative = {"blog": [], "cafe": []}
    type_names = {"blog": "블로그", "cafe": "카페"}

    for api_type, items in naver_data.items():
        if not items:
            continue
        type_name = type_names[api_type]
        prompt = f"""아래 '아이즈모바일' 관련 네이버 {type_name} 글 중 부정적인 내용의 글 인덱스를 반환하세요.
부정 기준: 불만, 오류, 환불, 안됨, 느림, 최악, 문제, 실망, 짜증, 해지, 비추

"""
        for i, item in enumerate(items):
            prompt += f"{i}. 제목: {item['title']}\n   내용: {item.get('description','')[:100]}\n"
        prompt += '\nJSON만 응답: {"negative_indices": [숫자들]}'

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
                                naver_negative[api_type].append(items[idx])
            print(f"    [Groq] 네이버 {type_name} 부정: {len(naver_negative[api_type])}건")
        except Exception as e:
            print(f"    [Groq] 오류: {e}")

    # 심각도 판단
    total_negative = sum(len(v) for v in naver_negative.values())
    all_titles = [p["title"] for p in all_posts]

    severity_prompt = f"""아래는 이번 주 '아이즈모바일' 관련 커뮤니티 게시글 {len(all_posts)}건과 네이버 부정글 {total_negative}건입니다.

커뮤니티 게시글 제목:
{chr(10).join(f"- {t}" for t in all_titles[:20])}

네이버 부정글:
{chr(10).join(f"- {item['title']}" for v in naver_negative.values() for item in v)}

아래 기준으로 심각도를 판단하세요:
- 높음: 특정 이슈 집중(환불/오류/서비스 불만 3건 이상), 또는 전반적 부정 여론 강함
- 보통: 부정 언급 있으나 특정 이슈 집중 아님
- 낮음: 부정 언급 거의 없음

JSON만 응답: {{"severity": "높음/보통/낮음", "main_issue": "주요 이슈 한 줄", "keywords": ["키워드1", "키워드2"]}}"""

    severity = "낮음"
    main_issue = ""
    extra_keywords = []

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": severity_prompt}], "max_tokens": 200, "temperature": 0.1},
            )
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"]
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    result = json.loads(match.group())
                    severity = result.get("severity", "낮음")
                    main_issue = result.get("main_issue", "")
                    extra_keywords = result.get("keywords", [])
        print(f"    [Groq] 심각도: {severity} / 주요이슈: {main_issue}")
    except Exception as e:
        print(f"    [Groq] 심각도 판단 오류: {e}")

    return {
        "naver_negative": naver_negative,
        "severity": severity,
        "main_issue": main_issue,
        "extra_keywords": extra_keywords,
    }


# ── Gemini: 추가 수집 키워드 결정 + 대응 초안 작성 ────────
async def gemini_action(severity: str, main_issue: str, extra_keywords: list, all_posts: list[dict]) -> dict:
    """심각도 높을 때만 실행"""
    if severity != "높음":
        return {"extra_posts": [], "response_draft": ""}

    print(f"\n[Gemini] 심각도 높음 감지 → 추가 수집 + 대응 초안 작성")

    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

    # 1. 대응 초안 작성
    draft_prompt = f"""당신은 아이즈모바일 고객 응대 전문가입니다.
이번 주 커뮤니티에서 아래 이슈가 집중 발생했습니다.

주요 이슈: {main_issue}
관련 키워드: {', '.join(extra_keywords)}

관련 게시글:
{chr(10).join(f"- {p['title']}" for p in all_posts[:10])}

아래 형식으로 대응 초안을 작성해주세요:

## 이슈 요약
(2줄 이내)

## 고객 응대 초안
(실제 고객에게 전달할 수 있는 공식 응대 문구)

## 내부 조치 권고
- 조치1
- 조치2
- 조치3

한국어로 작성해주세요."""

    response_draft = ""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(gemini_url, json={"contents": [{"parts": [{"text": draft_prompt}]}]})
            if resp.status_code == 200:
                response_draft = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                print(f"    [Gemini] 대응 초안 작성 완료")
            else:
                print(f"    [Gemini] 오류: {resp.status_code}")
    except Exception as e:
        print(f"    [Gemini] 오류: {e}")

    return {"response_draft": response_draft}


# ── Groq: 정기 리포트 생성 ────────────────────────────────
async def generate_report(all_posts: list[dict], week_label: str, naver_negative: dict, severity: str, main_issue: str) -> str:
    if not all_posts:
        return "이번 주 수집된 언급 데이터가 없습니다."

    site_summary = {}
    for post in all_posts:
        site = post["site"]
        if site not in site_summary:
            site_summary[site] = []
        site_summary[site].append(post)

    prompt = f"""당신은 브랜드 평판 분석 전문가입니다.
기간: {week_label} | 총 수집: {len(all_posts)}건 | 심각도: {severity}

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
                prompt += f"### {'블로그' if api_type == 'blog' else '카페'}\n"
                for item in items:
                    prompt += f"- [{item['date']}] {item['title']}\n"

    if main_issue:
        prompt += f"\n\n## 주요 이슈 (AI 감지)\n{main_issue}\n"

    prompt += """
아래 형식으로 리포트를 작성해주세요.

## 1. 이번 주 핵심 요약
(3줄 이내)

## 2. 사이트별 언급 현황
- 디시인사이드: X건 (주요 토픽)
- 뽐뿌: X건 (주요 토픽)

## 3. 감성 분석
- 긍정: X건
- 부정: X건
- 중립: X건
- 감성 점수: X/10

## 4. 주요 이슈 및 키워드
- 이슈1
- 이슈2

## 5. 네이버 부정 언급 현황
- 블로그 부정글: X건
- 카페 부정글: X건

## 6. 대응 제언
- 제언1
- 제언2

한국어로 간결하게 작성해주세요."""

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
        elif line.startswith("- ") or line.startswith("• "):
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


def build_naver_table(naver_data: dict, label: str = "네이버", color: str = "#34a853") -> str:
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


# ── 긴급 알림 이메일 ───────────────────────────────────────
def send_alert_email(subject: str, main_issue: str, response_draft: str, all_posts: list[dict], week_label: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚨 [긴급] 아이즈모바일 이슈 감지 — {week_label}"
    msg["From"] = GMAIL_USER
    msg["To"]   = REPORT_TO

    draft_html = format_report_html(response_draft)
    related = [p for p in all_posts if any(k in p["title"] for k in main_issue.split()[:2])][:5]
    related_html = "".join([
        f'<li style="margin:4px 0;"><a href="{p["url"]}" style="color:#c53030;">{p["title"]}</a> [{p["date"]}]</li>'
        for p in related
    ])

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:720px; margin:0 auto; padding:24px;">
  <div style="background:#c53030; color:white; padding:16px 20px; border-radius:8px; margin-bottom:24px;">
    <div style="font-size:18px; font-weight:500;">🚨 커뮤니티 이슈 급증 감지</div>
    <div style="font-size:12px; opacity:0.9; margin-top:4px;">{week_label} · 즉각 확인 필요</div>
  </div>
  <div style="background:#fff5f5; border-left:4px solid #e53e3e; padding:16px; border-radius:4px; margin-bottom:20px;">
    <strong style="color:#c53030;">주요 이슈:</strong> {main_issue}
  </div>
  <h3 style="font-size:15px; margin-bottom:8px;">관련 게시글</h3>
  <ul style="margin:0; padding-left:16px;">{related_html}</ul>
  <h3 style="font-size:15px; margin-top:24px; margin-bottom:8px;">대응 초안 (Gemini 작성)</h3>
  <div style="border:1px solid #eee; border-radius:8px; padding:8px 0;">
    {draft_html}
  </div>
  <hr style="border:none; border-top:1px solid #eee; margin-top:32px;">
  <p style="font-size:11px; color:#bbb;">아이즈모바일 · AI 에이전트 긴급 알림</p>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"[긴급 알림] 발송 완료 → {REPORT_TO}")


# ── 정기 리포트 이메일 ─────────────────────────────────────
def send_regular_email(subject: str, report: str, all_posts: list[dict], naver_data: dict, naver_negative: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"]   = REPORT_TO

    report_html    = format_report_html(report)
    community_html = build_community_table(all_posts)
    naver_all_html = build_naver_table(naver_data, "네이버", "#34a853")
    naver_neg_html = build_naver_table(naver_negative, "네이버 부정글", "#e53e3e")
    naver_total    = sum(len(v) for v in naver_data.values())
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
    print(f"[정기 리포트] 발송 완료 → {REPORT_TO}")


# ── 메인 ───────────────────────────────────────────────────
async def main():
    global _ppomppu_done
    _ppomppu_done = False

    week_label = get_week_label()
    print(f"[시작] {week_label} 커뮤니티 모니터링 AI 에이전트")

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
            all_posts.extend(await crawl_fmkorea(page, keyword))
            await asyncio.sleep(2)

        await browser.close()

    # 중복 제거
    seen = set()
    unique_posts = []
    for post in all_posts:
        key = post["url"] or post["title"]
        if key and key not in seen:
            seen.add(key)
            unique_posts.append(post)

    print(f"\n[수집 완료] 총 {len(unique_posts)}건")

    # ② 네이버 수집
    print("\n[네이버] 수집 중...")
    naver_data = await crawl_naver()

    # ③ Groq: 부정글 분류 + 심각도 판단
    print("\n[Groq] 분석 중...")
    analysis = await groq_analyze(unique_posts, naver_data)
    naver_negative = analysis["naver_negative"]
    severity       = analysis["severity"]
    main_issue     = analysis["main_issue"]
    extra_keywords = analysis["extra_keywords"]

    print(f"\n[판단] 심각도: {severity}")

    # ④ 심각도 높을 때 Gemini 행동
    gemini_result = await gemini_action(severity, main_issue, extra_keywords, unique_posts)

    # ⑤ 심각도 높을 때 긴급 알림 발송
    if severity == "높음" and gemini_result.get("response_draft"):
        print("\n[긴급 알림] 발송 중...")
        send_alert_email(
            subject=f"🚨 [긴급] 아이즈모바일 이슈 감지",
            main_issue=main_issue,
            response_draft=gemini_result["response_draft"],
            all_posts=unique_posts,
            week_label=week_label,
        )

    # ⑥ Groq: 정기 리포트 생성
    print("\n[분석] Groq 리포트 생성 중...")
    report = await generate_report(unique_posts, week_label, naver_negative, severity, main_issue)
    print(report)

    # ⑦ 정기 리포트 발송
    print("\n[발송] 정기 리포트 전송 중...")
    send_regular_email(
        subject=f"[커뮤니티 모니터링] 아이즈모바일 {week_label}",
        report=report,
        all_posts=unique_posts,
        naver_data=naver_data,
        naver_negative=naver_negative,
    )
    print("[완료]")


if __name__ == "__main__":
    asyncio.run(main())
