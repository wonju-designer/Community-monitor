"""
daily_check.py — AI 에이전트 일일 점검
- 매일 오전 9시 실행
- 아이즈모바일 + 경쟁사(프리티, 티플러스) 부정 언급 감지
- 급증 시 즉시 긴급 알림 발송
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
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
REPORT_TO          = os.environ["REPORT_TO"]

# ── 모니터링 대상 ──────────────────────────────────────────
BRANDS = {
    "아이즈모바일": ["아이즈모바일", "아이즈"],
    "프리티":       ["프리티"],
    "티플러스":     ["티플러스"],
}

# 부정 급증 기준
ALERT_THRESHOLD     = 5   # 부정 언급 절대값 기준
ALERT_RATIO         = 2.0 # 평소 대비 배율 기준
BASELINE_FILE       = "baseline.json"  # 평소 평균 저장 파일

# ──────────────────────────────────────────────────────────

def get_today_label():
    today = datetime.date.today()
    return f"{today.year}년 {today.month}월 {today.day}일"

def is_within_days(date_str: str, days: int = 1) -> bool:
    """오늘 날짜 기준 days일 이내인지 확인"""
    if not date_str:
        return True
    today = datetime.date.today()
    since = today - datetime.timedelta(days=days)
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


# ── 디시인사이드 빠른 수집 ─────────────────────────────────
async def quick_crawl_dcinside(page, keyword: str) -> list[dict]:
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

                date_el = await row.query_selector("td.gall_date, span.gall_date")
                if date_el:
                    date_text = await date_el.get_attribute("title") or (await date_el.inner_text()).strip()
                else:
                    date_text = ""

                # 오늘 게시글만 수집
                if not is_within_days(date_text, days=1):
                    continue

                view_el = await row.query_selector("td.gall_count")
                view_text = (await view_el.inner_text()).strip() if view_el else "0"

                href = await title_el.get_attribute("href") or ""
                full_url = f"https://gall.dcinside.com{href}" if href.startswith("/") else href

                results.append({
                    "site": "디시인사이드",
                    "brand": keyword,
                    "title": title,
                    "url": full_url,
                    "date": date_text,
                    "view_count": view_text,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"    [디시] 오류: {e}")
    return results


# ── 뽐뿌 빠른 수집 ────────────────────────────────────────
async def quick_crawl_ppomppu(page, keyword: str) -> list[dict]:
    results = []
    seen_nos = set()
    try:
        encoded = quote(keyword.encode("euc-kr"))
        url = f"https://www.ppomppu.co.kr/search_bbs.php?page_size=20&bbs_cate=2&keyword={encoded}&order_type=date&search_type=sub_memo"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        links = await page.query_selector_all("a[href*='view.php?id=ppomppu']")

        for link in links:
            try:
                href = await link.get_attribute("href") or ""
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
                if not any(k in title_clean for k in [keyword]):
                    continue

                seen_nos.add(no)
                full_url = f"https://www.ppomppu.co.kr{re.sub(r'&keyword=[^&]*', '', href)}"

                parent = await link.evaluate_handle(
                    "el => el.closest('li') || el.closest('tr') || el.closest('div') || el.parentElement"
                )
                parent_text = await page.evaluate(
                    "el => el.innerText || el.textContent || ''", parent
                )

                date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", parent_text)
                date_text = date_match.group(1) if date_match else ""

                if not is_within_days(date_text, days=1):
                    continue

                results.append({
                    "site": "뽐뿌",
                    "brand": keyword,
                    "title": title,
                    "url": full_url,
                    "date": date_text,
                    "view_count": "0",
                })
            except Exception:
                continue
    except Exception as e:
        print(f"    [뽐뿌] 오류: {e}")
    return results


# ── Groq 부정 언급 분석 ────────────────────────────────────
async def analyze_sentiment(posts: list[dict], brand: str) -> dict:
    """수집된 게시글에서 부정 언급 수와 주요 내용 분석"""
    if not posts:
        return {"negative_count": 0, "summary": "", "posts": []}

    prompt = f"""아래는 오늘 커뮤니티에서 수집된 '{brand}' 관련 게시글입니다.
각 게시글의 감성을 분석하고 부정적인 내용만 추출해주세요.

게시글 목록:
"""
    for i, p in enumerate(posts, 1):
        prompt += f"{i}. {p['title']} ({p['site']})\n"

    prompt += """
아래 JSON 형식으로만 응답해주세요 (다른 텍스트 없이):
{
  "negative_count": 부정 게시글 수,
  "negative_posts": ["부정 게시글 제목1", "부정 게시글 제목2"],
  "main_issues": ["주요 이슈1", "주요 이슈2"]
}"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.1,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            if resp.status_code != 200:
                return {"negative_count": 0, "summary": "", "posts": []}

            text = resp.json()["choices"][0]["message"]["content"]
            # JSON 파싱
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return {
                    "negative_count": result.get("negative_count", 0),
                    "negative_posts": result.get("negative_posts", []),
                    "main_issues": result.get("main_issues", []),
                }
    except Exception as e:
        print(f"[Groq] 분석 오류: {e}")

    return {"negative_count": 0, "negative_posts": [], "main_issues": []}


# ── 기준값 로드/저장 ───────────────────────────────────────
def load_baseline() -> dict:
    """저장된 평소 평균 부정 언급 수 로드"""
    try:
        if Path(BASELINE_FILE).exists():
            with open(BASELINE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    # 기본값: 브랜드별 평균 2건
    return {brand: 2.0 for brand in BRANDS}

def save_baseline(baseline: dict, today_counts: dict):
    """이동 평균으로 기준값 업데이트"""
    for brand, count in today_counts.items():
        if brand in baseline:
            # 지수 이동 평균 (가중치 0.3)
            baseline[brand] = baseline[brand] * 0.7 + count * 0.3
        else:
            baseline[brand] = float(count)
    try:
        with open(BASELINE_FILE, "w") as f:
            json.dump(baseline, f)
    except Exception:
        pass


# ── 긴급 알림 이메일 발송 ─────────────────────────────────
def send_alert_email(alerts: list[dict], today_label: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚨 [긴급] 커뮤니티 부정 언급 급증 감지 — {today_label}"
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_TO

    alert_html = ""
    for alert in alerts:
        issues_html = "".join([
            f'<li style="margin:4px 0; color:#444;">{issue}</li>'
            for issue in alert.get("main_issues", [])
        ])
        posts_html = "".join([
            f'<li style="margin:4px 0; color:#666; font-size:12px;">{post}</li>'
            for post in alert.get("negative_posts", [])
        ])

        alert_html += f"""
<div style="background:#fff5f5; border-left:4px solid #e53e3e; border-radius:4px;
  padding:16px; margin-bottom:16px;">
  <div style="font-size:15px; font-weight:500; color:#c53030; margin-bottom:8px;">
    ⚠️ {alert['brand']}
  </div>
  <div style="font-size:13px; color:#666; margin-bottom:8px;">
    오늘 부정 언급 <strong style="color:#c53030;">{alert['negative_count']}건</strong>
    (평소 평균 {alert['baseline']:.1f}건 대비 
    <strong style="color:#c53030;">{alert['ratio']:.1f}배</strong>)
  </div>
  <div style="font-size:13px; font-weight:500; color:#333; margin-bottom:4px;">주요 이슈</div>
  <ul style="margin:0; padding-left:16px;">{issues_html}</ul>
  <div style="font-size:13px; font-weight:500; color:#333; margin-top:10px; margin-bottom:4px;">부정 게시글</div>
  <ul style="margin:0; padding-left:16px;">{posts_html}</ul>
</div>"""

    html = f"""
<html>
<body style="font-family:sans-serif; line-height:1.7; color:#333; max-width:680px; margin:0 auto; padding:24px;">
  <div style="background:#c53030; color:white; padding:16px 20px; border-radius:8px; margin-bottom:24px;">
    <div style="font-size:18px; font-weight:500;">🚨 커뮤니티 부정 언급 급증 감지</div>
    <div style="font-size:12px; opacity:0.9; margin-top:4px;">{today_label} · 즉각 확인 필요</div>
  </div>
  {alert_html}
  <hr style="border:none; border-top:1px solid #eee; margin-top:32px;">
  <p style="font-size:11px; color:#bbb;">아이즈모바일 · 커뮤니티 모니터링 · AI 에이전트</p>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"[긴급 알림] 발송 완료 → {REPORT_TO}")


# ── 메인 ───────────────────────────────────────────────────
async def main():
    today_label = get_today_label()
    print(f"[일일 점검 시작] {today_label}")

    baseline = load_baseline()
    today_counts = {}
    alerts = []

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

        for brand, keywords in BRANDS.items():
            print(f"\n[점검] {brand}...")
            all_posts = []

            for kw in keywords:
                # 디시인사이드
                dci = await quick_crawl_dcinside(page, kw)
                all_posts.extend(dci)
                await asyncio.sleep(1)

                # 뽐뿌
                ppo = await quick_crawl_ppomppu(page, kw)
                all_posts.extend(ppo)
                await asyncio.sleep(1)

            print(f"    [{brand}] 오늘 수집: {len(all_posts)}건")

            if not all_posts:
                today_counts[brand] = 0
                continue

            # Groq 감성 분석
            analysis = await analyze_sentiment(all_posts, brand)
            negative_count = analysis.get("negative_count", 0)
            today_counts[brand] = negative_count

            print(f"    [{brand}] 부정 언급: {negative_count}건 (기준: {baseline.get(brand, 2.0):.1f}건)")

            # 급증 여부 판단
            brand_baseline = baseline.get(brand, 2.0)
            is_surge = (
                negative_count >= ALERT_THRESHOLD or
                (brand_baseline > 0 and negative_count >= brand_baseline * ALERT_RATIO)
            )

            if is_surge:
                print(f"    [{brand}] ⚠️ 급증 감지!")
                alerts.append({
                    "brand": brand,
                    "negative_count": negative_count,
                    "baseline": brand_baseline,
                    "ratio": negative_count / max(brand_baseline, 0.1),
                    "negative_posts": analysis.get("negative_posts", []),
                    "main_issues": analysis.get("main_issues", []),
                })

        await browser.close()

    # 기준값 업데이트
    save_baseline(baseline, today_counts)

    # 긴급 알림 발송
    if alerts:
        print(f"\n[알림] {len(alerts)}개 브랜드 급증 감지 → 긴급 이메일 발송")
        send_alert_email(alerts, today_label)
    else:
        print(f"\n[정상] 이상 없음 — 알림 없이 종료")

    print("[일일 점검 완료]")


if __name__ == "__main__":
    asyncio.run(main())
