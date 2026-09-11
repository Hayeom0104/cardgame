# 그림 넣기

여기에 PNG를 두면 게임이 알아서 씁니다. 코드를 고치거나 서비스를 다시 띄울
필요가 없습니다 — 파일을 바꾸면 다음 화면부터 새 그림이 나갑니다.

지금 들어 있는 그림은 **자리를 지키려고 만들어 둔 것**입니다. id 마다 다른
색과 무늬를 갖고 있어서 화면에서 서로 구별은 되지만, 그림이라기보다는
표식에 가깝습니다. 진짜 그림이 나오면 같은 이름으로 덮으면 되고, 한 장씩
바꿔 나가도 됩니다.

    python -m app.cli.make_art              # 아직 없는 것만 만든다
    python -m app.cli.make_art --종류 card   # 카드만
    python -m app.cli.assets 없음            # 무엇이 아직 없는지 본다

`make_art` 는 이미 있는 파일을 덮지 않습니다.

## 어디에 무슨 이름으로 두나요

파일 이름을 콘텐츠의 id 와 똑같이 맞추면 됩니다.

| 폴더 | 무엇 | 예 |
| --- | --- | --- |
| `cards/` | 카드 그림 | `cards/card_화_강타.png` |
| `passives/` | 패시브 카드 그림 | `passives/pas_예리함.png` |
| `characters/` | 캐릭터 초상화 | `characters/char_ignis.png` |
| `enemies/` | 적 그림 | `enemies/enemy_w1_고블린.png` |
| `banners/` | 뽑기 배너의 큰 그림 | `banners/b_limited_01.png` |
| `worlds/` | 지도 화면의 바탕 | `worlds/world_1.png` |
| `equipment/` | 장비 그림 | `equipment/eq_수련검.png` |
| `statuses/` | 상태이상 아이콘 | `statuses/화상.png` |
| `ui/` | 화면 장식 | 이름 자유 |
| `fonts/` | 글꼴 | `fonts/main.ttf` |

어떤 id 가 필요한지는 이렇게 확인합니다.

```
python -m app.cli.assets          # 전체 목록과 넣을 경로
python -m app.cli.assets 없음      # 아직 안 넣은 것만
python -m app.cli.assets 문제      # 넣긴 했는데 못 쓰는 파일만
```

## 그림이 없어도 됩니다

넣지 않은 것은 **이름과 희귀도 색으로 대신 그립니다.** 게임은 정상 동작하니
있는 것부터 하나씩 채워가면 됩니다. 파일이 깨졌거나 너무 커도 마찬가지로
대신 그리고, 위의 `문제` 목록에 나옵니다.

## 크기와 비율

각 종류의 크기는 `config/11_에셋.toml` 에 있습니다. 기본값은 이렇습니다.

| 종류 | 크기 | 맞추는 방식 |
| --- | --- | --- |
| 카드 | 180 × 250 | 꽉 채우고 넘치면 자름 |
| 캐릭터·적 | 140 × 140 | 꽉 채우고 넘치면 자름 |
| 배너 | 860 × 260 | 꽉 채우고 넘치면 자름 |
| 월드 | 900 × 560 | 꽉 채우고 넘치면 자름 |
| 장비 | 96 × 96 | 전부 넣고 남는 자리는 비움 |
| 상태이상 | 32 × 32 | 전부 넣고 남는 자리는 비움 |

넣는 그림의 **비율**을 표의 크기와 비슷하게 맞추면 잘리는 부분이 적습니다.
크기 자체는 커도 됩니다 — 알아서 줄입니다. 다만 한 장에 5 MiB, 한 변 4096
픽셀을 넘으면 쓰지 않습니다.

`.png` 를 권장하지만 `.webp`, `.jpg` 도 됩니다. 같은 이름으로 여러 개가 있으면
`.png` 가 이깁니다.

## 한글이 네모로 나올 때

기본 글꼴로 Neo둥근모 v1.601(`fonts/neodgm.ttf`)가 들어 있습니다. 다른 한글
글꼴을 쓰려면 `.ttf` 파일을 `fonts/main.ttf` 로 넣고
`config/10_화면.toml`의 `font_candidates`에서 그 경로를 맨 위로 옮기면 됩니다.

Neo둥근모는 SIL Open Font License 1.1에 따라 포함했습니다. 전문은
`fonts/NEODGM_LICENSE.txt`에 있고, 원본 프로젝트는
<https://github.com/neodgm/neodgm>입니다.
