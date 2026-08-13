# 이 저장소에서 작업할 때

## 여러 사람이 동시에 편집한다

이 저장소는 여러 사용자가 함께 편집한다. **작업을 시작하기 전에 반드시 변경분을
먼저 확인하고, 손댈 부분이 있으면 사용자에게 물어본 뒤 진행한다.**

```bash
git status --short                      # 작업 트리에 남의 변경이 있는가
git fetch origin <branch>               # 원격이 앞서 있는가
git log --oneline HEAD..origin/<branch>
git ls-remote --heads origin            # 다른 브랜치에서 작업 중인가
```

내가 마지막으로 본 상태와 다르면, 무엇이 어떻게 달라졌는지 먼저 보고하고
어떻게 할지 확인받는다. 남의 변경을 조용히 덮어쓰거나 되돌리지 않는다.

## 현재 병존하는 두 구현

| | `app/` | `cardgamebot/` |
|---|---|---|
| 브랜치 | `claude/new-session-m5fo91` | `claude/review-existing-implementation-5c0u0s` |
| 기준 문서 | **v6.4** | **v1** |
| 관리자 대시보드 | 검증 계층만 | 전체 구현 |

두 코드베이스는 패키지 이름이 달라 파일이 거의 겹치지 않는다
(`.gitignore`, `README.md`, `tests/conftest.py`, `tests/test_combat.py` 정도).
어느 쪽을 살릴지는 **오너 결정 사항**이며, 임의로 병합하거나 폐기하지 않는다.

v1과 v6.4 사이에는 세 차례의 검증 보고서와 27개 수정사항이 있다. v1 기준
코드에는 그 수정들이 들어 있지 않다는 점을 감안해서 판단해야 한다.

## 설계 문서를 대하는 방식

- §0.1 결정 출처 표시를 지킨다: 🔴 PENDING은 **발명하지 않는다**, 🟡
  RECOMMENDED는 쓰여진 대로 구현하되 주석으로 표시한다.
- §15의 모든 수치는 DB/설정에서 읽는다. **하드코딩 금지.**
- 문서가 스스로 모순될 때는 임의로 고르지 말고, 어느 쪽을 택했고 왜인지
  README에 기록한 뒤 오너 확인을 요청한다.

## 검증

```bash
python -m app.cli.bootstrap --db deckout.db      # 마이그레이션 + 시드 + 발행
python -m app.cli.check_content --db deckout.db  # §10.5 검증 패스
pytest
```

스키마를 바꾸면 `EXPECTED_SCHEMA_VERSION`을 올리고 `MIGRATIONS`에 항목을
추가한다 — `schema.sql`은 `CREATE TABLE IF NOT EXISTS`라 기존 테이블을 절대
변경하지 않는다.
