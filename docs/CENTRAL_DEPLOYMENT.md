# 중앙봇 연동 최종 준비

2026-09-11 운영 연동 가이드 기준. 과거 IP 및 도메인의 현재 연결 상태는 확인하지 않았다.

## 운영 서버 설정

`.env.example`을 참고해 운영 서버의 비공개 환경 설정에 입력한다. 키를 채팅, Git 또는 로그에 넣지 않는다.

| 설정 | 필요한 값 |
| --- | --- |
| `DECKOUT_CENTRAL_URL` | 중앙봇 Tailscale IP 또는 DNS와 포트를 포함한 URL |
| `DECKOUT_API_KEY` | deckout 전용 키: read, currency:grant, currency:deduct |
| `DECKOUT_INGRESS_SECRET` | 중앙봇과 Deckout 양쪽에 동일하게 설치하는 별도 공유 비밀 |
| `DECKOUT_CHANNEL_ID` | 전용 부모 채널 ID |
| `DECKOUT_DB` | 영속 디스크의 기존 DB 경로 |
| `DECKOUT_SKIP_CAPABILITY_CHECK` | 운영은 0 |
| `DECKOUT_ALLOW_UNAUTHENTICATED_LOCAL` | 운영은 0 |

관리자 화면을 사용하면 관리자 비밀번호와 서명 비밀도 서버에 설정한다. HTTPS에서는 보안 쿠키 설정을 유지한다.

## 중앙봇 설정

등록 이름 `deckout`, `channel`은 부모 채널, `service_url`은 **중앙봇에서 접근 가능한 Deckout 주소**로 설정하고 `route_threads: true`를 유지한다.
모든 `/event`와 `/shutdown` 요청에 `X-ARI-Minigame-Secret` 헤더를 붙인다. 배달 결과 및 스레드 삭제 콜백도 예외가 없다.
Deckout에서 중앙봇으로 보내는 요청은 별도 `X-API-Key`를 사용한다.
Tailscale은 양쪽 호스트가 연결되어야 하며 양방향 포트 접근이 필요하다. 같은 호스트가 아니면 loopback 주소로 서로 연결할 수 없다.

## 적용 순서

1. 기존 서비스 정지 후 SQLite DB 백업. 기존 DB에는 `bootstrap --force`를 실행하지 않는다.
2. 의존성 설치 및 기존 배포의 `python -m app.cli.upgrade_deck_refresh --db <기존-DB-경로>` 절차로 DB/콘텐츠 갱신. 이 명령의 기본 경로는 `deckout.db`이므로 운영 DB 경로를 반드시 명시한다.
3. 위 환경 설정을 적용하고 `python -m app.cli.check_central` 실행. 이 명령은 health/capabilities 조회만 수행한다.
4. Deckout 실행 후 `/healthz` 200 확인. 미준비·종료 중 또는 DB 접근 실패는 503이다.
5. 중앙봇 측에서 Deckout에 도달 가능한지 확인. 인증 없는 `/event`, `/shutdown`은 401이어야 한다. 비밀 미설정 시 운영 기동은 차단된다.
6. 지정 채널에서 `!덱아웃`을 실행해 개인 스레드 생성, 멘션, 버튼, 대상 선택, 이미지 분리, 콜백, 재시작 복구를 확인한다. 이 단계는 실제 플레이어 상태를 만든다.

기존 서비스에도 인증을 추가하므로 **중앙봇의 공유 헤더 설정을 함께 적용해야 한다**. 먼저 Deckout만 갱신하면 기존 요청이 401로 거절된다.

## 아직 필요한 정보

중앙봇 URL(포트 포함), Deckout 실행 호스트와 접근 주소/포트, 부모 채널 확인, Tailscale 양방향 연결 상태, 운영 호스트 접근 방법.
API 키와 공유 비밀은 운영 호스트에 설치한 후 설정 완료 여부만 확인한다. 현재 작업 공간의 프로세스는 상시 운영 서버를 대신하지 않는다.
