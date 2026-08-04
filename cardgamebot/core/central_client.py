"""중앙봇 Economy/XP/업적 API 클라이언트 (설계 문서 §1.1).

이 봇은 코인·XP·업적을 자체 DB에 저장하지 않는다. 전부 중앙봇 소유이고
HTTP API 로만 접근한다 (중앙봇 가이드 §1 "DB 직접 접근 금지").

엔드포인트 경로는 설계 문서 §1.1 표에 적힌 것을 그대로 따랐다::

    GET  /v1/users/{user_id}
    POST /v1/currency/add
    POST /v1/xp/add
    POST /v1/achievements/grant

⚠️ 요청/응답 **본문 스키마**는 설계 문서에 없다. 실제 필드명은
`중앙봇_API_연동_가이드_업데이트.md` 와 대조해 확정해야 한다. 그때 고칠 곳이
이 파일 하나가 되도록 파싱을 이곳에 가뒀다.

`central_api_enabled=False` 이면 로컬 스텁으로 동작해, 중앙봇 없이도 개발과
테스트가 가능하다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


class CentralAPIError(Exception):
    """중앙봇 호출 실패. 호출부는 게임 진행을 막지 않고 안내만 해야 한다."""


@dataclass
class CentralUser:
    user_id: str
    balance: int
    xp: int
    level: int


class CentralBotClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.central_api_base).rstrip("/")
        self.token = token or settings.central_api_token
        self.timeout = settings.central_api_timeout
        self.enabled = settings.central_api_enabled
        # 중앙봇이 꺼져 있을 때 쓰는 인메모리 스텁 잔액.
        self._stub_balance: dict[str, int] = {}

    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, url, headers=self._headers(), **kwargs)
                resp.raise_for_status()
                return resp.json() if resp.content else {}
        except httpx.HTTPError as exc:
            log.warning("중앙봇 API 호출 실패: %s %s — %s", method, url, exc)
            raise CentralAPIError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 코인 / XP (§1.1)
    # ------------------------------------------------------------------

    async def get_user(self, user_id: str) -> CentralUser:
        if not self.enabled:
            return CentralUser(user_id, self._stub_balance.get(user_id, 0), 0, 1)
        data = await self._request("GET", f"/v1/users/{user_id}")
        payload = data.get("data", data)
        return CentralUser(
            user_id=str(payload.get("user_id", user_id)),
            balance=int(payload.get("balance", 0)),
            xp=int(payload.get("xp", 0)),
            level=int(payload.get("level", 1)),
        )

    async def get_balance(self, user_id: str) -> int:
        return (await self.get_user(user_id)).balance

    async def add_currency(self, user_id: str, amount: int, reason: str = "") -> int:
        """코인을 더한다(음수면 차감). 변경 후 잔액을 반환한다."""
        if not self.enabled:
            new_balance = self._stub_balance.get(user_id, 0) + amount
            self._stub_balance[user_id] = new_balance
            return new_balance
        data = await self._request(
            "POST",
            "/v1/currency/add",
            json={"user_id": user_id, "amount": amount, "reason": reason},
        )
        payload = data.get("data", data)
        return int(payload.get("balance", 0))

    async def spend_currency(self, user_id: str, amount: int, reason: str = "") -> int:
        """코인을 차감한다. 잔액 부족이면 CentralAPIError."""
        balance = await self.get_balance(user_id)
        if balance < amount:
            raise CentralAPIError(f"코인이 부족합니다. (필요 {amount:,}, 보유 {balance:,})")
        return await self.add_currency(user_id, -amount, reason)

    async def add_xp(self, user_id: str, amount: int, reason: str = "") -> None:
        if not self.enabled:
            return
        await self._request(
            "POST", "/v1/xp/add", json={"user_id": user_id, "amount": amount, "reason": reason}
        )

    # ------------------------------------------------------------------
    # 업적 (§1.1)
    # ------------------------------------------------------------------

    async def grant_achievement(self, user_id: str, achievement_code: str) -> None:
        if not self.enabled:
            return
        await self._request(
            "POST",
            "/v1/achievements/grant",
            json={"user_id": user_id, "achievement": achievement_code},
        )


_client: CentralBotClient | None = None


def get_central_client() -> CentralBotClient:
    global _client
    if _client is None:
        _client = CentralBotClient()
    return _client
