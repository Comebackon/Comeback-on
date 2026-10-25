"""모바일 웹의 장소 검색·공공시설 조회·실제 도로망 경로 비교.

TMAP_APP_KEY: 기존 TMAP 키. 최단은 옵션 10, 계단 제외 최단은 30.
ROUTING_GRAPH_FILE: 검증한 보행 도로망 GeoJSON 파일(선택).

도로망이 없으면 최단·대로우선 TMAP 경로 두 개 안에서만 후보를 비교한다.
두 후보의 내부 교차점을 임의로 합치지 않으며, 전체 도로망의 최적해를 주장하지 않는다.
계단 제외 시에는 옵션 4가 계단 제외를 보장하지 않으므로 후보 비교를 생략한다.

도로망 파일은 FeatureCollection이며 각 LineString은 WGS84 [경도,위도]를 쓴다.
properties에 from/to(교차점 ID), walkable=true가 필수다. 교차점 ID가 같을 때만
연결하므로 교량/지하도 교차를 임의로 연결하지 않는다. oneway=true, stairs=true를
지원한다. verified_attention=true와 attention_reason이 함께 있을 때만
attention_penalty_s(0~600초)를 적용한다. 파일이 없거나 연결되지 않으면 TMAP
최단 경로만 반환한다. 데이터 부족으로 위험 점수를 부여하거나 가짜 대안을 만들지 않는다.

공식 API 문서:
https://tmap-skopenapi.readme.io/reference/보행자-경로안내
https://tmap-skopenapi.readme.io/reference/장소통합검색
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
import heapq
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from tmap_client import TmapAPIError, request_pedestrian_route


DATA_DIR = Path(__file__).resolve().parent / "data"
SEARCH_URL = "https://apis.openapi.sk.com/tmap/pois"
MAX_ROUTE_DISTANCE_M = 30000
MAX_GRAPH_EDGES = 50000
MAX_SNAP_M = 30
ALIASES = {"미림마이스터고": "미림마이스터고등학교", "미림마이스터": "미림마이스터고등학교", "미림마고": "미림마이스터고등학교"}
_search_cache = {}
_search_lock = threading.Lock()


class RoutingError(ValueError):
    """사용자에게 그대로 보여도 되는 경로/입력 오류."""


def _number(value, label, minimum, maximum):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or not minimum <= result <= maximum:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        raise RoutingError(f"{label}: {minimum}~{maximum} 범위의 숫자를 입력해 주세요.") from None


def _coord(lat, lon):
    return (_number(lat, "위도", -90, 90), _number(lon, "경도", -180, 180))


def distance_m(a, b):
    """두 위경도 점의 지표면 직선 거리. 보행 경로를 대신하지 않는다."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742000 * math.asin(min(1, math.sqrt(x)))


def _provider_error():
    return RoutingError("장소·경로 서비스에 연결하지 못했어요. 잠시 후 다시 시도해 주세요. 계속되면 서버의 TMAP 키와 상품 등록 상태를 확인해 주세요.")


def search_places(query):
    """사용자가 검색을 제출했을 때 TMAP 장소 검색. 최대 5개, 캐시 10분."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
        raise RoutingError("검색어를 1~100자로 입력해 주세요.")
    query = ALIASES.get(re.sub(r"\s+", "", query), query.strip())
    key = os.environ.get("TMAP_APP_KEY", "").strip()
    if not key:
        raise RoutingError("장소 검색 연결 준비 중이에요. 서버에 TMAP_APP_KEY를 등록하면 검색할 수 있어요.")
    now = time.monotonic()
    with _search_lock:
        cached = _search_cache.get(query)
        if cached and cached[0] > now:
            return [dict(item) for item in cached[1]]
    request = Request(SEARCH_URL + "?" + urlencode({"version": "1", "searchKeyword": query, "searchType": "all", "count": "5", "page": "1", "resCoordType": "WGS84GEO"}), headers={"appKey": key, "Accept": "application/json", "User-Agent": "Comeback-on/1.0"})
    try:
        with urlopen(request, timeout=10) as response:
            raw = json.loads(response.read(1024 * 1024))
        pois = raw.get("searchPoiInfo", {}).get("pois", {}).get("poi", [])
        if not isinstance(pois, list):
            raise ValueError
        result = []
        for poi in pois[:5]:
            # 보행자 입구 좌표를 우선하고 없으면 대표점 좌표를 사용한다.
            lat = poi.get("pnsLat") or poi.get("noorLat") or poi.get("frontLat")
            lon = poi.get("pnsLon") or poi.get("noorLon") or poi.get("frontLon")
            try:
                lat, lon = _coord(lat, lon)
            except RoutingError:
                continue
            if not (32 <= lat <= 39 and 123 <= lon <= 133):
                continue
            new_addresses = poi.get("newAddressList", {}).get("newAddress", [])
            address = (new_addresses[0].get("fullAddressRoad", "") if new_addresses else "")
            if not address:
                address = " ".join(str(poi.get(field) or "") for field in ("upperAddrName", "middleAddrName", "lowerAddrName", "detailAddrName")).strip()
            result.append({"name": str(poi.get("name") or query)[:150], "address": str(address)[:250], "lat": lat, "lon": lon})
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise _provider_error() from None
    with _search_lock:
        # Bound process memory; TMAP responses are never cached beyond 24 hours.
        if len(_search_cache) >= 256:
            _search_cache.clear()
        _search_cache[query] = (now + 600, result)
    return [dict(item) for item in result]


def _find_column(columns, candidates):
    normalized = {str(col).strip().lower(): col for col in columns}
    for candidate in candidates:
        if candidate.lower() in normalized:
            return normalized[candidate.lower()]
    return None


def _read_csv(path):
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=encoding, low_memory=False)
        except UnicodeError:
            continue
    return pd.DataFrame()


@lru_cache(maxsize=1)
def _facilities():
    """서버 시작 후 한 번만 CSV를 읽어 0.005도 격자 인덱스로 분류한다."""
    rows, buckets = [], defaultdict(list)
    for path in sorted(DATA_DIR.glob("*.csv")):
        name = path.name.lower()
        kind = next((label for token, label in (("cctv", "CCTV"), ("가로등", "가로등"), ("보안등", "가로등"), ("비상벨", "비상벨"), ("안전시설물", "안전시설물")) if token in name), None)
        if kind is None:
            continue
        try:
            frame = _read_csv(path)
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        lat_col = _find_column(frame.columns, ("WGS84위도", "위도", "lat", "latitude"))
        lon_col = _find_column(frame.columns, ("WGS84경도", "경도", "lon", "longitude"))
        if lat_col is None or lon_col is None:
            continue
        frame["_lat"] = pd.to_numeric(frame[lat_col], errors="coerce")
        frame["_lon"] = pd.to_numeric(frame[lon_col], errors="coerce")
        frame = frame[frame["_lat"].between(32, 39) & frame["_lon"].between(123, 133)]
        address_cols = [col for col in ("소재지도로명주소", "소재지지번주소", "소재지번주소", "도로명주소", "주소", "세부위치", "설치위치", "address") if col in frame.columns]
        for index, row in zip(frame.index, frame.to_dict("records")):
            address = next((str(row[col]).strip() for col in address_cols if pd.notna(row[col]) and str(row[col]).strip()), "")
            title = str(row.get("비고", "")) if pd.notna(row.get("비고", "")) else ""
            subtype = kind
            # Only explicit facility names identify a store/police facility. The
            # managing police station is not the physical facility itself.
            if kind == "안전시설물" and title:
                if any(token in title for token in ("파출소", "지구대", "치안센터", "경찰서")):
                    subtype = "경찰시설"
                elif any(token in title.upper() for token in ("CU", "GS25", "세븐일레븐", "이마트24", "미니스톱")):
                    subtype = "편의점"
            record = {"id": f"{path.stem}:{index}", "name": title or subtype, "type": subtype, "lat": float(row["_lat"]), "lon": float(row["_lon"]), "address": address, "source": path.name}
            rows.append(record)
            buckets[(math.floor(record["lat"] / .005), math.floor(record["lon"] / .005))].append(len(rows) - 1)
    return rows, dict(buckets)


def nearby_facilities(lat, lon, radius=500, limit=200):
    lat, lon = _coord(lat, lon)
    radius = _number(radius, "조회 반경(m)", 50, 2000)
    rows, buckets = _facilities()
    dlat, dlon = radius / 110540, radius / (111320 * max(.01, math.cos(math.radians(lat))))
    found = []
    for row in range(math.floor((lat - dlat) / .005), math.floor((lat + dlat) / .005) + 1):
        for col in range(math.floor((lon - dlon) / .005), math.floor((lon + dlon) / .005) + 1):
            for index in buckets.get((row, col), ()):
                item = rows[index]
                dist = distance_m((lat, lon), (item["lat"], item["lon"]))
                if dist <= radius:
                    found.append({**item, "distance_m": round(dist)})
    found.sort(key=lambda item: item["distance_m"])
    return found[:limit]


def route_evidence(coordinates):
    """경로 50m 주변에 CSV에 등록된 시설 수. 미등록=위험으로 해석하지 않는다."""
    observed = {}
    # Sample actual route segments at <=50m spacing. Cap to a 30km walking trip.
    for a, b in zip(coordinates, coordinates[1:]):
        count = max(1, math.ceil(distance_m(a, b) / 50))
        for step in range(count):
            ratio = step / count
            sample = (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)
            for facility in nearby_facilities(*sample, radius=50, limit=500):
                observed[facility["id"]] = facility
    for facility in nearby_facilities(*coordinates[-1], radius=50, limit=500):
        observed[facility["id"]] = facility
    counts = Counter(item["type"] for item in observed.values())
    reasons = [f"경로 주변 50m의 등록 {kind} {counts[kind]}곳을 확인했어요." for kind in ("CCTV", "가로등", "비상벨", "경찰시설", "편의점", "안전시설물") if counts[kind]]
    if not reasons:
        reasons = ["이 경로 주변에서 확인할 수 있는 등록 시설 정보가 부족해요. 위험하거나 안전하다는 뜻은 아니에요."]
    return dict(counts), reasons


@dataclass
class Edge:
    target: str
    coordinates: list
    distance: float
    duration: float
    cost: float
    stairs: bool = False
    reason: str = ""


def build_graph(document, walk_speed=1.2):
    """검증된 LineString 보행 링크만 그래프로 연결. 교차점을 추정하지 않는다."""
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise RoutingError("보행 도로망 파일 형식을 확인해 주세요.")
    features = document.get("features", [])
    if not isinstance(features, list) or len(features) > MAX_GRAPH_EDGES:
        raise RoutingError("보행 도로망의 구간 수를 확인해 주세요.")
    graph, nodes = defaultdict(list), {}
    for feature in features:
        if not isinstance(feature, dict):
            raise RoutingError("보행 도로망 구간 형식을 확인해 주세요.")
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if not isinstance(props, dict) or not isinstance(geometry, dict):
            raise RoutingError("보행 도로망 구간 형식을 확인해 주세요.")
        if geometry.get("type") != "LineString" or props.get("walkable") is not True:
            continue
        if props.get("access") in ("no", "private") or props.get("foot") == "no":
            continue
        start, end = str(props.get("from", "")), str(props.get("to", ""))
        if not start or not end or start == end:
            continue
        raw_coords = geometry.get("coordinates", [])
        if not isinstance(raw_coords, list) or not 2 <= len(raw_coords) <= 10000:
            raise RoutingError("보행 도로망 구간 좌표를 확인해 주세요.")
        try:
            coords = [list(_coord(point[1], point[0])) for point in raw_coords]
        except (IndexError, TypeError):
            raise RoutingError("보행 도로망 구간 좌표를 확인해 주세요.") from None
        for name, point in ((start, coords[0]), (end, coords[-1])):
            if name in nodes and distance_m(nodes[name], point) > 1:
                raise RoutingError("보행 도로망의 같은 교차점 ID가 서로 다른 위치를 가리켜요.")
            nodes[name] = point
        length = sum(distance_m(a, b) for a, b in zip(coords, coords[1:]))
        if not 0 < length <= MAX_ROUTE_DISTANCE_M:
            continue
        duration = length / walk_speed
        penalty, reason = 0, ""
        if props.get("verified_attention") is True and isinstance(props.get("attention_reason"), str) and props["attention_reason"].strip():
            penalty = _number(props.get("attention_penalty_s", 0), "구간 가중치", 0, 600)
            reason = props["attention_reason"].strip()[:300]
        # Missing observations contribute exactly zero; no inferred crime or dark penalty.
        edge = Edge(end, coords, length, duration, duration + penalty, props.get("stairs") is True, reason)
        graph[start].append(edge)
        graph.setdefault(end, [])
        if props.get("oneway") is not True:
            graph[end].append(Edge(start, list(reversed(coords)), length, duration, duration + penalty, edge.stairs, reason))
    return dict(graph), nodes


def dijkstra(graph, start, end, avoid_stairs=False, max_duration=math.inf, time_only=False):
    """시간 제한 안에서 비용이 가장 작은 경로. 시간·비용 Pareto 라벨로 탐색.

단일 최저비용 경로가 시간 제한을 넘더라도 다른 가능한 경로를 놓치지 않는다.
모든 구간 비용은 양수이며, 동일 노드에서 더 느리고 비싼 라벨은 제거한다.
"""
    if start not in graph or end not in graph:
        return None
    serial = 0
    first = (0.0, 0.0, start, None, None)
    labels = {0: first}
    frontier = defaultdict(list)
    frontier[start] = [0]
    queue = [(0.0, 0, 0)]
    while queue:
        _, _, label_id = heapq.heappop(queue)
        cost, duration, node, parent_id, incoming = labels[label_id]
        if label_id not in frontier[node]:
            continue
        if node == end:
            route = []
            while labels[label_id][3] is not None:
                route.append(labels[label_id][4])
                label_id = labels[label_id][3]
            return list(reversed(route))
        for edge in graph[node]:
            if avoid_stairs and edge.stairs:
                continue
            new_cost, new_duration = cost + (edge.duration if time_only else edge.cost), duration + edge.duration
            if new_duration > max_duration + 1e-7:
                continue
            previous = frontier[edge.target]
            if any(labels[lid][0] <= new_cost and labels[lid][1] <= new_duration for lid in previous):
                continue
            previous[:] = [lid for lid in previous if not (new_cost <= labels[lid][0] and new_duration <= labels[lid][1])]
            serial += 1
            if serial > 200000:
                raise RoutingError("도로망 탐색 범위가 커요. 가까운 목적지로 다시 검색해 주세요.")
            labels[serial] = (new_cost, new_duration, edge.target, label_id, edge)
            previous.append(serial)
            heapq.heappush(queue, (new_cost, serial, serial))
    return None


@lru_cache(maxsize=2)
def _load_graph_file(filename, modified_ns):
    path = Path(filename)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise RoutingError("보행 도로망 파일이 너무 커요.")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RoutingError("보행 도로망 파일을 읽지 못했어요.") from None
    return build_graph(document)


def _tmap_route(raw):
    if not isinstance(raw, dict):
        raise _provider_error()
    coords, distance, duration = [], None, None
    for feature in raw.get("features", []):
        props, geometry = feature.get("properties", {}), feature.get("geometry", {})
        if "totalDistance" in props:
            distance = props["totalDistance"]
        if "totalTime" in props:
            duration = props["totalTime"]
        if geometry.get("type") != "LineString":
            continue
        for point in geometry.get("coordinates", []):
            coordinate = list(_coord(point[1], point[0]))
            if not coords or coords[-1] != coordinate:
                coords.append(coordinate)
    if len(coords) < 2:
        raise RoutingError("이 위치의 보행 경로를 찾지 못했어요. 출발지와 목적지를 확인해 주세요.")
    distance = _number(distance, "경로 거리", 1, 100000)
    duration = _number(duration, "경로 소요 시간", 1, 86400)
    return {"id": "basic", "name": "최단 경로", "coordinates": coords, "distance_m": round(distance), "duration_s": round(duration), "source": "TMAP 보행자 최단 경로", "warnings": []}


def _safe_route(base, start, end, limit, avoid_stairs):
    filename = os.environ.get("ROUTING_GRAPH_FILE", "").strip()
    if not filename or not Path(filename).is_file():
        return None, "안심 경로를 계산할 실제 보행 도로망이 아직 연결되지 않았어요. 지금은 TMAP 최단 경로와 확인된 주변 시설을 안내해요."
    graph, nodes = _load_graph_file(filename, Path(filename).stat().st_mtime_ns)
    if not nodes:
        return None, "보행 가능한 구간이 확인되지 않아 안심 대안을 표시하지 않았어요."
    start_node = min(nodes, key=lambda node: distance_m(start, nodes[node]))
    end_node = min(nodes, key=lambda node: distance_m(end, nodes[node]))
    start_gap, end_gap = distance_m(start, nodes[start_node]), distance_m(end, nodes[end_node])
    if max(start_gap, end_gap) > MAX_SNAP_M:
        return None, "출발지 또는 목적지가 검증된 보행 도로망에서 벗어나 안심 대안을 계산할 수 없어요."
    # The graph's durations use 1.2m/s. This is explicit; do not hide speed differences.
    path = dijkstra(graph, start_node, end_node, avoid_stairs, math.floor(limit))
    if not path:
        return None, "설정한 우회 시간과 계단 조건을 만족하는 연결된 안심 대안을 찾지 못했어요."
    shortest = dijkstra(graph, start_node, end_node, avoid_stairs, math.floor(limit), time_only=True)
    # Compare with the shortest path in this same topology. An unrelated warning
    # elsewhere in the network does not justify labelling a route an alternative.
    chosen_penalty = sum(edge.cost - edge.duration for edge in path)
    shortest_penalty = sum(edge.cost - edge.duration for edge in shortest or [])
    if shortest_penalty - chosen_penalty <= .01:
        return None, "이 출발지와 목적지 사이에서 확인된 주의 정보를 더 줄이는 별도의 안심 대안을 찾지 못했어요."
    coords = []
    for edge in path:
        coords.extend(edge.coordinates if not coords else edge.coordinates[1:])
    reasons = list(dict.fromkeys(edge.reason for edge in path if edge.reason))
    warnings = ["보행 도로망에서 초당 1.2m로 계산한 예상 시간이에요. 현장 상황을 함께 확인해 주세요."]
    if max(start_gap, end_gap) > 3:
        warnings.append(f"도로망 안내 시작점은 출발지에서 약 {round(start_gap)}m, 끝점은 목적지에서 약 {round(end_gap)}m 떨어져 있어요. 연결되지 않은 길을 임의로 그리지 않았어요.")
    return {"id": "safe", "name": "안심 경로", "coordinates": coords, "distance_m": round(sum(edge.distance for edge in path)), "duration_s": round(sum(edge.duration for edge in path)), "source": "검증된 보행 도로망 · 다익스트라", "warnings": warnings, "attention_reasons": reasons}, None


def _candidate_graph(candidates):
    """공급자가 반환한 경로 각각을 독립 연결 사슬로 만든다.

시작/끝 이외의 노드를 공유하지 않아 좌표가 겹치는 고가도로·지하도를 오접속하지
않는다. 노드 좌표는 반환된 실제 LineString 내부에서만 세분화한다.
"""
    graph = {"start": [], "finish": []}
    for route_index, route in enumerate(candidates):
        segments = []
        total_length = sum(distance_m(a, b) for a, b in zip(route["coordinates"], route["coordinates"][1:]))
        if total_length <= 0:
            continue
        for a, b in zip(route["coordinates"], route["coordinates"][1:]):
            length = distance_m(a, b)
            parts = max(1, math.ceil(length / 50))
            for part in range(parts):
                p = [a[0] + (b[0] - a[0]) * part / parts, a[1] + (b[1] - a[1]) * part / parts]
                q = [a[0] + (b[0] - a[0]) * (part + 1) / parts, a[1] + (b[1] - a[1]) * (part + 1) / parts]
                kinds = {item["type"] for item in nearby_facilities((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, radius=50)}
                # Positive observed support only; unknown contributes zero.
                discount = min(.30, sum({"CCTV": .08, "가로등": .08, "비상벨": .10, "경찰시설": .10}.get(kind, 0) for kind in kinds))
                seconds = route["duration_s"] * (length / parts) / total_length
                segments.append((p, q, seconds, discount))
        previous = "start"
        for index, (p, q, seconds, discount) in enumerate(segments):
            target = "finish" if index == len(segments) - 1 else f"{route_index}:{index}"
            graph.setdefault(previous, []).append(Edge(target, [p, q], distance_m(p, q), seconds, seconds * (1 - discount), reason=str(route_index)))
            graph.setdefault(target, [])
            previous = target
    return graph


def _provider_candidate(base, start, end, payload, limit, avoid_stairs):
    if avoid_stairs:
        return None, "계단 제외 최단 경로를 안내해요. 계단 제외가 확인되지 않은 대로우선 후보는 비교하지 않았어요."
    try:
        raw = request_pedestrian_route(start[1], start[0], end[1], end[0], str(payload["start"].get("name") or "출발지")[:100], str(payload["end"].get("name") or "목적지")[:100], search_option="4")
        alternative = _tmap_route(raw)
    except (TmapAPIError, RoutingError, OSError, KeyError, TypeError, IndexError, AttributeError):
        return None, "추가 경로 후보를 불러오지 못했어요. 확인된 최단 경로를 안내해요."
    if alternative["duration_s"] > math.floor(limit) or alternative["distance_m"] > MAX_ROUTE_DISTANCE_M:
        return None, "등록 시설을 비교할 추가 경로가 설정한 우회 시간을 넘어 최단 경로를 안내해요."
    if alternative["coordinates"] == base["coordinates"]:
        return None, "TMAP의 최단·대로우선 후보가 같은 경로예요. 별도의 안심 대안은 표시하지 않았어요."
    graph = _candidate_graph([base, alternative])
    selected = dijkstra(graph, "start", "finish", max_duration=math.floor(limit))
    if not selected or selected[0].reason != "1" or not any(edge.cost < edge.duration for edge in selected):
        return None, "두 경로 후보 안에서 우회 시간과 등록 시설 조건을 함께 고려한 결과, 별도로 추천할 대안을 찾지 못했어요. 시설 미등록을 위험으로 판단하지는 않아요."
    alternative.update({"id": "safe", "name": "안심 경로", "source": "TMAP 후보 2개 비교 · 다익스트라", "selection_mode": "provider_candidates", "warnings": ["전체 도로망의 최적 경로가 아니라 TMAP의 최단·대로우선 후보 2개를 비교한 결과예요.", "주변 등록 시설에 가중치를 적용한 후보이며 실제 안전이나 시설 작동을 보장하지 않아요."]})
    return alternative, None


def compare_routes(payload):
    if not isinstance(payload, dict):
        raise RoutingError("출발지와 목적지를 선택해 주세요.")
    points = []
    for field in ("start", "end"):
        value = payload.get(field)
        if not isinstance(value, dict):
            raise RoutingError("출발지와 목적지를 선택해 주세요.")
        points.append(_coord(value.get("lat"), value.get("lon")))
    start, end = points
    span = distance_m(start, end)
    if span < 5:
        raise RoutingError("출발지와 목적지가 너무 가까워요.")
    if span > MAX_ROUTE_DISTANCE_M:
        raise RoutingError("30km 이내의 보행 목적지를 선택해 주세요.")
    percentage = _number(payload.get("detour_percent", 30), "우회 증가율(%)", 0, 100)
    max_extra = _number(payload.get("max_extra_minutes", 20), "최대 추가 시간(분)", 0, 60)
    avoid_stairs = payload.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise RoutingError("계단 피하기 설정을 확인해 주세요.")
    if not os.environ.get("TMAP_APP_KEY", "").strip():
        raise RoutingError("경로 안내 연결 준비 중이에요. 서버에 TMAP_APP_KEY를 등록하면 실제 보행 경로를 찾을 수 있어요.")
    try:
        raw = request_pedestrian_route(start[1], start[0], end[1], end[0], str(payload["start"].get("name") or "출발지")[:100], str(payload["end"].get("name") or "목적지")[:100], search_option="30" if avoid_stairs else "10")
        base = _tmap_route(raw)
    except (TmapAPIError, OSError, KeyError, TypeError, IndexError, AttributeError):
        raise _provider_error() from None
    if base["distance_m"] > MAX_ROUTE_DISTANCE_M:
        raise RoutingError("계산된 보행 거리가 30km를 넘어요. 가까운 목적지를 선택해 주세요.")
    base["facility_counts"], base["reasons"] = route_evidence(base["coordinates"])
    if avoid_stairs:
        base["name"] = "최단 경로 · 계단 제외"
        base["reasons"].insert(0, "TMAP의 최단거리·계단 제외 옵션을 적용했어요.")
    limit = min(base["duration_s"] * (1 + percentage / 100), base["duration_s"] + max_extra * 60)
    try:
        if os.environ.get("ROUTING_GRAPH_FILE", "").strip():
            safe, warning = _safe_route(base, start, end, limit, avoid_stairs)
        else:
            safe, warning = _provider_candidate(base, start, end, payload, limit, avoid_stairs)
    except RoutingError as exc:
        safe, warning = None, str(exc)
    routes = [base]
    if safe:
        safe["facility_counts"], safe["reasons"] = route_evidence(safe["coordinates"])
        if safe.get("selection_mode") == "provider_candidates":
            safe["reasons"].insert(0, "TMAP 경로 후보 2개의 구간별 시간과 등록 CCTV·가로등·비상벨·경찰시설의 근접 정보를 비교했어요.")
        else:
            safe["reasons"].insert(0, "확인된 구간별 주의 정보에 가중치를 적용해 설정한 우회 시간 안에서 경로를 계산했어요.")
        routes.append(safe)
    return {"routes": routes, "safe_available": safe is not None, "warnings": [warning] if warning else [], "detour": {"percent": percentage, "max_extra_minutes": max_extra, "max_duration_s": math.floor(limit)}, "explanation_mode": "data", "notice": "등록 시설의 존재는 현장 안전을 보장하지 않아요. 데이터가 없는 구간을 위험하다고 판단하지 않아요."}
