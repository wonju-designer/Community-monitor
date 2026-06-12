"""
커뮤니티 아이즈비전(아이즈모바일) 언급 모니터링
- 디시인사이드 알뜰폰 마이너 갤러리
- 뽐뿌 휴대폰 포럼
- FM코리아 검색
- 리포트 생성: Gemini API
- 발송: Gmail SMTP
"""

import os
import asyncio
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

import httpx
from playwright.async_api import async_playwright

# ── 환경 변수 ──────────────────────────────────────────────
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
REPORT_TO          = os.environ["REPORT_TO"]

KEYWORDS = ["아이즈비전", "아이즈모바일"]

def get_week_label():
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)
    week_num = (monday.day - 1) // 7 + 1
    return f"{today.year}년 {today.month}월 {week_num}주차 ({monday.month}/{monday.day} – {sunday.month}/{sunday.day})"


# ── 디시인사이드 알뜰폰 갤러리 ────────────────────────────
async def crawl_dcinside(page, keyword: str) -> list[dict]:
    results = []
    try:
        encoded = quote(keyword)
        url = f"https://gall.dcinside.com/mgallery/board/lists?id=mvnogallery&s_type=search_subject_memo&s_keyword={encoded}"
        print(f"    [디시] URL: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        rows = await page.query_selector_all("tr.ub-content")
        if not rows:
            rows = await page.query_selector_all("tbody tr")
        print(f"    [디시] {len(rows)}개 행 발견")

        for row in rows[:20]:
            try:
                notice = await row.get_attribute("class")
                if notice and "notice" in notice:
                    continue
                title_el = await row.query_selector("td.gall_tit a:first-child, .gall_tit a")
                if not title_el:
                    continue
                title = (await title_el.inner_text()).strip()
                if not title or len(title) < 2:
                    continue
                href = await title_el.get_attribute("href")

                date_el  = await row.query_selector("td.gall_date")
                reply_el = await row.query_selector("td.gall_reply_num, .gall_comment")
                view_el  = await row.query_selector("td.gall_count")

                date_text  = (await date_el.get_attribute("title") or await date_el.inner_text()).strip() if date_el else ""
                reply_text = (await reply_el.inner_text()).strip() if reply_el else "0"
                view_text  = (await view_el.inner_text()).strip() if view_el else "0"
                full_url   = f"https://gall.dcinside.com{href}" if href and href.startswith("/") else (href or "")

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

        for item in results[:3]:
            try:
                if not item["url"]:
                    continue
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                comment_els = await page.query_selector_all("p.usertxt.ub-word, .cmt_txtbox p")
                for c in comment_els[:10]:
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"    [디시] 오류: {e}")
    return results


# ── 뽐뿌 ──────────────────────────────────────────────────
async def crawl_ppomppu(page, keyword: str) -> list[dict]:
    results = []
    try:
        encoded = quote(keyword)
        url = f"https://www.ppomppu.co.kr/search.php?search_type=sub_memo&keyword={encoded}"
        print(f"    [뽐뿌] URL: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        rows = await page.query_selector_all("tr.list0, tr.list1")
        print(f"    [뽐뿌] {len(rows)}개 행 발견")

        for row in rows[:20]:
            try:
                title_el = await row.query_selector("a.baseList-title, td.baseList-title a, .title a, td.title a")
                if not title_el:
                    links = await row.query_selector_all("a")
                    for l in links:
                        href = await l.get_attribute("href") or ""
                        if "no=" in href or "view" in href:
                            title_el = l
                            break
                if not title_el:
                    continue

                title = (await title_el.inner_text()).strip()
                if not title or len(title) < 2:
                    continue
                href = await title_el.get_attribute("href")

                cells = await row.query_selector_all("td")
                date_text  = (await cells[-2].inner_text()).strip() if len(cells) >= 2 else ""
                view_text  = (await cells[-1].inner_text()).strip() if len(cells) >= 1 else "0"
                reply_text = "0"
                reply_el   = await row.query_selector(".replyNum, .comment, span.replyCount")
                if reply_el:
                    reply_text = (await reply_el.inner_text()).strip().replace("[","").replace("]","")

                full_url = f"https://www.ppomppu.co.kr{href}" if href and href.startswith("/") else (href or "")

                results.append({
                    "site": "뽐뿌",
                    "title": title,
                    "url": full_url,
                    "date": date_text,
                    "reply_count": reply_text,
                    "view_count": view_text,
                    "comments": [],
                })
            except Exception:
                continue

        print(f"    [뽐뿌] '{keyword}' 결과: {len(results)}건")

        for item in results[:3]:
            try:
                if not item["url"]:
                    continue
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                comment_els = await page.query_selector_all("td.comment_contents, .comment_text")
                for c in comment_els[:10]:
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
        print(f"    [FM코리아] URL: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        selectors = ["ul.searchList li", ".search_result li", "li.li"]
        items = []
        for sel in selectors:
            items = await page.query_selector_all(sel)
            if len(items) > 1:
                print(f"    [FM코리아] 셀렉터 '{sel}' 로 {len(items)}개 발견")
                break

        for item in items[:20]:
            try:
                title_el = await item.query_selector("h3 a, .title a, a.title, h4 a, a")
                if not title_el:
                    continue
                title = (await title_el.inner_text()).strip()
                if not title or len(title) < 2:
                    continue
                href = await title_el.get_attribute("href")

                date_el  = await item.query_selector(".regdate, time, .date")
                reply_el = await item.query_selector(".replyCount, .comment_cnt, .reply")
                view_el  = await item.query_selector(".readCount, .hit, .view")

                date_text  = (await date_el.inner_text()).strip()  if date_el  else ""
                reply_text = (await reply_el.inner_text()).strip() if reply_el else "0"
                view_text  = (await view_el.inner_text()).strip()  if view_el  else "0"
                full_url   = f"https://www.fmkorea.com{href}" if href and href.startswith("/") else (href or "")

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
                if not item["url"]:
                    continue
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                comment_els = await page.query_selector_all(".xe_content, .comment_content, .fdb_itm p")
                for c in comment_els[:10]:
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"    [FM코리아] 오류: {e}")
    return results


# ── Groq로 감성 분석 + 리포트 생성 ──────────────────────
async def analyze_and_report(all_posts: list[dict], week_label: str) -> str:
    if not all_posts:
        return "이번 주 수집된 언급 데이터가 없습니다."

    site_summary = {}
    for post in all_posts:
        site = post["site"]
        if site not in site_summary:
            site_summary[site] = []
        site_summary[site].append(post)

    prompt = f"""당신은 브랜드 평판 분석 전문가입니다.
아래는 이번 주 커뮤니티에서 수집된 '아이즈비전(아이즈모바일)' 관련 게시글 및 댓글 데이터입니다.

기간: {week_label}
총 언급 수: {len(all_posts)}건

"""
    for site, posts in site_summary.items():
        prompt += f"\n## {site} ({len(posts)}건)\n"
        for i, p in enumerate(posts, 1):
            prompt += f"\n{i}. [{p['date']}] {p['title']}\n"
            prompt += f"   조회 {p['view_count']} | 댓글 {p['reply_count']}\n"
            if p.get("comments"):
                prompt += "   주요 댓글:\n"
                for c in p["comments"]:
                    prompt += f"   - {c}\n"

    prompt += """

아래 형식으로 리포트를 작성해주세요:

## 1. 이번 주 핵심 요약
(3줄 이내)

## 2. 사이트별 언급 현황
(각 사이트별 언급 수, 주요 토픽)

## 3. 감성 분석
- 긍정: X건
- 부정: X건
- 중립: X건
- 전반적 감성 점수: X/10

## 4. 주요 이슈 및 키워드

## 5. 대응 제언

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
            print(f"[Groq] 오류: {resp.status_code} {resp.text}")
            return f"리포트 생성 실패 (Groq API 오류: {resp.status_code})"
        data = resp.json()
        return data["choices"][0]["message"]["content"]


# ── Gmail 발송 ─────────────────────────────────────────────
def send_email(subject: str, body: str, post_count: int):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_TO

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:680px; margin:0 auto; padding:24px;">
  <h2 style="font-size:18px; font-weight:500; border-bottom:1px solid #eee; padding-bottom:12px;">{subject}</h2>
  <p style="font-size:12px; color:#999; margin-bottom:20px;">총 {post_count}건 수집 · 디시인사이드(알뜰폰갤), 뽐뿌, FM코리아</p>
  <div style="white-space:pre-wrap; font-size:14px; line-height:1.8;">{body}</div>
  <hr style="border:none; border-top:1px solid #eee; margin-top:32px;">
  <p style="font-size:11px; color:#bbb;">IzsVision 커뮤니티 모니터링 · 매주 월요일 자동 발송</p>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"[Gmail] 발송 완료 → {REPORT_TO}")


# ── 메인 ───────────────────────────────────────────────────
async def main():
    week_label = get_week_label()
    print(f"[시작] {week_label} 커뮤니티 모니터링")

    all_posts = []

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

    print(f"\n[수집 완료] 총 {len(unique_posts)}건 (중복 제거 후)")

    print("[분석] Groq 감성 분석 + 리포트 생성 중...")
    report = await analyze_and_report(unique_posts, week_label)
    print(report)

    print("[발송] Gmail 전송 중...")
    send_email(
        subject=f"[커뮤니티 모니터링] 아이즈비전 {week_label}",
        body=report,
        post_count=len(unique_posts),
    )
    print("[완료]")


if __name__ == "__main__":
    asyncio.run(main())
