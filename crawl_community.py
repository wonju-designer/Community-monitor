"""
커뮤니티 아이즈비전(아이즈모바일) 언급 모니터링
- FM코리아, 뽐뿌, 디시인사이드 크롤링
- 감성 분석 (긍정/부정/중립)
- Claude API로 리포트 생성
- Gmail 발송
"""

import os
import asyncio
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
from playwright.async_api import async_playwright

# ── 환경 변수 ──────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
REPORT_TO          = os.environ["REPORT_TO"]

# ── 검색 키워드 ────────────────────────────────────────────
KEYWORDS = ["아이즈비전", "아이즈모바일", "eyesvision", "IzsVision"]

# ──────────────────────────────────────────────────────────

def get_week_label():
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)
    week_num = (monday.day - 1) // 7 + 1
    return f"{today.year}년 {today.month}월 {week_num}주차 ({monday.month}/{monday.day} – {sunday.month}/{sunday.day})"

def get_since_date():
    """7일 전 날짜 반환"""
    return datetime.date.today() - datetime.timedelta(days=7)


# ── 뽐뿌 크롤링 ───────────────────────────────────────────
async def crawl_ppomppu(page, keyword: str) -> list[dict]:
    results = []
    try:
        url = f"https://www.ppomppu.co.kr/search.php?search_type=sub_memo&keyword={keyword}"
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1)

        rows = await page.query_selector_all("table.common-list0 tr.list0, table.common-list0 tr.list1")
        for row in rows[:20]:
            try:
                title_el = await row.query_selector("a.baseList-title, td.baseList-title a")
                date_el  = await row.query_selector("td.baseList-space, time")
                reply_el = await row.query_selector("span.baseList-replyCount, td:nth-child(6)")
                view_el  = await row.query_selector("td:nth-child(7), td.baseList-views")

                if not title_el:
                    continue

                title = (await title_el.inner_text()).strip()
                href  = await title_el.get_attribute("href")
                date  = (await date_el.inner_text()).strip() if date_el else ""
                reply = (await reply_el.inner_text()).strip() if reply_el else "0"
                view  = (await view_el.inner_text()).strip() if view_el else "0"

                results.append({
                    "site": "뽐뿌",
                    "title": title,
                    "url": f"https://www.ppomppu.co.kr{href}" if href and href.startswith("/") else href,
                    "date": date,
                    "reply_count": reply.replace("[","").replace("]",""),
                    "view_count": view,
                    "comments": [],
                })
            except Exception:
                continue

        # 각 게시글 댓글 수집 (상위 3개만)
        for item in results[:3]:
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                comment_els = await page.query_selector_all("td.comment_contents, div.comment-content")
                for c in comment_els[:10]:
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"[뽐뿌] 오류: {e}")
    return results


# ── 디시인사이드 크롤링 ────────────────────────────────────
async def crawl_dcinside(page, keyword: str) -> list[dict]:
    results = []
    try:
        url = f"https://search.dcinside.com/post/p/1/sort/latest/q/{keyword}"
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)

        items = await page.query_selector_all("li.sch-result-item, div.sch_result_area li")
        for item in items[:20]:
            try:
                title_el  = await item.query_selector("a.tit, strong.tit a, .tit_area a")
                date_el   = await item.query_selector("span.date, em.date")
                reply_el  = await item.query_selector("span.reply_num, em.reply_num")
                view_el   = await item.query_selector("span.view, em.view")

                if not title_el:
                    continue

                title = (await title_el.inner_text()).strip()
                href  = await title_el.get_attribute("href")
                date  = (await date_el.inner_text()).strip() if date_el else ""
                reply = (await reply_el.inner_text()).strip() if reply_el else "0"
                view  = (await view_el.inner_text()).strip() if view_el else "0"

                results.append({
                    "site": "디시인사이드",
                    "title": title,
                    "url": href,
                    "date": date,
                    "reply_count": reply,
                    "view_count": view,
                    "comments": [],
                })
            except Exception:
                continue

        # 댓글 수집 (상위 3개만)
        for item in results[:3]:
            try:
                if not item["url"]:
                    continue
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                comment_els = await page.query_selector_all(
                    "p.usertxt.ub-word, div.cmt_txt, li.ub-content p"
                )
                for c in comment_els[:10]:
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"[디시인사이드] 오류: {e}")
    return results


# ── FM코리아 크롤링 ────────────────────────────────────────
async def crawl_fmkorea(page, keyword: str) -> list[dict]:
    results = []
    try:
        url = f"https://www.fmkorea.com/search.php?act=IS&is_keyword={keyword}&mid=home&where=document&page=1"
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)

        items = await page.query_selector_all("li.li, ul.fmkorea-search li")
        for item in items[:20]:
            try:
                title_el  = await item.query_selector("h3.title a, a.title")
                date_el   = await item.query_selector("span.regdate, time")
                reply_el  = await item.query_selector("span.replyCount, .comment_cnt")
                view_el   = await item.query_selector("span.readCount, .hit")

                if not title_el:
                    continue

                title = (await title_el.inner_text()).strip()
                href  = await title_el.get_attribute("href")
                date  = (await date_el.inner_text()).strip() if date_el else ""
                reply = (await reply_el.inner_text()).strip() if reply_el else "0"
                view  = (await view_el.inner_text()).strip() if view_el else "0"

                full_url = f"https://www.fmkorea.com{href}" if href and href.startswith("/") else href

                results.append({
                    "site": "FM코리아",
                    "title": title,
                    "url": full_url,
                    "date": date,
                    "reply_count": reply,
                    "view_count": view,
                    "comments": [],
                })
            except Exception:
                continue

        # 댓글 수집 (상위 3개만)
        for item in results[:3]:
            try:
                if not item["url"]:
                    continue
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                comment_els = await page.query_selector_all(
                    "div.comment_content, li.comment_item p, .fdb_itm .xe_content"
                )
                for c in comment_els[:10]:
                    text = (await c.inner_text()).strip()
                    if text:
                        item["comments"].append(text[:200])
            except Exception:
                continue

    except Exception as e:
        print(f"[FM코리아] 오류: {e}")
    return results


# ── Claude 감성 분석 + 리포트 생성 ────────────────────────
def analyze_and_report(all_posts: list[dict], week_label: str) -> str:
    if not all_posts:
        return "이번 주 수집된 언급 데이터가 없습니다."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 데이터 정리
    site_summary = {}
    for post in all_posts:
        site = post["site"]
        if site not in site_summary:
            site_summary[site] = []
        site_summary[site].append(post)

    prompt = f"""당신은 브랜드 평판 분석 전문가입니다.
아래는 이번 주 커뮤니티에서 수집된 '아이즈비전(아이즈모바일)' 관련 게시글 및 댓글 데이터입니다.
각 게시글과 댓글의 감성을 분석하고 종합 리포트를 작성해주세요.

기간: {week_label}
총 언급 수: {len(all_posts)}건

"""
    for site, posts in site_summary.items():
        prompt += f"\n## {site} ({len(posts)}건)\n"
        for i, p in enumerate(posts, 1):
            prompt += f"\n{i}. [{p['date']}] {p['title']}\n"
            prompt += f"   조회 {p['view_count']} | 댓글 {p['reply_count']}\n"
            prompt += f"   URL: {p['url']}\n"
            if p["comments"]:
                prompt += f"   주요 댓글:\n"
                for c in p["comments"]:
                    prompt += f"   - {c}\n"

    prompt += """

아래 형식으로 리포트를 작성해주세요:

## 1. 이번 주 핵심 요약
(3줄 이내로 가장 중요한 내용)

## 2. 사이트별 언급 현황
(각 사이트별 언급 수, 주요 토픽)

## 3. 감성 분석
- 긍정: X건 (주요 내용)
- 부정: X건 (주요 내용)
- 중립: X건 (주요 내용)
- 전반적 감성 점수: X/10

## 4. 주요 이슈 및 키워드
(반복적으로 언급되는 주제나 키워드)

## 5. 대응 제언
(부정 이슈가 있다면 대응 방향, 긍정 요소 활용 방안)

한국어로 작성하고 실무진이 바로 활용할 수 있게 간결하게 작성해주세요."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Gmail 발송 ─────────────────────────────────────────────
def send_email(subject: str, body: str, post_count: int):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_TO

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:680px; margin:0 auto; padding:24px;">
  <h2 style="font-size:18px; font-weight:500; border-bottom:1px solid #eee; padding-bottom:12px;">
    {subject}
  </h2>
  <p style="font-size:12px; color:#999; margin-bottom:20px;">
    총 {post_count}건 수집 · FM코리아, 뽐뿌, 디시인사이드
  </p>
  <div style="white-space:pre-wrap; font-size:14px; line-height:1.8;">{body}</div>
  <hr style="border:none; border-top:1px solid #eee; margin-top:32px;">
  <p style="font-size:11px; color:#bbb;">IzsVision 커뮤니티 모니터링 · 자동 발송</p>
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
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        for keyword in KEYWORDS:
            print(f"[검색] 키워드: {keyword}")

            print(f"  → 뽐뿌...")
            ppomppu = await crawl_ppomppu(page, keyword)
            all_posts.extend(ppomppu)

            print(f"  → 디시인사이드...")
            dcinside = await crawl_dcinside(page, keyword)
            all_posts.extend(dcinside)

            print(f"  → FM코리아...")
            fmkorea = await crawl_fmkorea(page, keyword)
            all_posts.extend(fmkorea)

            await asyncio.sleep(2)  # 키워드 간 딜레이

        await browser.close()

    # 중복 제거 (URL 기준)
    seen = set()
    unique_posts = []
    for p in all_posts:
        if p["url"] and p["url"] not in seen:
            seen.add(p["url"])
            unique_posts.append(p)

    print(f"[수집 완료] 총 {len(unique_posts)}건 (중복 제거 후)")

    print("[분석] Claude 감성 분석 + 리포트 생성 중...")
    report = analyze_and_report(unique_posts, week_label)
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
