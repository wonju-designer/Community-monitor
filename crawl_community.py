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
        # 휴대폰 포럼 게시판 페이지별 수집 후 키워드 필터링
        base_url = "https://www.ppomppu.co.kr/zboard/zboard.php?id=phone&category=6&page={page}"
        print(f"    [뽐뿌] 휴대폰 포럼에서 '{keyword}' 검색 중...")

        for page_num in range(1, 4):  # 최대 3페이지
            url = base_url.format(page=page_num)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)

            title_els = await page.query_selector_all("a.baseList-title")
            print(f"    [뽐뿌] {page_num}페이지 {len(title_els)}개 게시글 발견")

            found_in_page = 0
            for title_el in title_els:
                try:
                    title = (await title_el.inner_text()).strip()
                    if not title or keyword not in title:
                        continue

                    href = await title_el.get_attribute("href")
                    full_url = f"https://www.ppomppu.co.kr/zboard/{href}" if href and not href.startswith("http") else (href or "")

                    # 부모 행에서 날짜/조회/댓글 추출
                    row = await title_el.evaluate_handle("el => el.closest('tr')")
                    cells = await row.query_selector_all("td")
                    date_text  = (await cells[-2].inner_text()).strip() if len(cells) >= 2 else ""
                    view_text  = (await cells[-1].inner_text()).strip() if len(cells) >= 1 else "0"
                    reply_el   = await row.query_selector("span.baseList-replyCount, .replyNum")
                    reply_text = (await reply_el.inner_text()).strip().replace("[","").replace("]","") if reply_el else "0"

                    results.append({
                        "site": "뽐뿌",
                        "title": title,
                        "url": full_url,
                        "date": date_text,
                        "reply_count": reply_text,
                        "view_count": view_text,
                        "comments": [],
                    })
                    found_in_page += 1
                except Exception:
                    continue

            if found_in_page == 0 and page_num > 1:
                break  # 더 이상 결과 없으면 중단

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

        # 확인된 셀렉터: a href="/숫자" 형태
        title_els = await page.query_selector_all("a[href^='/']")
        print(f"    [FM코리아] {len(title_els)}개 링크 발견")

        for title_el in title_els[:50]:
            try:
                href = await title_el.get_attribute("href") or ""
                # 숫자로만 이루어진 경로 (게시글 URL 패턴)
                if not href or not href.strip("/").isdigit():
                    continue

                # 텍스트에서 strong 태그 제거 후 추출
                title = (await title_el.inner_text()).strip()
                if not title or len(title) < 2:
                    continue

                full_url = f"https://www.fmkorea.com{href}"

                # 부모 요소에서 날짜/조회/댓글 추출
                parent = await title_el.evaluate_handle("el => el.closest('li') || el.parentElement")
                date_el  = await parent.query_selector(".regdate, time, .date, span.time")
                reply_el = await parent.query_selector(".replyCount, .comment_cnt, .noti_reply")
                view_el  = await parent.query_selector(".readCount, .hit, .noti_read")

                date_text  = (await date_el.inner_text()).strip()  if date_el  else ""
                reply_text = (await reply_el.inner_text()).strip() if reply_el else "0"
                view_text  = (await view_el.inner_text()).strip()  if view_el  else "0"

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
def build_table_html(all_posts: list[dict]) -> str:
    site_groups = {}
    for post in all_posts:
        site = post["site"]
        if site not in site_groups:
            site_groups[site] = []
        site_groups[site].append(post)

    tables_html = ""
    for site, posts in site_groups.items():
        tables_html += f"""
<h3 style="font-size:14px; font-weight:500; margin:28px 0 8px; color:#111; border-left:3px solid #1a73e8; padding-left:8px;">
  {site} <span style="font-size:12px; color:#999; font-weight:400;">({len(posts)}건)</span>
</h3>
<table style="width:100%; border-collapse:collapse; font-size:12px;">
  <thead>
    <tr style="background:#f8f8f8;">
      <th style="text-align:left; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:55%;">제목</th>
      <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:15%;">날짜</th>
      <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:10%;">조회</th>
      <th style="text-align:center; padding:7px 10px; border-bottom:1px solid #e0e0e0; width:10%;">댓글</th>
    </tr>
  </thead>
  <tbody>"""
        for post in posts:
            title_html = (
                f'<a href="{post["url"]}" style="color:#1a73e8; text-decoration:none;">{post["title"]}</a>'
                if post["url"] else post["title"]
            )
            tables_html += f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:7px 10px;">{title_html}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{post["date"]}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{post["view_count"]}</td>
      <td style="padding:7px 10px; text-align:center; color:#666;">{post["reply_count"]}</td>
    </tr>"""
        tables_html += "</tbody></table>"
    return tables_html


def send_email(subject: str, body: str, post_count: int, all_posts: list[dict]):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_TO

    tables_html = build_table_html(all_posts)

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:720px; margin:0 auto; padding:24px;">
  <h2 style="font-size:18px; font-weight:500; border-bottom:1px solid #eee; padding-bottom:12px;">{subject}</h2>
  <p style="font-size:12px; color:#999; margin-bottom:20px;">총 {post_count}건 수집 · 검색어: 아이즈비전, 아이즈모바일</p>

  <!-- AI 분석 리포트 -->
  <div style="white-space:pre-wrap; font-size:14px; line-height:1.8; background:#fafafa; padding:16px 20px; border-radius:8px; border:1px solid #eee;">{body}</div>

  <!-- 수집 게시글 목록 -->
  <h2 style="font-size:16px; font-weight:500; margin-top:36px; margin-bottom:4px; border-bottom:1px solid #eee; padding-bottom:10px;">수집 게시글 목록</h2>
  {tables_html}

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
        all_posts=unique_posts,
    )
    print("[완료]")


if __name__ == "__main__":
    asyncio.run(main())
