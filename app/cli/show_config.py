"""설정을 설명과 함께 훑어본다.

    python -m app.cli.show_config              # 전체
    python -m app.cli.show_config 뽑기          # 파일 이름으로 좁히기
    python -m app.cli.show_config pity          # 설정 이름으로 좁히기

파일을 직접 열지 않고도 어떤 설정이 있고 무엇을 바꾸는 값인지 확인할 수 있다.
"""

from __future__ import annotations

import json
import sys

from app.content import config_loader as cl


def _render(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 68 else json.dumps(value, ensure_ascii=False,
                                                   indent=2)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    needle = argv[0] if argv else ""

    values = cl.load_constants()
    docs = cl.load_docs()
    origins = cl.source_files()

    shown = 0
    for path in cl.config_files():
        keys = [key for key, origin in origins.items() if origin == path.name]
        keys = [key for key in keys
                if not needle or needle in key or needle in path.name]
        if not keys:
            continue
        print(f"\n{'=' * 70}\n  {path.name}\n{'=' * 70}")
        for key in sorted(keys):
            shown += 1
            print(f"\n{key} = {_render(values[key])}")
            for line in docs.get(key, "(설명 없음)").splitlines():
                print(f"    {line}")
            # 표 안쪽 항목에도 설명이 붙어 있으면 함께 보여준다.
            for sub in sorted(k for k in docs if k.startswith(f"{key}.")):
                print(f"    · {sub.split('.', 1)[1]}: "
                      f"{docs[sub].splitlines()[0]}")

    if not shown:
        print(f"'{needle}' 와(과) 맞는 설정이 없습니다.")
        return 1
    print(f"\n설정 {shown}개.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
