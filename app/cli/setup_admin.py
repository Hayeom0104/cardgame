"""Enable the dashboard in the existing project .env without touching game data."""

from getpass import getpass
from pathlib import Path
import secrets

from dotenv import dotenv_values, set_key


def configure(path: Path, password: str) -> None:
    if not path.is_file():
        raise ValueError("기존 .env 파일을 찾지 못했습니다. 프로젝트 설정을 먼저 확인하세요.")
    if not password.strip() or any(c in password for c in "\r\n\x00"):
        raise ValueError("비밀번호는 비어 있거나 줄바꿈을 포함할 수 없습니다.")
    values = dotenv_values(path, interpolate=False)
    # dotenv expands ${...} while loading config. Refuse it instead of saving a
    # password that would silently change on the next process start.
    if "${" in password:
        raise ValueError("비밀번호에 ${ 문자열을 사용할 수 없습니다.")
    path.chmod(0o600)
    set_key(str(path), "DECKOUT_ADMIN_PASSWORD", password)
    if not values.get("DECKOUT_ADMIN_SECRET"):
        set_key(str(path), "DECKOUT_ADMIN_SECRET", secrets.token_urlsafe(48))
    path.chmod(0o600)


def main() -> int:
    path = Path(__file__).resolve().parents[2] / ".env"
    try:
        password = getpass("관리자 비밀번호 (입력 내용 숨김): ")
        if password != getpass("비밀번호 확인: "):
            raise ValueError("비밀번호가 일치하지 않습니다. 저장하지 않았습니다.")
        configure(path, password)
    except (ValueError, OSError) as error:
        print(f"중단: {error}")
        return 1
    print("관리자 설정 저장 완료. 서버를 재시작하세요.")
    print("서버를 실행하는 기기: http://127.0.0.1:8081/admin/")
    print("다른 기기: http://<서버 기기의 Tailscale IP>:8081/admin/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
