"""허브 화면의 **실행** 버튼들 — 성급 상승, 카드 업그레이드, 연구, 상점, 강화.

이 층이 없는 동안 §4.4 성급 상승, §5.8 카드 업그레이드, §20.5 연구, §7.2 허브
상점, §8.4 장비 강화는 엔진에 전부 구현돼 있으면서도 플레이어가 실행할 방법이
없었다 — 허브 화면이 전부 읽기 전용 목록이었기 때문이다.

§19.2의 `custom_id` 형식은 run_id를 요구하지만 허브는 런 밖이다. 그래서
`dko:prep:` / `dko:gacha:` 와 같은 방식으로 별도 접두사를 쓰고, 다섯
게이트(§1.3.10) 대신 여기에 맞는 검사를 한다.

    · OWNERSHIP  이벤트의 user_id가 곧 대상이다 — 남의 계정을 조작할 수 없다
    · LEGALITY   비용·선행조건·소유 여부는 엔진이 다시 검사한다. 화면이
                 걸러 주더라도 `custom_id`는 위조할 수 있다.
    · 원자성     코인이 드는 조작은 §17의 `deduct` 트랜잭션으로 나간다

**트랜잭션 키는 이벤트에서 유도한다.** 서버는 핸들러가 돌아온 *뒤에*
`processed_events`를 기록하므로(§16.7), 차감 직후 죽으면 중앙봇이 같은 이벤트를
다시 보낸다. 그때 새 키를 만들면 두 번 청구된다 — §17.3 규칙 2가 금지하는
바로 그것이다. 같은 키로 다시 들어가면 `run_transaction`이 기록된 상태에서
재개하므로 중복 청구가 구조적으로 불가능하다.
"""

from __future__ import annotations

import logging

from app.api import errors
from app.engine import attendance as att
from app.engine import progression as pg

logger = logging.getLogger(__name__)

HUB_PREFIX = "dko:hub:"

def _tx_id(event_id: str | None) -> str | None:
    """이 조작의 §17 트랜잭션 키.

    `event_id`가 있으면 그것으로 만든다. 없으면(테스트나 직접 호출) None을
    돌려주어 엔진이 자기 결정적 키를 쓰게 한다 — 그쪽도 재시도에 안전하다.
    """
    return f"evt:{event_id}" if event_id else None


def handle_hub(ctx, user_id: int, custom_id: str, values: list[str], *,
               event_id: str | None = None) -> dict:
    """허브 컴포넌트 제출을 처리하고, 끝나면 화면을 다시 그린다."""
    from app.api import handlers

    payload = custom_id[len(HUB_PREFIX):]
    action, _, argument = payload.partition(":")
    chosen = values[0] if values else argument

    # 장착만 두 단계다 — 장비를 고른 다음 대상 캐릭터를 고른다. 한 컴포넌트에
    # 둘을 담을 수 없어서, 첫 단계는 결과가 아니라 다음 화면을 돌려준다.
    if action == "equip":
        try:
            return equip_prompt(ctx, user_id, int(chosen))
        except ValueError:
            return {"action": "reply_ephemeral", "content": errors.ILLEGAL_STATE}

    # §5.3a 카드 도감 — 읽기 전용이라 재화도 게이트도 없다. `_dispatch`의
    # 메시지+새로고침 모양이 아니라 완성된 화면 하나를 그대로 돌려준다.
    if action == "catalog":
        from app.api import handlers

        return {**handlers.catalog_screen(ctx, user_id), "action": "edit"}

    try:
        message, refresh = _dispatch(ctx, user_id, action, chosen, argument,
                                     event_id=event_id)
    except pg.ProgressionError as reason:
        # 평범한 거절(재화 부족, 선행 조건 미달)이다. 화면은 그대로 두고
        # 사유만 알린다 — 목록이 사라지면 무엇이 부족했는지 확인할 수 없다.
        logger.info("hub %s rejected for %s: %s", action, user_id, reason)
        return {"action": "reply_ephemeral", "content": str(reason)}
    except ValueError:
        return {"action": "reply_ephemeral", "content": errors.ILLEGAL_STATE}

    if refresh is None:
        return {"action": "reply_ephemeral", "content": message}

    screen = getattr(handlers, refresh)(ctx, user_id)
    return {**screen, "action": "edit",
            "content": f"{message}\n\n{screen.get('content', '')}"}


def _dispatch(ctx, user_id: int, action: str, chosen: str, argument: str, *,
              event_id: str | None) -> tuple[str, str | None]:
    version = ctx.content_version_id

    if action == "starup":
        result = pg.star_up(ctx.db, ctx.balance, ctx.central, user_id=user_id,
                            character_id=chosen, content_version_id=version,
                            tx_id=_tx_id(event_id))
        return _settled(result, "성급을 올렸습니다."), "characters_screen"

    if action == "cardup":
        result = pg.upgrade_card(ctx.db, ctx.central, user_id=user_id,
                                 card_id=chosen, content_version_id=version,
                                 tx_id=_tx_id(event_id))
        return _settled(result, "카드를 강화했습니다."), "deck_screen"

    if action == "research":
        result = pg.unlock_research(ctx.db, ctx.balance, ctx.central,
                                    user_id=user_id, node_id=chosen,
                                    content_version_id=version,
                                    tx_id=_tx_id(event_id))
        return _settled(result, "연구를 해금했습니다."), "research_screen"

    if action == "buyequip":
        result = pg.buy_hub_equipment(ctx.db, ctx.balance, ctx.central,
                                      user_id=user_id, equipment_def_id=chosen,
                                      content_version_id=version,
                                      tx_id=_tx_id(event_id))
        return _settled(result, "장비를 구매했습니다."), "hub_shop_screen"

    if action == "buystone":
        result = pg.buy_hub_stone(ctx.db, ctx.balance, ctx.central,
                                  user_id=user_id, tier=int(chosen),
                                  tx_id=_tx_id(event_id))
        return _settled(result, "강화석을 구매했습니다."), "hub_shop_screen"

    if action == "daily":
        # 출석은 날짜에서 유도한 키를 쓰므로(§17.3 규칙 2) 이벤트 키를
        # 넘기지 않는다 — 같은 날의 두 번째 클릭도 같은 키여야 한다.
        result = att.claim(ctx.db, ctx.balance, ctx.central, user_id=user_id)
        if result.already_claimed:
            return "오늘 출석 보상은 이미 받았습니다.", None
        got = [f"코인 {result.coin}"] if result.coin else []
        if result.carta:
            got.append(f"카르타 {result.carta}")
        if result.wildcards:
            got.append(f"와일드카드 {result.wildcards}")
        message = f"{result.streak}일째 출석 — {' · '.join(got)}"
        if result.coin_result is not None:
            message = _settled(result.coin_result, message)
        return message, None

    # -- 여기서부터는 순수 로컬이다. Central 왕복이 필요 없다. -----------
    if action == "enhance":
        outcome = pg.enhance_equipment(
            ctx.db, ctx.balance, user_id=user_id,
            equipment_instance_id=int(chosen), content_version_id=version)
        return f"장비를 T{outcome['tier']}로 강화했습니다.", "equipment_screen"

    if action == "equipto":
        pg.equip(ctx.db, user_id=user_id, equipment_instance_id=int(argument),
                 character_id=chosen, content_version_id=version)
        # §16.2.3 — 진행 중인 런은 빌드 스냅샷을 쓰므로 다음 런부터 반영된다.
        return "장착했습니다. 진행 중인 런에는 다음 런부터 반영됩니다.", \
            "equipment_screen"

    raise ValueError(f"unknown hub action {action!r}")


def _settled(result, success: str) -> str:
    """§17 트랜잭션 결과를 플레이어의 말로 옮긴다.

    성공이 아닌 종료 상태를 성공처럼 보여주면 안 된다 — 코인이 빠졌는지
    돌아왔는지가 플레이어에게는 가장 중요한 정보다.
    """
    from app.central import transactions as tx

    if result.status == tx.COMPLETED:
        return success
    if result.status == tx.REJECTED_NO_CHARGE:
        return "코인이 부족해 취소했습니다. 청구되지 않았습니다."
    if result.status == tx.COMPENSATED:
        return "처리에 실패해 코인을 돌려드렸습니다."
    return f"처리 중입니다. ({result.status})"


def equip_prompt(ctx, user_id: int, instance_id: int) -> dict:
    """장착 2단계 — 어느 캐릭터에게 채울지 고른다."""
    characters = ctx.db.query(
        "SELECT oc.character_id, c.name FROM owned_characters oc "
        "JOIN characters c ON c.character_id = oc.character_id "
        "AND c.content_version_id = ? WHERE oc.user_id = ? ORDER BY c.name",
        (ctx.content_version_id, user_id))
    if not characters:
        return {"action": "reply_ephemeral", "content": "보유한 캐릭터가 없습니다."}
    return {
        "action": "edit",
        "content": "장착할 캐릭터를 고르세요.",
        "components": [{
            "type": "string_select",
            "custom_id": f"{HUB_PREFIX}equipto:{instance_id}",
            "placeholder": "캐릭터",
            "options": [{"label": row["name"], "value": row["character_id"]}
                        for row in characters[:25]],
        }],
    }
