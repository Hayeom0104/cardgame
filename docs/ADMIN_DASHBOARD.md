# Deckout 관리자 대시보드

## 카드 선택·상세 편집

상단 **카드 편집**에서 이름이나 ID로 검색한 뒤 왼쪽 카드를 선택한다.
오른쪽에 현재 사진과 모든 편집 가능한 상세 필드가 나타난다. 작은 화면에서는
카드 목록 아래에 상세 편집이 표시된다. 캐릭터·패시브도 같은 방식으로 편집한다.
사진 변경은 즉시 저장하고 미리보기를 새로 불러온다. 수치·효과는 초안 저장 후
버전 메뉴에서 검증·발행해야 적용된다. 저장하지 않고 나가면 브라우저가 확인한다.

게임 서버와 같은 프로세스에서 실제 DB를 관리한다. 현황, 밸런싱, 콘텐츠 편집,
그림 업로드, 버전 검증·발행, 플레이어·런 관리 메뉴가 있다. 현황은 실제 실행
중인 키의 Central service_id와 부모 채널을 표시한다. 인증키는 표시하지 않는다.

## Termux에서 활성화

실행 중인 서버를 Ctrl+C로 종료한 뒤:

```bash
cd ~/cardgame &&
git pull --ff-only origin claude/new-session-m5fo91 &&
python -m app.cli.setup_admin
```

비밀번호를 두 번 입력한다. 입력은 화면에 보이지 않는다. 기존 .env의 관리자
비밀번호만 저장하고 서명 비밀이 없을 때 생성한다. DB와 Central 인증 설정은
유지한다. 저장 완료 후:

```bash
cd ~/cardgame &&
env -u DECKOUT_ADMIN_PASSWORD -u DECKOUT_ADMIN_SECRET \
  DECKOUT_DB=runtime/deckout.db DECKOUT_CHANNEL_ID=1537480674825740388 \
  python -m uvicorn app.api.server:app --host 0.0.0.0 --port 8081
```

## 접속 주소

- 서버를 실행하는 태블릿: http://127.0.0.1:8081/admin/
- 같은 Tailscale 네트워크의 휴대폰/PC: `http://<태블릿의 Tailscale IP>:8081/admin/`

127.0.0.1은 링크를 여는 기기 자신이다. 휴대폰에서는 태블릿의 Tailscale 앱에
표시된 IP로 바꿔야 한다. Central 서버 IP가 아니라 **Deckout 태블릿 IP**다.
서버가 켜져 있어야 접속된다. 이 주소는 공개 인터넷 배포 주소가 아니다.

503이면 관리자 설정과 재시작 여부를 확인한다. 연결 자체가 실패하면 서버 실행,
8081 포트, Tailscale 연결을 확인한다. 로그인 후 현황의 Central 상태로 이전
서비스 키가 남아 있는지 확인할 수 있다. 초안 수정은 검증·발행 후 적용된다.
