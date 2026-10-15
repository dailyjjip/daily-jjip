# 오늘의 이슈 카드뉴스 자동 게시

매일 08:10 / 12:10 / 19:10(한국시간)에 뉴스 헤드라인을 모아 이슈 1개를 골라 카드뉴스(1080×1350)를 만들고 인스타그램에 올립니다. 하루 3개.

## 셋업 (약 30분, 한 번만)

1. **인스타 계정 준비**: 프로페셔널(크리에이터/비즈니스) 계정으로 전환. 페이스북 페이지는 필요 없음.
2. **Meta 토큰 발급 (Instagram 로그인 방식)**: developers.facebook.com → 앱 만들기 → 사용 사례에서 "Instagram API" 선택 →
   "Instagram 로그인으로 API 설정" → daily.jjip 계정 추가 → 권한 `instagram_business_basic`, `instagram_business_content_publish` →
   **액세스 토큰 생성** (장기 토큰, 60일) 및 인스타 사용자 ID 확인.
3. **Claude API 키**: console.anthropic.com 에서 발급.
4. **GitHub 공개 저장소** 새로 만들고 이 폴더 전체 업로드 (이미지 공개 URL 때문에 반드시 Public).
5. 저장소 → Settings → Secrets and variables → Actions:
   - Secrets: `ANTHROPIC_API_KEY`, `IG_USER_ID`, `IG_ACCESS_TOKEN`
   - (선택) `PEXELS_API_KEY`: pexels.com/api 에서 무료 발급. 넣으면 상단 비주얼이 실제 사진, 안 넣으면 3D 아이콘
   - Variables: `DRY_RUN` = `true` (처음엔 게시 없이 이미지만 생성)
6. Actions 탭 → "오늘의 이슈 카드뉴스" → Run workflow 로 테스트 → `posts/` 폴더에서 결과 확인.
7. 품질 괜찮으면 `DRY_RUN` 을 `false` 로 바꾸면 실제 게시 시작.

## 바꾸고 싶을 때
- 게시 시간·횟수: `.github/workflows/daily.yml` 의 cron (UTC 기준, 한국시간 −9시간)
- 색상·글씨 크기: `main.py` 의 `THEMES`, `render_card`
- 문구 톤·주제 규칙: `main.py` 의 `pick_issue` 프롬프트
- 뉴스 분야 제한(예: 경제만): `RSS_URL` 을 구글뉴스 분야별 RSS로 교체

## 주의
- 장기 토큰은 60일마다 갱신 필요 (만료되면 게시 단계에서 오류).
- 뉴스 사진은 쓰지 않음 (저작권). 비주얼은 Pexels 무료 사진(상업적 이용 가능) 또는 Fluent 3D 이모지(MIT)만 사용.
- 헤드라인만 근거로 쓰므로 가끔 맥락이 얕을 수 있음 → 초기 1~2주는 DRY_RUN으로 확인 권장.
