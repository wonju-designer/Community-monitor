"""
커뮤니티 모니터링 - 아이즈모바일, 아이즈
- 디시인사이드 알뜰폰 마이너 갤러리
- 뽐뿌 커뮤니티 (bbs_cate=2, sub_memo 검색)
- FM코리아 검색
- 네이버 블로그/카페 부정글 수집
- 리포트 생성: Groq API (무료)
- 발송: Gmail SMTP
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


# ── 디시인사이드 ───────────────────────────────────────────
async def crawl_dcinside(page, keyword: str) -> list[dict]:
    results = []
    try:
        encoded = quote(keyword)
        url = f"https://gall.dcinside.com/mgallery/board/lists?id=mvnogallery&s_type=search_subject_memo&s_keyword={encoded}"
        print(f"    [디시] {keyword} 검색 중...")
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

                href = await title_el.get_attribute("href") or ""
                full_url = f"https://gall.dcinside.com{href}" if href.startswith("/") else href

                date_el = await row.query_selector("td.gall_date, span.gall_date")
                if date_el:
                    date_text = await date_el.get_attribute("title") or (await date_el.inner_text()).strip()
                else:
                    date_text = ""

                view_el   = await row.query_selector("td.gall_count")
                view_text = (await view_el.inner_text()).strip() if view_el else "0"
                reply_text = ""

                if not is_within_week(date_text):
                    continue

                keywords_check = ["아이즈모바일", "아이즈"]
                if not any(k in title for k in keywords_check):
                    continue

                results.append({
                    "site": "디시인사이드(알뜰폰갤)",
                    "title": title,
                    "url": full_url,
                    "date": date_text,
                    "reply_count": reply_text,
                    "view_count": view_text,
                    "comments": [],
                })
            except Exception:
                continue

        print(f"    [디시] '{keyword}' 결과: {len(results)}건")

        def parse_view(v):
            try:
                return int(v.replace(",", "").strip())
            except:
                return 0

        top3 = sorted(results, key=lambda x: parse_view(x["view_count"]), reverse=True)[:3]

        for item in top3:
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


# ── 뽐뿌 ──────────────────────────────────────────────────
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
            print(f"    [뽐뿌] '{kw}' 전체 링크 {len(links)}개")

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
                    parent_text = await page.evaluate(
                        "el => el.innerText || el.textContent || ''", parent
                    )

                    date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", parent_text)
                    view_match = re.search(r"조회수[:\s]*(\d+)", parent_text)
                    reply_el   = await link.query_selector("font.comment-cnt")

                    date_text  = date_match.group(1) if date_match else ""
                    view_text  = view_match.group(1) if view_match else "0"
                    reply_text = (await reply_el.inner_text()).strip() if reply_el else "0"

                    if not is_within_week(date_text):
                        continue

                    results.append({
                        "site": "뽐뿌",
                        "title": title,
                        "url": full_url,
                        "date": date_text,
                        "reply_count": reply_text,
                        "view_count": view_text,
                        "comments": [],
                    })
                    print(f"    [뽐뿌] 수집: {title[:40]}...")

                except Exception:
                    continue

            await asyncio.sleep(1)

        print(f"    [뽐뿌] 최종 결과: {len(results)}건")

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


# ── FM코리아 ───────────────────────────────────────────────
async def crawl_fmkorea(page, keyword: str) -> list[dict]:
    results = []
    try:
        encoded = quote(keyword)
        url = f"https://www.fmkorea.com/search.php?act=IS&is_keyword={encoded}&mid=home&where=document&page=1"
        print(f"    [FM코리아] '{keyword}' 검색 중...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        links = await page.query_selector_all("a[href]")
        print(f"    [FM코리아] 전체 링크 {len(links)}개")

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

                title = await page.evaluate("""el => {
                    return (el.innerText || el.textContent || '').trim();
                }""", link)
                if not title or len(title) < 5:
                    continue

                parent = await link.evaluate_handle(
                    "el => el.closest('li') || el.closest('div') || el.parentElement"
                )
                date_el  = await parent.query_selector(".regdate, time, .date")
                reply_el = await parent.query_selector(".replyCount, .comment_cnt")
                view_el  = await parent.query_selector(".readCount, .hit")

                date_text  = (await date_el.inner_text()).strip()  if date_el  else ""
                reply_text = (await reply_el.inner_text()).strip() if reply_el else "0"
                view_text  = (await view_el.inner_text()).strip()  if view_el  else "0"

                if date_text and not is_within_week(date_text):
                    continue

                results.append({
                    "site": "FM코리아",
                    "title": title,
                    "url": full_url,
                    "date": date_text,
                    "reply_count": reply_text,
                    "view_count": view_text,
                    "comments": [],
                })
            except Exception:
                continue

        print(f"    [FM코리아] '{keyword}' 결과: {len(results)}건")

        for item in results[:3]:
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                for c in await page.query_selector_all(".xe_content, .comment_content, .fdb_itm p"):
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"    [FM코리아] 오류: {e}")
    return results


# ── 네이버 수집 ────────────────────────────────────────────
async def crawl_naver() -> dict:
    """네이버 블로그/카페에서 아이즈모바일 수집 후 부정글만 반환"""
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
                    "query": "아이즈모바일",
                    "display": 30,
                    "sort": "date",
                })
                if resp.status_code != 200:
                    print(f"    [네이버 {api_type}] 오류: {resp.status_code}")
                    continue

                items = resp.json().get("items", [])
                for item in items:
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

                    if "아이즈모바일" not in (title + desc):
                        continue

                    results[api_type].append({
                        "title": title,
                        "description": desc[:200],
                        "url": item.get("link", "") or item.get("url", ""),
                        "date": str(item_date),
                        "source": item.get("bloggername", "") or item.get("cafename", ""),
                    })

                print(f"    [네이버 {api_type}] 수집: {len(results[api_type])}건")

            except Exception as e:
                print(f"    [네이버 {api_type}] 오류: {e}")

    return results


async def filter_negative_naver(naver_data: dict) -> dict:
    """Groq로 부정글만 추출"""
    filtered = {"blog": [], "cafe": []}
    type_names = {"blog": "블로그", "cafe": "카페"}

    for api_type, items in naver_data.items():
        if not items:
            continue
        type_name = type_names[api_type]

        prompt = f"아래 '아이즈모바일' 관련 {type_name} 글 중 부정적인 내용(불만, 오류, 환불, 안됨, 느림, 최악, 문제 등)의 글 인덱스를 반환하세요.\n\n"
        for i, item in enumerate(items):
            prompt += f"{i}. {item['title']} / {item.get('description','')[:80]}\n"
        prompt += '\nJSON 형식으로만 응답: {"negative_indices": [숫자들]}'

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.1,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    json_match = re.search(r'\{.*\}', text, re.DOTALL)
                    if json_match:
                        indices = json.loads(json_match.group()).get("negative_indices", [])
                        for idx in indices:
                            if isinstance(idx, int) and 0 <= idx < len(items):
                                filtered[api_type].append(items[idx])
            print(f"    [네이버 {type_name}] 전체 {len(items)}건 → 부정 {len(filtered[api_type])}건")
        except Exception as e:
            print(f"    [네이버 부정 분류] 오류: {e}")

    return filtered


# ── Groq 리포트 생성 ───────────────────────────────────────
async def generate_report(all_posts: list[dict], week_label: str, naver_negative: dict) -> str:
    if not all_posts:
        return "이번 주 수집된 언급 데이터가 없습니다."

    site_summary = {}
    for post in all_posts:
        site = post["site"]
        if site not in site_summary:
            site_summary[site] = []
        site_summary[site].append(post)

    prompt = f"""당신은 브랜드 평판 분석 전문가입니다.
아래는 이번 주 커뮤니티에서 수집된 '아이즈모바일' 관련 게시글 및 댓글 데이터입니다.

기간: {week_label}
총 수집: {len(all_posts)}건

"""
    for site, posts in site_summary.items():
        prompt += f"\n## {site} ({len(posts)}건)\n"
        for i, p in enumerate(posts, 1):
            prompt += f"{i}. [{p['date']}] {p['title']} (조회 {p['view_count']} | 댓글 {p['reply_count']})\n"
            if p.get("comments"):
                for c in p["comments"][:3]:
                    prompt += f"   댓글: {c}\n"

    naver_total = sum(len(v) for v in naver_negative.values())
    if naver_total > 0:
        prompt += f"\n\n## 네이버 부정 언급 ({naver_total}건)\n"
        for api_type, items in naver_negative.items():
            type_name = {"blog": "블로그", "cafe": "카페"}.get(api_type, api_type)
            if items:
                prompt += f"\n### {type_name}\n"
                for item in items:
                    prompt += f"- [{item['date']}] {item['title']}\n"

    prompt += """
아래 형식으로 리포트를 작성해주세요.
각 항목은 반드시 새 줄에서 시작하고, 항목 사이에 빈 줄을 넣어주세요.

## 1. 이번 주 핵심 요약
(3줄 이내, 각 줄은 줄바꿈으로 구분)

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
- 이슈3

## 5. 네이버 부정 언급 현황
- 블로그 부정글: X건 (주요 내용)
- 카페 부정글: X건 (주요 내용)

## 6. 대응 제언
- 제언1
- 제언2
- 제언3

한국어로 간결하게 작성해주세요."""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.3,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        if resp.status_code != 200:
            print(f"[Groq] 오류: {resp.status_code}")
            return f"리포트 생성 실패 (Groq 오류: {resp.status_code})"
        return resp.json()["choices"][0]["message"]["content"]


# ── Gmail 발송 ─────────────────────────────────────────────
def parse_date_for_sort(date_str: str) -> str:
    if not date_str or date_str == "-":
        return "0000-00-00"
    return date_str.replace(".", "-")[:10]


def build_table_html(all_posts: list[dict]) -> str:
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
<h3 style="font-size:14px; font-weight:500; margin:28px 0 8px; color:#111;
  border-left:3px solid #1a73e8; padding-left:8px;">
  {site} <span style="font-size:12px; color:#999; font-weight:400;">({len(posts)}건)</span>
</h3>
<table style="width:100%; border-collapse:collapse; font-size:12px;">
  <thead>
    <tr style="background:#f8f8f8;">
      <th style="text-align:left; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:60%;">제목</th>
      <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:20%;">날짜</th>
      <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:10%;">조회</th>
    </tr>
  </thead>
  <tbody>"""
        for post in posts_sorted:
            title_html = (
                f'<a href="{post["url"]}" style="color:#1a73e8; text-decoration:none;">{post["title"]}</a>'
                if post["url"] else post["title"]
            )
            html += f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:7px 10px;">{title_html}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{post["date"]}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{post["view_count"]}</td>
    </tr>"""
        html += "</tbody></table>"
    return html


def build_naver_table_html(naver_negative: dict) -> str:
    type_names = {"blog": "블로그", "cafe": "카페"}
    html = ""
    for api_type, items in naver_negative.items():
        if not items:
            continue
        type_name = type_names[api_type]
        html += f"""
<h3 style="font-size:14px; font-weight:500; margin:28px 0 8px; color:#111;
  border-left:3px solid #e53e3e; padding-left:8px;">
  네이버 {type_name} 부정글 <span style="font-size:12px; color:#999; font-weight:400;">({len(items)}건)</span>
</h3>
<table style="width:100%; border-collapse:collapse; font-size:12px;">
  <thead>
    <tr style="background:#f8f8f8;">
      <th style="text-align:left; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:55%;">제목</th>
      <th style="text-align:left; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:25%;">출처</th>
      <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:15%;">날짜</th>
    </tr>
  </thead>
  <tbody>"""
        for item in items:
            title_html = (
                f'<a href="{item["url"]}" style="color:#e53e3e; text-decoration:none;">{item["title"]}</a>'
                if item.get("url") else item["title"]
            )
            html += f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:7px 10px;">{title_html}</td>
      <td style="padding:7px 10px; color:#666; font-size:11px;">{item.get("source","")[:30]}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{item.get("date","")}</td>
    </tr>"""
        html += "</tbody></table>"
    return html


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
            text = line.replace("- ", "")
            html_parts.append(
                f'<div style="padding:4px 14px 4px 28px; color:#444; font-size:13px;">• {text}</div>'
            )
        else:
            html_parts.append(
                f'<div style="padding:4px 14px; color:#333; font-size:13px;">{line}</div>'
            )
    return "\n".join(html_parts)


def send_email(subject: str, report: str, all_posts: list[dict], naver_negative: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_TO

    tables_html      = build_table_html(all_posts)
    naver_html       = build_naver_table_html(naver_negative)
    report_html      = format_report_html(report)
    naver_total      = sum(len(v) for v in naver_negative.values())

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:720px; margin:0 auto; padding:24px;">
  <h2 style="font-size:18px; font-weight:500; border-bottom:1px solid #eee; padding-bottom:12px;">{subject}</h2>
  <p style="font-size:12px; color:#999; margin-bottom:20px;">
    커뮤니티 {len(all_posts)}건 · 네이버 부정글 {naver_total}건 수집
  </p>
  <div style="font-size:14px; line-height:1.8; border:1px solid #eee; border-radius:8px; padding:8px 0; margin-bottom:8px;">
    {report_html}
  </div>
  <h2 style="font-size:16px; font-weight:500; margin-top:36px; margin-bottom:4px;
    border-bottom:1px solid #eee; padding-bottom:10px;">수집 게시글 목록</h2>
  {tables_html}
  {('<h2 style="font-size:16px; font-weight:500; margin-top:36px; margin-bottom:4px; border-bottom:1px solid #eee; padding-bottom:10px;">네이버 부정 언급 목록</h2>' + naver_html) if naver_html.strip() else ""}
  <hr style="border:none; border-top:1px solid #eee; margin-top:32px;">
  <p style="font-size:11px; color:#bbb;">아이즈모바일 · 커뮤니티 모니터링 · 매주 월요일 자동 발송</p>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"[Gmail] 발송 완료 → {REPORT_TO}")


# ── 메인 ───────────────────────────────────────────────────
async def main():
    global _ppomppu_done
    _ppomppu_done = False

    week_label = get_week_label()
    print(f"[시작] {week_label} 커뮤니티 모니터링")

    all_posts = []

    # ── 커뮤니티 수집 (기존 코드 그대로) ──────────────────
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
        )
        page = await context.new_page()

        for keyword in KEYWORDS:
            print(f"\n[검색] 키워드: {keyword}")

            dcinside = await crawl_dcinside(page, keyword)
            all_posts.extend(dcinside)
            await asyncio.sleep(2)

            ppomppu = await crawl_ppomppu(page, keyword)
            all_posts.extend(ppomppu)
            await asyncio.sleep(2)

            fmkorea = await crawl_fmkorea(page, keyword)
            all_posts.extend(fmkorea)
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

    # ── 네이버 수집 + 부정글 분류 (브라우저 종료 후 독립 실행) ──
    print("\n[네이버] 수집 중...")
    naver_data = await crawl_naver()

    print("\n[네이버] 부정글 분류 중...")
    naver_negative = await filter_negative_naver(naver_data)
    naver_total = sum(len(v) for v in naver_negative.values())
    print(f"[네이버] 부정글 최종 {naver_total}건")

    # ── 리포트 생성 ────────────────────────────────────────
    print("\n[분석] Groq 리포트 생성 중...")
    report = await generate_report(unique_posts, week_label, naver_negative)
    print(report)

    # ── Gmail 발송 ─────────────────────────────────────────
    print("\n[발송] Gmail 전송 중...")
    send_email(
        subject=f"[커뮤니티 모니터링] 아이즈모바일 {week_label}",
        report=report,
        all_posts=unique_posts,
        naver_negative=naver_negative,
    )
    print("[완료]")


if __name__ == "__main__":
    asyncio.run(main())
