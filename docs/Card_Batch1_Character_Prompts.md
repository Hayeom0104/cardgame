# Deckout 1차 신규 캐릭터 — NovelAI 태그 프롬프트

신규 캐릭터 파일은 `assets/characters/<character_id>.png`에 넣는다. 게임 표시
크기는 140×140이므로 얼굴과 상반신의 실루엣이 작은 크기에서도 분명해야 한다.

## 공통 품질 태그

각 프롬프트 맨 앞에 붙인다.

```text
masterpiece, best quality, anime game character concept art, solo, full body,
standing, centered composition, clean silhouette, simple dark gradient background,
soft cel shading, restrained lighting, fantasy adventurer, no text
```

공통 네거티브 프롬프트:

```text
worst quality, low quality, blurry, bad anatomy, extra fingers, missing fingers,
extra limbs, duplicate, multiple views, cropped head, cropped feet, text, logo,
watermark, signature, busy background, excessive particles, overexposed,
photorealistic, 3d render, nsfw
```

## 파이라 — `char_pyra`

- 속성/역할/성급: 화 · 디버퍼형 · 1★
- 핵심 인상: 불꽃 자체보다 재와 약화 마법을 다루는 침착한 화염술사

```text
adult woman, ash red bob cut, amber eyes, calm expression, dark charcoal mage coat,
muted red lining, short layered cape, leather gloves, small bronze censer in one hand,
thin wand in the other hand, faint ember smoke curling around the censer,
practical travel boots, red and charcoal color palette, minimal ornament
```

## 마레아 — `char_marea`

- 속성/역할/성급: 수 · 방어형 · 2★
- 핵심 인상: 물의 장막으로 동료를 지키는 수호 기사

```text
adult man, short teal hair, blue gray eyes, composed expression, navy light armor,
silver trim, broad round shield made of translucent water, compact one handed mace,
long blue sash, a few floating water droplets, sturdy defensive stance,
navy teal and silver palette, simple armor design
```

## 제피르 — `char_zephyr`

- 속성/역할/성급: 풍 · 공격형 · 3★
- 핵심 인상: 가벼운 쌍검과 빠른 이동을 사용하는 바람 검사

```text
adult woman, short sage green hair, pale green eyes, focused expression,
light cream and green combat jacket, asymmetric short cape, twin curved daggers,
thin wind ribbons following the blades, fitted fantasy trousers, light boots,
forward ready stance, sage green cream and pale cyan palette, minimal accessories
```

## 녹티스 — `char_noctis`

- 속성/역할/성급: 암 · 딜서포트형 · 2★
- 핵심 인상: 그림자로 공격하면서 동료의 흐름을 보조하는 등불 검사

```text
adult man, dark indigo medium hair, gray violet eyes, reserved expression,
black and indigo long coat, simple layered cloth armor, short straight sword,
small hooded lantern emitting muted violet light, soft shadow ribbon near the feet,
balanced relaxed stance, indigo black and dull silver palette, low ornament
```

## 솔렌 — `char_solenne`

- 속성/역할/성급: 광 · 방어형 · 1★
- 핵심 인상: 눈부신 성녀보다 실전적인 빛의 방패병

```text
adult woman, warm blond bob cut, golden brown eyes, steady expression,
ivory and tan padded armor, modest short mantle, large plain kite shield,
small sun emblem without letters, short staff, faint warm halo behind the shield,
grounded defensive stance, ivory tan and muted gold palette, practical equipment
```

## 저장할 때

- 캐릭터만 보이게 자르고 정사각형 PNG로 저장한다.
- 권장 원본 크기: 1024×1024 이상.
- 배경이 있어도 되지만 인물보다 밝거나 복잡하지 않게 한다.
- 파일명은 위의 `character_id`와 정확히 일치시킨다.
