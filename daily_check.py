"""
아이즈모바일 커뮤니티 모니터링 — 매일 점검
- 수집: 디시인사이드, 뽐뿌 (오늘 게시글만)
- 1차: Groq — 부정글 분류 + 심각도 판단
- 2차: Gemini — 추가 키워드 결정 + 추가 수집 + 대응 초안 (심각도 높을 때만)
- 발송: 긴급 알림 (심각도 높을 때만)
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
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
REPORT_TO          = os.environ["REPORT_TO"]

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
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

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
- 정보성 글
"""

# 심각도 기준
ALERT_THRESHOLD = 5    # 부정글 절대값
ALERT_RATIO     = 2.0  # 평소 대비 배율


# ── 날짜 유틸 ──────────────────────────────────────────────
def get_today_label():
    today = datetime.date.today()
    return f"{today.year}년 {today.month}월 {today.day}일"

def is_today(date_str: str) -> bool:
    if not date_str:
        return True
    today = datetime.date.today()
    since = today - datetime.timedelta(days=2)
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


# ── 디시인사이드 수집 (오늘치) ────────────────────────────
async def crawl_dcinside_today(page, keyword: str) -> list[dict]:
    results = []
    try:
        encoded = quote(keyword)
        url = f"https://gall.dcinside.com/mgallery/board/lists?id=mvnogallery&s_type=search_subject_memo&s_keyword={encoded}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        rows = await page.query_selector_all("tr.ub-content")
        if not rows:
            rows = await page.query_selector_all("tbody tr")

        for row in rows[:20]:
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
                if is_excluded(title):
                    continue
                date_el = await row.query_selector("td.gall_date, span.gall_date")
                date_text = (await date_el.get_attribute("title") or await date_el.inner_text()).strip() if date_el else ""
                if not is_today(date_text):
                    continue
                href = await title_el.get_attribute("href") or ""
                full_url = f"https://gall.dcinside.com{href}" if href.startswith("/") else href
                view_el = await row.query_selector("td.gall_count")
                view_text = (await view_el.inner_text()).strip() if view_el else "0"
                results.append({
                    "site": "디시인사이드", "title": title,
                    "url": full_url, "date": date_text, "view_count": view_text,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"    [디시] 오류: {e}")
    return results


# ── 뽐뿌 수집 (오늘치) ────────────────────────────────────
_ppomppu_done = False

async def crawl_ppomppu_today(page) -> list[dict]:
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
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            links = await page.query_selector_all("a[href]")
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
                    if is_excluded(title):
                        continue
                    parent = await link.evaluate_handle(
                        "el => el.closest('li') || el.closest('tr') || el.closest('div') || el.parentElement"
                    )
                    parent_text = await page.evaluate("el => el.innerText || el.textContent || ''", parent)
                    date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", parent_text)
                    date_text = date_match.group(1) if date_match else ""
                    if not is_today(date_text):
                        continue
                    seen_nos.add(no)
                    full_url = f"https://www.ppomppu.co.kr{href}" if href.startswith("/") else href
                    full_url = re.sub(r"&keyword=[^&]*", "", full_url)
                    results.append({
                        "site": "뽐뿌", "title": title,
                        "url": full_url, "date": date_text, "view_count": "0",
                    })
                except Exception:
                    continue
            await asyncio.sleep(1)

    except Exception as e:
        print(f"    [뽐뿌] 오류: {e}")
    return results


# ── 추가 키워드 수집 (Gemini 결정 후) ─────────────────────
async def crawl_extra_keywords(page, keywords: list[str]) -> list[dict]:
    results = []
    seen_urls = set()
    for kw in keywords[:3]:
        try:
            encoded = quote(kw)
            url = f"https://gall.dcinside.com/mgallery/board/lists?id=mvnogallery&s_type=search_subject_memo&s_keyword={encoded}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            rows = await page.query_selector_all("tr.ub-content")
            for row in rows[:10]:
                try:
                    title_el = await row.query_selector("td.gall_tit a:first-child, .gall_tit a")
                    if not title_el:
                        continue
                    title = (await title_el.inner_text()).strip()
                    if not title:
                        continue
                    href = await title_el.get_attribute("href") or ""
                    full_url = f"https://gall.dcinside.com{href}" if href.startswith("/") else href
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    date_el = await row.query_selector("td.gall_date, span.gall_date")
                    date_text = (await date_el.get_attribute("title") or await date_el.inner_text()).strip() if date_el else ""
                    if not is_today(date_text):
                        continue
                    results.append({
                        "site": "디시인사이드(추가수집)",
                        "title": title, "url": full_url, "date": date_text,
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"    [추가수집] '{kw}' 오류: {e}")
        await asyncio.sleep(1)
    return results


# ── Groq: 부정글 분류 + 심각도 판단 ──────────────────────
async def groq_classify_and_judge(all_posts: list[dict]) -> dict:
    if not all_posts:
        return {"negative_posts": [], "severity": "낮음", "main_issue": "", "extra_keywords": []}

    titles = [p["title"] for p in all_posts]
    prompt = f"""아래 오늘 수집된 '아이즈모바일' 관련 게시글을 분석하세요.

{NEGATIVE_CRITERIA}

게시글 목록:
{chr(10).join(f"{i}. [{p['site']}] {p['title']}" for i, p in enumerate(all_posts))}

아래 JSON만 응답:
{{
  "negative_indices": [부정글 0기반 인덱스들],
  "severity": "높음 또는 보통 또는 낮음",
  "main_issue": "주요 이슈 한 줄 (없으면 빈 문자열)",
  "extra_keywords": ["추가수집 키워드1", "추가수집 키워드2"]
}}

심각도 기준:
- 높음: 부정글 5건 이상, 또는 동일 이슈(환불/오류/장애) 3건 이상 집중
- 보통: 부정글 2~4건
- 낮음: 부정글 1건 이하

extra_keywords: 심각도 높음일 때만 주요 이슈 관련 검색 키워드 2~3개 반환 (예: ["환불", "개통오류"])"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 400, "temperature": 0.1},
            )
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"]
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    result = json.loads(match.group())
                    negative_posts = [all_posts[i] for i in result.get("negative_indices", []) if isinstance(i, int) and 0 <= i < len(all_posts)]
                    return {
                        "negative_posts": negative_posts,
                        "severity": result.get("severity", "낮음"),
                        "main_issue": result.get("main_issue", ""),
                        "extra_keywords": result.get("extra_keywords", []),
                    }
    except Exception as e:
        print(f"[Groq] 분류 오류: {e}")

    return {"negative_posts": [], "severity": "낮음", "main_issue": "", "extra_keywords": []}


# ── Gemini: 대응 초안 작성 ────────────────────────────────
async def gemini_write_draft(main_issue: str, negative_posts: list[dict], extra_posts: list[dict]) -> str:
    all_related = negative_posts + extra_posts
    prompt = f"""당신은 아이즈모바일 고객 응대 전문가입니다.
오늘 커뮤니티에서 아래 이슈가 집중 발생했습니다.

주요 이슈: {main_issue}

관련 게시글:
{chr(10).join(f"- [{p['site']}] {p['title']}" for p in all_related[:10])}

아래 형식으로 작성해주세요:

## 이슈 요약
(2줄 이내)

## 고객 응대 초안
(실제 고객에게 전달할 공식 응대 문구)

## 내부 조치 권고
- 조치1
- 조치2
- 조치3

한국어로 작성해주세요."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]}
            )
            if resp.status_code == 200:
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                print(f"[Gemini] 오류: {resp.status_code}")
    except Exception as e:
        print(f"[Gemini] 오류: {e}")
    return ""


# ── 긴급 알림 이메일 ───────────────────────────────────────
def send_alert_email(today_label: str, severity: str, main_issue: str,
                     negative_posts: list[dict], extra_posts: list[dict],
                     response_draft: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚨 [긴급] 아이즈모바일 이슈 감지 — {today_label}"
    msg["From"] = GMAIL_USER
    msg["To"]   = REPORT_TO

    # 부정 게시글 HTML
    neg_html = "".join([
        f'<li style="margin:6px 0;">'
        f'<span style="background:#fee2e2; color:#991b1b; padding:1px 6px; border-radius:3px; font-size:11px; margin-right:6px;">{p["site"]}</span>'
        f'<a href="{p["url"]}" style="color:#991b1b; text-decoration:none;">{p["title"]}</a>'
        f'<span style="color:#999; font-size:11px; margin-left:6px;">{p.get("date","")}</span></li>'
        for p in negative_posts
    ])

    # 추가 수집 게시글 HTML
    extra_html = ""
    if extra_posts:
        extra_html = f"""
<h3 style="font-size:14px; font-weight:500; margin:20px 0 8px; color:#111;">추가 수집 게시글</h3>
<ul style="margin:0; padding-left:16px;">
{"".join([f'<li style="margin:4px 0; font-size:13px;"><a href="{p["url"]}" style="color:#444;">{p["title"]}</a></li>' for p in extra_posts])}
</ul>"""

    # 대응 초안 HTML
    draft_html = ""
    if response_draft:
        lines = response_draft.split("\n")
        parts = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            elif line.startswith("## "):
                parts.append(f'<div style="margin-top:16px; font-size:14px; font-weight:500; color:#111;">{line[3:]}</div>')
            elif line.startswith("- "):
                parts.append(f'<div style="padding:2px 0 2px 16px; font-size:13px; color:#444;">• {line[2:]}</div>')
            else:
                parts.append(f'<div style="font-size:13px; color:#444; padding:2px 0;">{line}</div>')
        draft_html = "\n".join(parts)

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:720px; margin:0 auto; padding:24px;">
  <div style="background:#991b1b; color:white; padding:16px 20px; border-radius:8px; margin-bottom:24px;">
    <div style="font-size:18px; font-weight:500;">🚨 커뮤니티 이슈 급증 감지</div>
    <div style="font-size:12px; opacity:0.9; margin-top:4px;">{today_label} · 심각도: {severity} · 즉각 확인 필요</div>
  </div>

  <div style="background:#fef2f2; border-left:4px solid #ef4444; padding:14px 16px; border-radius:4px; margin-bottom:20px;">
    <strong style="color:#991b1b; font-size:14px;">주요 이슈:</strong>
    <span style="font-size:13px; color:#7f1d1d; margin-left:8px;">{main_issue}</span>
  </div>

  <h3 style="font-size:14px; font-weight:500; margin:20px 0 8px; color:#111;">
    부정 게시글 ({len(negative_posts)}건)
  </h3>
  <ul style="margin:0; padding-left:16px;">{neg_html}</ul>

  {extra_html}

  {f'<h3 style="font-size:14px; font-weight:500; margin:24px 0 8px; color:#111;">대응 초안 (Gemini 작성)</h3><div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px; padding:14px 16px;">{draft_html}</div>' if draft_html else ""}

  <hr style="border:none; border-top:1px solid #eee; margin-top:32px;">
  <p style="font-size:11px; color:#bbb;">아이즈모바일 · AI 에이전트 긴급 알림 · 자동 발송</p>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"[긴급 알림] 발송 완료 → {REPORT_TO}")


# ── 메인 ───────────────────────────────────────────────────
async def main():
    global _ppomppu_done
    _ppomppu_done = False

    today_label = get_today_label()
    print(f"[시작] {today_label} 일일 점검")

    # ① 오늘 게시글 수집
    all_posts = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}, locale="ko-KR",
        )
        page = await context.new_page()

        for keyword in KEYWORDS:
            all_posts.extend(await crawl_dcinside_today(page, keyword))
            await asyncio.sleep(1)

        all_posts.extend(await crawl_ppomppu_today(page))

        print(f"\n[수집 완료] 오늘 총 {len(all_posts)}건")

        # ② Groq: 부정글 분류 + 심각도 판단
        print("\n[Groq] 부정글 분류 + 심각도 판단 중...")
        analysis = await groq_classify_and_judge(all_posts)
        negative_posts = analysis["negative_posts"]
        severity       = analysis["severity"]
        main_issue     = analysis["main_issue"]
        extra_keywords = analysis["extra_keywords"]

        print(f"[판단] 심각도: {severity} / 부정글: {len(negative_posts)}건 / 이슈: {main_issue}")

        # ③ 심각도 높을 때만 추가 행동
        extra_posts = []
        response_draft = ""

        if severity == "높음":
            print(f"\n[Gemini] 심각도 높음 → 추가 수집 키워드: {extra_keywords}")

            # ④ 추가 키워드로 게시글 수집
            if extra_keywords:
                extra_posts = await crawl_extra_keywords(page, extra_keywords)
                print(f"[추가수집] {len(extra_posts)}건")

            await browser.close()

            # ⑤ Gemini: 대응 초안 작성
            print("\n[Gemini] 대응 초안 작성 중...")
            response_draft = await gemini_write_draft(main_issue, negative_posts, extra_posts)
            if response_draft:
                print("[Gemini] 대응 초안 작성 완료")

            # ⑥ 긴급 알림 발송
            print("\n[긴급 알림] 발송 중...")
            send_alert_email(
                today_label=today_label,
                severity=severity,
                main_issue=main_issue,
                negative_posts=negative_posts,
                extra_posts=extra_posts,
                response_draft=response_draft,
            )

        else:
            await browser.close()
            print(f"\n[정상] 심각도 {severity} — 알림 없이 종료")

    print("[완료]")


if __name__ == "__main__":
    asyncio.run(main())
