# 커뮤니티 모니터링 — 아이즈비전(아이즈모바일)

매주 월요일 오전 9시(KST), FM코리아 / 뽐뿌 / 디시인사이드에서
아이즈비전 관련 게시글·댓글을 자동 수집하고 감성 분석 리포트를 Gmail로 발송합니다.

## 수집 항목
- 게시글 제목, 날짜, 조회수, 댓글수
- 주요 댓글 내용
- 감성 분석 (긍정 / 부정 / 중립)

## 검색 키워드
- 아이즈비전
- 아이즈모바일
- eyesvision
- IzsVision

## 설정 방법

### 1. GitHub Secrets 등록
저장소 → Settings → Secrets and variables → Actions → New repository secret

| Secret 이름        | 설명                              |
|--------------------|-----------------------------------|
| `ANTHROPIC_API_KEY` | Anthropic API 키                 |
| `GMAIL_USER`        | 발신 Gmail 주소                  |
| `GMAIL_APP_PASSWORD`| Gmail 앱 비밀번호 (16자리)       |
| `REPORT_TO`         | 수신 이메일 주소                 |

### 2. Gmail 앱 비밀번호 발급
1. Google 계정 → 보안 → 2단계 인증 활성화
2. 보안 → 앱 비밀번호 → 새 앱 비밀번호 생성
3. 생성된 16자리를 `GMAIL_APP_PASSWORD`에 등록

### 3. 수동 테스트
저장소 → Actions → 주간 커뮤니티 모니터링 → Run workflow

## 리포트 구성
1. 이번 주 핵심 요약
2. 사이트별 언급 현황
3. 감성 분석 (긍정/부정/중립 건수 + 감성 점수)
4. 주요 이슈 및 키워드
5. 대응 제언
