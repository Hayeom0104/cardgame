# CardGameBot (가칭)

ARI 미니게임용 **로그라이크 카드 배틀 봇**. `docs/CardGame_Design_Doc_v1.md` 의
설계를 구현한 것으로, 문서에서 확정된 규칙만 코드로 옮기고 `TBD` 항목은
임의로 만들어 넣지 않았습니다.

> **봇 이름**은 설계 문서 §0 에서 미확정이라 코드/설정 전반에 `CardGameBot`
> 을 플레이스홀더로 씁니다. 확정되면 `CARDGAME_BOT_NAME` 환경 변수만 바꾸면
> 됩니다.

---

## 빠른 시작

```bash
pip install -r requirements.txt
uvicorn cardgamebot.main:app --reload --port 8080
```

기동 시 SQLite DB(`data/cardgame.db`)가 자동 생성되고, 게임이 처음부터 끝까지
동작하는지 확인할 수 있는 예시 콘텐츠가 시드됩니다.

* 게임 API: `POST /event` — 중앙봇이 호출
* 관리자 대시보드: <http://localhost:8080/admin/>
* API 문서: <http://localhost:8080/docs>

```bash
pytest          # 63개 테스트
```

---

## 아키텍처 (§1)

```
중앙봇 ──POST /event──▶ CardGameBot (FastAPI)
  ▲                         │
  └──── 응답(텍스트+PNG) ────┘
                            │
              ┌─────────────┴──────────────┐
              │                            │
       cardgame.db (자체 SQLite)      /admin 대시보드
```

* 이 봇은 **디스코드 게이트웨이 연결도 토큰도 갖지 않습니다.** 모든 디스코드
  전달은 중앙봇이 프록시합니다.
* 코인 / XP / 업적은 **중앙봇 소유**라 HTTP API 로만 접근합니다 (§1.1).
* 카르타 / 카드 조각 / 와일드카드 / 보유·덱·연구 상태는 **이 봇의 자체 DB**
  에만 존재합니다.

### 디렉터리

| 경로 | 역할 |
|---|---|
| `cardgamebot/balance.py` | **모든 수치가 모인 단일 파일.** 밸런싱 확정 시 여기만 수정 |
| `cardgamebot/game/combat.py` | 전투 엔진 (§2) |
| `cardgamebot/game/mapgen.py` | 로그라이크 맵 생성 (§3) |
| `cardgamebot/game/run_service.py` | 런 생애주기 — 노드 이동/해결/보상 (§3, §4, §12) |
| `cardgamebot/game/gacha.py` | 가챠·배너·천장·성급 (§4.4, §5) |
| `cardgamebot/game/research.py` | 연구 시스템 (§9) |
| `cardgamebot/game/shop.py` | 노드 상점 + 허브 상점 (§7) |
| `cardgamebot/game/equipment.py` | 장비 티어제 (§8) |
| `cardgamebot/render/` | Pillow 전투/맵/카드 렌더링 (§11) |
| `cardgamebot/core/commands.py` | `!카드` 커맨드 라우터 |
| `cardgamebot/core/central_client.py` | 중앙봇 API 클라이언트 (§1.1) |
| `cardgamebot/api/admin/` | 콘텐츠 관리 대시보드 (§10) |

---

## 설정

`.env` 또는 환경 변수 (`CARDGAME_` 접두사):

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CARDGAME_BOT_NAME` | `CardGameBot` | 봇 표시 이름 (§0 TBD) |
| `CARDGAME_DB_PATH` | `data/cardgame.db` | 자체 SQLite 경로 |
| `CARDGAME_CENTRAL_API_BASE` | `http://localhost:8000` | 중앙봇 API 주소 |
| `CARDGAME_CENTRAL_API_TOKEN` | — | 중앙봇 인증 토큰 |
| `CARDGAME_CENTRAL_API_ENABLED` | `false` | `false` 면 코인/XP 를 로컬 스텁 처리 |
| `CARDGAME_ALLOWED_CHANNEL_IDS` | `[]` | 비우면 전체 채널 허용 |
| `CARDGAME_DISCORD_CLIENT_ID` / `_SECRET` | — | 대시보드 OAuth (§10.2) |
| `CARDGAME_SESSION_SECRET` | `dev-insecure-change-me` | **운영에서 반드시 변경** |
| `CARDGAME_BOOTSTRAP_OWNER_IDS` | `[]` | 최초 Owner 로 지정할 디스코드 ID |

---

## 명령어

```
!카드 도움말                       명령 목록
!카드 정보 / 출석                  계정 상태 / 일일 보상
!카드 배너 / 뽑기 [배너] [10]      가챠
!카드 캐릭터 / 성급 <코드>         보유 캐릭터 / 성급 상승
!카드 시작 <캐릭터코드...>         런 시작
!카드 맵 / 이동 <번호>             맵 확인 / 노드 선택
!카드 상태                         전투 화면 다시 보기
!카드 사용 <번호> [대상번호]       카드 사용
!카드 넘기기                       턴 넘기기
!카드 선택 <번호>                  보상 카드 선택
!카드 구매 <번호>                  노드 상점 구매
!카드 포기                         런 포기
!카드 상점 / 상점구매 <번호>       허브 상점 (장비)
!카드 장비 / 장착 / 강화           장비 관리
!카드 연구 / 연구해금 <코드>       연구 시스템
```

---

## 구현된 설계 규칙

**전투 (§2)** — 캐릭터별 개별 턴, 플레이어·적 교차 진행, 매 턴 여러 장 드로우 후
1장 선택(나머지는 버림), 드로우 더미 고갈 시 재섞기 없음, 파티 공유 자원의
라운드별 완전 회복, 캐릭터별 개별 HP, 턴 시작 시 사라지는 블록,
상한 없는 적 수와 다중 행 배치.

**맵 (§3)** — 런마다 무작위 생성, 플레이어의 분기 선택, 패배 시 런 전체 리셋,
노드 간 HP 유지, 확정된 6종 노드 타입.

**메타 (§4~§9)** — 파티 1→3 슬롯(연구 해금), 가챠 0회여도 플레이 가능한 기본 카드,
1~3성 + 특정 캐릭터만 6성, 캐릭터/카드 통합 가챠 풀, 중복 변환(캐릭터→와일드카드,
카드→카드 조각), 영구 해금과 런 스코프 덱의 분리, 상시+한정 배너와 50/50,
카르타, 패시브 2→4 슬롯, 두 종류의 상점, 장비 티어제, 즉시 해금 연구.

**대시보드 (§10)** — 디스코드 OAuth, Owner/Editor 권한 분리, 카드/적/캐릭터
CRUD, 이미지 업로드, 변경 이력.

---

## 확인이 필요한 미확정 항목

설계 문서 §13 의 `TBD` 는 **임의로 정하지 않았습니다.** 동작에 값이 꼭 필요한
곳은 `balance.py` 에 플레이스홀더를 두고 전부 `TBD:` 주석을 달았습니다.

특히 확인이 필요한 것들:

1. **중앙봇 `/event` 요청·응답 스키마** — 경로만 설계 문서에 있고 본문 필드는
   `중앙봇_API_연동_가이드_업데이트.md` 에 있습니다. 해당 문서가 저장소에 없어
   `core/protocol.py` 가 여러 필드 별칭을 허용하도록 느슨하게 구현되어 있습니다.
   가이드와 대조가 필요합니다.
2. **덱 크기 ↔ 전투 길이** — §2.2 가 "재섞기 없음"이라, 한 캐릭터가 한 전투에서
   행동 가능한 턴 수가 `덱 크기 ÷ 턴당 드로우 수` 로 고정됩니다. 드로우 더미
   고갈 페널티(§13)를 정할 때 이 상호작용을 함께 봐야 합니다.
3. **캐릭터 성급(1~3)과 카드 등급(1~6)의 대응** — 같은 가챠 풀에서 뽑히는데
   척도가 달라, 임시로 2단계씩 묶었습니다 (`CARD_RARITY_TO_GACHA_TIER`).
4. **이벤트 노드 콘텐츠** — 미확정이라 통과만 하고 아무 일도 일어나지 않습니다.
5. **상태 효과 분류** — 화상/기절 등이 미확정이라 `effects.py` 에 해당 op 를
   만들지 않았습니다. 확정 시 `register_effect()` 로 추가하면 엔진 수정 없이
   붙습니다.
6. **장비 강화 재료 이름** — 식별자만 `equip_material` 로 잡아두었습니다.

---

## 아트 에셋

§11 은 카드마다 고유 아트를 요구하며, 이는 엔지니어링이 아니라 **아트 프로덕션
의존성**입니다. 에셋이 없어도 렌더링이 깨지지 않도록 코드 색상 블록으로 대체하며,
대시보드에서 이미지를 업로드하면 자동으로 반영됩니다.

한글 렌더링에는 CJK 폰트가 필요합니다. `cardgamebot/assets/fonts/` 에
`NotoSansKR-Regular.ttf` 등을 넣으면 우선 사용하고, 없으면 시스템 폰트를 찾습니다.
