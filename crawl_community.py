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

def get_since_date():
    """7일 전 날짜 반환"""
    return datetime.date.today() - datetime.timedelta(days=7)

def is_within_week(date_str: str) -> bool:
    """날짜 문자열이 최근 7일 이내인지 확인"""
    if not date_str:
        return True  # 날짜 없으면 포함
    since = get_since_date()
    # 다양한 날짜 형식 파싱
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d",
        "%y/%m/%d",
        "%m/%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(date_str.strip(), fmt)
            # 연도 없는 형식 처리 (MM/DD)
            if fmt == "%m/%d":
                parsed = parsed.replace(year=datetime.date.today().year)
            return parsed.date() >= since
        except ValueError:
            continue
    return True  # 파싱 실패시 포함


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

                date_el  = await row.query_selector("td.gall_date, span.gall_date")
                reply_el = await row.query_selector("td.gall_reply_num, .gall_comment")
                view_el  = await row.query_selector("td.gall_count")

                if date_el:
                    date_text = await date_el.get_attribute("title") or ""
                    if not date_text:
                        date_text = (await date_el.inner_text()).strip()
                    date_text = date_text.strip()
                else:
                    date_text = ""
                reply_text = (await reply_el.inner_text()).strip() if reply_el else "0"
                view_text  = (await view_el.inner_text()).strip() if view_el else "0"
                full_url   = f"https://gall.dcinside.com{href}" if href and href.startswith("/") else (href or "")

                # 7일 이내 게시글만 포함
                if not is_within_week(date_text):
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
PPOMPPU_SEARCH_KEYWORDS = ["아이즈비전", "아이즈모바일", "아이즈"]
_ppomppu_done = False

async def crawl_ppomppu(page, keyword: str) -> list[dict]:
    """뽐뿌 통합검색 → 커뮤니티 결과만 수집 → 7일 이내 필터링"""
    global _ppomppu_done
    if _ppomppu_done:
        return []
    _ppomppu_done = True

    results = []
    seen_urls = set()

    try:
        for kw in PPOMPPU_SEARCH_KEYWORDS:
            encoded = quote(kw.encode("euc-kr"))
            url = f"https://www.ppomppu.co.kr/search_bbs.php?bbs_cate=&keyword={encoded}"
            print(f"    [뽐뿌] 검색: '{kw}' → {url}")

            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)

            # href에 /zboard/view.php 포함된 링크 전체 수집
            all_links = await page.query_selector_all("a[href*='/zboard/view.php']")
            print(f"    [뽐뿌] '{kw}' 링크 {len(all_links)}개 발견")

            for link in all_links[:30]:
                try:
                    href = await link.get_attribute("href") or ""
                    if not href:
                        continue

                    # 커뮤니티(ppomppu) 게시판만 수집
                    if "id=ppomppu" not in href and "id=phone" not in href and "id=freeboard" not in href:
                        # id 파라미터가 없거나 다른 게시판이면 일단 포함 (커뮤니티 폭넓게)
                        pass

                    full_url = f"https://www.ppomppu.co.kr{href}" if href.startswith("/") else href

                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    # 제목: font.comment-cnt(댓글수) 제외한 텍스트
                    title = await page.evaluate("""el => {
                        const clone = el.cloneNode(true);
                        const font = clone.querySelector('font.comment-cnt');
                        if (font) font.remove();
                        return clone.innerText || clone.textContent || '';
                    }""", link)
                    title = title.strip()
                    if not title or len(title) < 2:
                        continue

                    # 부모 tr에서 날짜/조회/댓글 추출
                    row = await link.evaluate_handle("el => el.closest('tr') || el.parentElement")
                    date_text = ""
                    view_text = "0"
                    reply_text = "0"

                    # span 날짜 탐색
                    date_el = await row.query_selector("span")
                    if date_el:
                        date_candidate = (await date_el.inner_text()).strip()
                        if "." in date_candidate or "-" in date_candidate:
                            date_text = date_candidate

                    # td 전체에서 날짜/조회 추출
                    cells = await row.query_selector_all("td")
                    if not date_text and len(cells) >= 2:
                        for cell in cells:
                            cell_text = (await cell.inner_text()).strip()
                            if "." in cell_text and len(cell_text) <= 12:
                                date_text = cell_text
                                break
                    if len(cells) >= 1:
                        view_text = (await cells[-1].inner_text()).strip()

                    # 댓글수: font.comment-cnt
                    reply_el = await link.query_selector("font.comment-cnt")
                    if reply_el:
                        reply_text = (await reply_el.inner_text()).strip()

                    # 7일 이내만 포함
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

        # 상위 3개 댓글 수집
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

                # 7일 이내 게시글만 포함
                if not is_within_week(date_text):
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
아래는 이번 주 알뜰폰 커뮤니티에서 수집된 게시글 및 댓글 데이터입니다.
아이즈비전(아이즈모바일)을 직접 언급하지 않더라도 알뜰폰 시장 전반의 여론, 경쟁사 동향, 고객 불만 등 아이즈비전과 관련될 수 있는 내용을 분석해주세요.

기간: {week_label}
총 수집 게시글: {len(all_posts)}건

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

## 2. 아이즈비전 직접 언급
(아이즈비전/아이즈모바일을 직접 언급한 게시글 요약)

## 3. 알뜰폰 시장 여론 (간접 관련)
(직접 언급은 없지만 아이즈비전에 영향을 줄 수 있는 시장 트렌드, 경쟁사 이슈, 고객 불만 등)

## 4. 감성 분석 (아이즈비전 직접 언급 기준)
- 긍정: X건
- 부정: X건
- 중립: X건
- 전반적 감성 점수: X/10

## 5. 주요 이슈 및 키워드

## 6. 대응 제언

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
