# 이 저장소에서 작업할 때

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
