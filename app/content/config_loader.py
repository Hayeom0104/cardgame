"""`config/*.toml` 을 읽어 §15 밸런싱 상수로 만든다.

## 왜 파일로 나누어 두는가

§15의 모든 수치는 "설정/DB에서 읽으며 대시보드에서 편집 가능하고, 코드에
하드코딩하지 않는다"가 원칙이다. 그 원본을 게임 플레이 영역별 파일로 나누어
`config/` 아래에 둔다 — 뽑기 확률만 만지고 싶은 사람이 전투 수식을 지나칠
일이 없어야 한다.

## 흐름

    config/*.toml  ──load_constants()──▶  balancing_constants (콘텐츠 버전에 고정)
                   ──load_docs()───────▶  balancing_metadata  (설명, 버전 무관)

파일은 *씨앗*일 뿐이다. 엔진은 절대 이 모듈을 거쳐 값을 읽지 않고, 언제나
`Balance`를 통해 DB에서 읽는다. 그래야 진행 중인 런이 §10.6대로 시작할 때의
수치를 그대로 유지한다 — 파일을 고쳐도 이미 돌고 있는 런은 흔들리지 않는다.

## 파일 규칙

* 설정 이름 바로 위의 `#` 주석 묶음이 그 설정의 **설명**이다. 코드를 몰라도
  무엇을 바꾸는 값인지 알 수 있게 쓴다. 이 주석은 `load_docs()`가 그대로
  읽어가므로 파일과 대시보드의 설명이 갈라지지 않는다.
* `─`, `=` 로만 이루어진 구분선 주석은 설명이 아니라 눈에 보이는 칸막이로
  취급해 건너뛴다.
* TOML에는 빈 값이 없다. "제한 없음 / 해당 없음"을 나타내려면 `"없음"` 이라고
  적는다 (`NONE_SENTINEL`).
* TOML 문법상 `[표]` 머리글이 한 번 나오면 그 뒤의 `키 = 값` 은 모두 그 표의
  소속이 된다. 따라서 각 파일은 **낱개 설정을 먼저, 표 설정을 나중에** 적는다.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

#: `config/` 디렉터리. 저장소 루트 기준이며, `DECKOUT_CONFIG_DIR` 로 바꿀 수 있다.
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

#: TOML에 없는 "빈 값"을 나타내는 약속된 문자열. 로드할 때 `None` 이 된다.
NONE_SENTINEL = "없음"

#: 설명이 아니라 칸막이인 주석 (예: `# ─────────────`).
_RULE_CHARACTERS = set("-─=—_·* ")


class ConfigError(RuntimeError):
    """설정 파일이 깨졌을 때. 서비스는 기동을 거부한다 (fail closed)."""


def config_dir() -> Path:
    import os

    override = os.environ.get("DECKOUT_CONFIG_DIR")
    return Path(override) if override else CONFIG_DIR


def config_files() -> list[Path]:
    """`config/` 안의 모든 `.toml` 을 파일명 순서로 돌려준다.

    파일명 앞의 번호(`01_`, `02_` …)는 순서를 눈에 보이게 하려는 것뿐이고,
    설정 이름은 파일 전체에서 유일해야 하므로 순서가 값을 바꾸지는 않는다.
    """
    directory = config_dir()
    if not directory.is_dir():
        raise ConfigError(f"설정 디렉터리가 없습니다: {directory}")
    files = sorted(directory.glob("*.toml"))
    if not files:
        raise ConfigError(f"설정 파일이 하나도 없습니다: {directory}")
    return files


# =====================================================================
# 값
# =====================================================================
def _resolve(value: Any) -> Any:
    """`"없음"` 을 `None` 으로 바꾼다. 중첩된 표와 배열 안까지 훑는다."""
    if isinstance(value, str):
        return None if value == NONE_SENTINEL else value
    if isinstance(value, dict):
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return value


def load_constants() -> dict[str, Any]:
    """모든 설정 파일을 하나의 평평한 `{설정 이름: 값}` 으로 합친다.

    두 파일이 같은 설정을 정의하면 어느 쪽이 이겼는지 알 수 없으므로 거부한다.
    """
    merged: dict[str, Any] = {}
    owner: dict[str, str] = {}
    for path in config_files():
        try:
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path.name}: TOML 문법 오류 — {error}") from error
        for key, value in parsed.items():
            if key in merged:
                raise ConfigError(
                    f"설정 {key!r} 이(가) {owner[key]} 와(과) {path.name} 양쪽에 "
                    "정의되어 있습니다. 한 곳에만 두세요."
                )
            merged[key] = _resolve(value)
            owner[key] = path.name
    return merged


def source_files() -> dict[str, str]:
    """`{설정 이름: 그 설정이 정의된 파일 이름}`."""
    origins: dict[str, str] = {}
    for path in config_files():
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        for key in parsed:
            origins.setdefault(key, path.name)
    return origins


# =====================================================================
# 설명
# =====================================================================
def _is_rule(comment: str) -> bool:
    return not comment or set(comment) <= _RULE_CHARACTERS


def parse_docs(text: str) -> dict[str, str]:
    """TOML 원문에서 `설정 이름 → 바로 위 주석 묶음` 을 뽑아낸다.

    주석 묶음은 빈 줄로 끊긴다. 즉 설명은 설정 바로 위에 붙어 있어야 하며,
    사이에 빈 줄이 있으면 그 주석은 (칸막이나 문단 설명으로 보고) 버린다.

    이름은 TOML에 적힌 그대로의 점 표기를 쓴다. `[표]` 안의 항목은
    `표.항목` 으로 기록되므로, 설정 하나하나에 설명을 붙일 수 있다.
    """
    docs: dict[str, str] = {}
    buffer: list[str] = []
    table: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            buffer = []
            continue
        if line.startswith("#"):
            comment = line.lstrip("#").strip()
            if not _is_rule(comment):
                buffer.append(comment)
            continue

        name: str | None = None
        if line.startswith("[["):
            table = name = line[2:].split("]")[0].strip().strip('"')
        elif line.startswith("["):
            table = name = line[1:].split("]")[0].strip().strip('"')
        elif "=" in line:
            key = line.split("=")[0].strip().strip('"')
            name = f"{table}.{key}" if table else key

        if name and name not in docs and buffer:
            docs[name] = "\n".join(buffer)
        buffer = []

    return docs


def load_docs() -> dict[str, str]:
    """모든 설정 파일의 설명을 하나로 합친다."""
    docs: dict[str, str] = {}
    for path in config_files():
        for key, text in parse_docs(path.read_text(encoding="utf-8")).items():
            docs.setdefault(key, text)
    return docs


# =====================================================================
# 점검
# =====================================================================
def undocumented() -> list[str]:
    """설명이 붙지 않은 설정 이름. 비어 있어야 정상이다."""
    docs = load_docs()
    return sorted(key for key in load_constants() if key not in docs)
