"""TMAP 보행자 경로 API 연결 모듈.

setup-tmap.command로 저장한 로컬 키 또는 TMAP_APP_KEY 환경 변수를 사용한다.
키는 저장소에 커밋하지 않고 브라우저에도 전달하지 않는다.
"""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TMAP_PEDESTRIAN_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1"


class TmapAPIError(Exception):
    """TMAP API 호출 실패."""


def get_app_key():
    """맥 입력창에 저장한 키를 우선 읽고, 없으면 실행 환경의 키를 사용한다."""
    directory = Path(os.environ.get("COMEBACK_DATA_DIR", "").strip() or Path(__file__).resolve().parent / "instance").expanduser()
    try:
        with (directory / "tmap_app_key").open(encoding="utf-8") as stream:
            saved = stream.read(1025).strip()
        if 1 <= len(saved) <= 512 and all(33 <= ord(c) <= 126 for c in saved):
            return saved
    except (OSError, UnicodeError):
        pass
    value = os.environ.get("TMAP_APP_KEY", "").strip()
    return value if 1 <= len(value) <= 512 and all(33 <= ord(c) <= 126 for c in value) else ""


def request_pedestrian_route(start_lon, start_lat, end_lon, end_lat,
                             start_name="출발지", end_name="목적지", search_option="0"):
    """보행자 경로 GeoJSON 응답을 반환한다. 지도 표시나 안전 분석은 수행하지 않는다."""
    # TMAP 공식 보행자 옵션: 0 추천, 10 최단, 30 최단거리+계단제외.
    # 기존 호출은 0을 유지하며 모바일 앱에서 명시적으로 10/30을 선택한다.
    if str(search_option) not in {"0", "4", "10", "30"}:
        raise TmapAPIError("지원하지 않는 보행자 경로 옵션입니다.")
    app_key = get_app_key()
    if not app_key:
        raise TmapAPIError("TMap 키가 없습니다. setup-tmap.command로 등록하거나 TMAP_APP_KEY를 설정해 주세요.")

    payload = {
        "startX": str(start_lon),
        "startY": str(start_lat),
        "endX": str(end_lon),
        "endY": str(end_lat),
        "startName": start_name,
        "endName": end_name,
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
        "searchOption": str(search_option),
    }
    request = Request(
        TMAP_PEDESTRIAN_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "appKey": app_key,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # 서버 응답에 인증 정보가 포함될 가능성이 있어 출력 전에 마스킹한다.
        detail = detail.replace(app_key, "[REDACTED]")
        raise TmapAPIError(
            f"TMAP API HTTP 오류: {exc.code}\n"
            f"상세 내용: {detail[:500]}"
        ) from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise TmapAPIError(f"TMAP API 연결 또는 응답 오류: {exc}") from exc
