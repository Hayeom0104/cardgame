# main.ttf 출처

배포 환경에 한글 글꼴이 하나도 없으면 카드 이름 등 모든 한글이 네모(□)로
깨져 나온다 — `app/render/theme.py`의 `_load_font()`가 이미 이 경우를
경고하도록 되어 있었지만, `assets/fonts/main.ttf` 자리가 비어 있어서
실제로는 배포 서버에 설치된 시스템 글꼴에 의존하고 있었다.

이 파일은 **WenQuanYi Zen Hei**(문泉驿正黑, Debian 패키지
`fonts-wqy-zenhei`)에서 그대로 가져온 것이다. 한글을 포함한 CJK
글리프를 갖추고 있고, GPL-2 (font embedding exception 포함) +
M+ FONTS License로 배포·임베드가 자유롭다.

- 출처: http://wenq.org/ (Debian 패키지 `fonts-wqy-zenhei` 0.9.45-8)
- 라이선스: GPL-2 with Font embedding exception, M+ FONTS License
- 이 글꼴을 쓰는 문서/이미지 자체는 GPL의 영향을 받지 않는다
  (embedding exception 조항).

더 나은 한글 글꼴(예: Nanum Gothic, Pretendard 등)로 바꾸고 싶으면 이
파일을 원하는 `.ttf`로 교체하면 된다 — `config/10_화면.toml`의
`font_candidates` 목록 맨 위가 이 경로를 가리킨다.
