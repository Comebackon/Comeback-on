#!/usr/bin/env python3
"""TMap 키를 이 컴퓨터에만 저장한다. 키를 출력하거나 네트워크로 보내지 않는다."""

import getpass
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
KEY_FILENAME = "tmap_app_key"
PROMPT_SCRIPT = '''text returned of (display dialog ("발급받은 TMap API 키를 붙여넣어 주세요." & return & "이 컴퓨터에 저장되며 GitHub에는 올라가지 않아요.") with title "귀가 ON · TMap 연결" default answer "" buttons {"취소", "저장"} default button "저장" cancel button "취소" with hidden answer)'''
SUCCESS_SCRIPT = '''display dialog ("키를 저장했어요." & return & "실행 중인 귀가 ON 앱을 종료한 뒤 다시 시작해 주세요.") with title "귀가 ON · 등록 완료" buttons {"확인"} default button "확인"'''


def validate_key(value):
    """공급자 키 형식은 추측하지 않고 빈 값·과도한 길이·공백만 걸러낸다."""
    if not isinstance(value, str):
        raise ValueError("키를 다시 붙여넣어 주세요.")
    key = value.strip()
    if not key or len(key) > 512:
        raise ValueError("키가 비어 있거나 너무 길어요. 발급받은 키만 붙여넣어 주세요.")
    if any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("키에 공백·줄바꿈 또는 지원하지 않는 문자가 있어요. 발급받은 키만 붙여넣어 주세요.")
    return key


def data_directory():
    configured = os.environ.get("COMEBACK_DATA_DIR", "").strip()
    return Path(configured).expanduser() if configured else ROOT / "instance"


def save_key(value, directory=None):
    """취소·검증 실패 시 기존 키를 보존하고, 성공 시 0600 파일로 원자 교체한다."""
    key = validate_key(value)
    directory = Path(directory) if directory is not None else data_directory()
    if directory.is_symlink():
        raise ValueError("키 저장 폴더가 바로가기예요. 실제 폴더를 사용해 주세요.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / KEY_FILENAME
    if target.is_symlink():
        raise ValueError("키 저장 파일이 바로가기예요. 실제 파일을 사용해 주세요.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmap-key-", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def prompt_key():
    if sys.platform == "darwin":
        # 명령행 인수에는 고정된 안내문만 있다. 입력한 키는 자식 프로세스의
        # 표준 출력을 메모리로 받아 처리하며 터미널이나 로그로 출력하지 않는다.
        result = subprocess.run(
            ["osascript", "-e", PROMPT_SCRIPT], capture_output=True,
            text=True, encoding="utf-8", check=False,
        )
        if result.returncode:
            if "(-128)" in result.stderr:
                return None
            raise RuntimeError("키 입력창을 열지 못했어요. 터미널에서 다시 실행해 주세요.")
        return result.stdout.removesuffix("\n")
    if not sys.stdin.isatty():
        raise RuntimeError("일반 터미널에서 실행해 주세요.")
    try:
        return getpass.getpass("TMap API 키를 붙여넣고 Enter: ")
    except (EOFError, KeyboardInterrupt):
        return None


def main():
    try:
        value = prompt_key()
        if value is None:
            print("등록을 취소했어요. 기존 키는 바꾸지 않았어요.")
            return 0
        save_key(value)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, RuntimeError):
        # 원본 예외나 입력 값을 출력하지 않는다.
        print("키를 저장하지 못했어요. 프로젝트 폴더의 쓰기 권한과 입력창을 확인해 주세요.", file=sys.stderr)
        return 1
    print("TMap 키를 저장했어요. 실행 중인 앱을 종료한 뒤 다시 시작해 주세요.")
    if sys.platform == "darwin":
        try:
            subprocess.run(["osascript", "-e", SUCCESS_SCRIPT], capture_output=True, check=False)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
