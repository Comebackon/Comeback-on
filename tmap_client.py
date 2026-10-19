"""TMAP 보행자 경로 API 연결 모듈.

TMAP_APP_KEY 환경 변수를 설정한 뒤 request_pedestrian_route()를 호출한다.
키는 저장소에 커밋하지 않는다.
"""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TMAP_PEDESTRIAN_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1"


class TmapAPIError(Exception):
    """TMAP API 호출 실패."""


def request_pedestrian_route(start_lon, start_lat, end_lon, end_lat,
                             start_name="출발지", end_name="목적지"):
    """보행자 경로 GeoJSON 응답을 반환한다. 지도 표시나 안전 분석은 수행하지 않는다."""
    app_key = os.environ.get("TMAP_APP_KEY", "").strip()
    if not app_key:
        raise TmapAPIError("TMAP_APP_KEY 환경 변수가 설정되지 않았습니다.")

    payload = {
        "startX": str(start_lon),
        "startY": str(start_lat),
        "endX": str(end_lon),
        "endY": str(end_lat),
        "startName": start_name,
        "endName": end_name,
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
        "searchOption": "0",
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
