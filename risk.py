"""귀가ON 안전 점수(Safety Score) 계산 모듈.

지역범죄데이터.csv(자치구별 집계) + CCTV/가로등/비상벨/안전시설물 포인트 데이터를
격자(기본 200m) 단위로 합쳐 0~100점 안전 점수를 만든다.

데이터 한계:
- 범죄 위험도는 자치구(구) 단위까지만 세분화할 수 있다. 동/도로 단위 범죄 데이터가
  없어 같은 구에 속한 모든 격자는 동일한 범죄 점수를 공유한다.
- 격자가 어느 구에 속하는지 판정할 행정경계(GeoJSON)가 없어, CCTV 주소에서 구 이름을
  뽑아 구별 평균 좌표(중심점)를 근사한 뒤 최근접 구를 배정한다. 완벽한 경계 매칭은 아니다.
- 시설물범죄발생데이터.csv(장소유형별 범죄 비중, 서울 전체 집계)는 위치 정보가 없어
  격자 점수에 직접 넣을 수 없다. 대신 "도로" 유형 범죄 비중만 참고 상수로 남겨둔다.
- 유동인구(으슥한 정도) 데이터는 보유하고 있지 않아 이번 버전에서는 제외했다.
"""

import math
import re

import numpy as np
import pandas as pd

CELL_SIZE_M = 200  # 격자 한 변 길이(미터). 값을 키우면 더 성긴 격자가 된다.

_DISTRICT_RE = re.compile(r"(?:서울특별시|서울시)\s*([가-힣]+구)")

# 가중치 (기본 1.0 = 동일 비중). 필요에 따라 조정한다.
WEIGHT_CRIME = 1.0  # 범죄 위험도 → 위험(+)
WEIGHT_DARK = 1.0  # 가로등 부재로 인한 어두운 정도 → 위험(+)
WEIGHT_CCTV = 1.0  # CCTV 밀도 → 안전(-)
WEIGHT_SAFETY_INFRA = 1.0  # 비상벨 + 안전시설물 밀도 → 안전(-)

# 참고용: 시설물범죄발생데이터.csv 기준 서울 전체 범죄 중 "도로"에서 발생한 비중.
# 격자 점수 계산에는 쓰이지 않지만, 길거리 인프라(CCTV/가로등/비상벨) 가중치의 근거 자료.
STREET_CRIME_SHARE = 15749 / 80819  # 약 19.5%


def extract_district(address: str):
    """주소 문자열에서 자치구 이름을 추출한다. 못 찾으면 None."""
    if not isinstance(address, str):
        return None
    m = _DISTRICT_RE.search(address)
    return m.group(1) if m else None


def load_district_crime_scores(filepath: str) -> dict:
    """지역범죄데이터.csv를 읽어 {자치구명: 범죄 위험도(0~5)} 딕셔너리를 반환한다."""
    df = pd.read_csv(filepath, encoding="utf-8-sig")
    df = df.iloc[3:]  # 상단 3행은 "합계/소계", "발생/검거" 등 헤더 설명 행
    df = df.rename(columns={"자치구별(2)": "district", "2024": "crime_count"})
    df["district"] = df["district"].astype(str).str.strip()
    df = df[df["district"] != "소계"]  # 서울 전체 합계 행 제외, 개별 구만 사용
    df["crime_count"] = pd.to_numeric(df["crime_count"], errors="coerce")
    df = df.dropna(subset=["crime_count"])

    lo, hi = df["crime_count"].min(), df["crime_count"].max()
    span = (hi - lo) or 1
    df["score"] = (df["crime_count"] - lo) / span * 5
    return dict(zip(df["district"], df["score"]))


def compute_district_centroids(point_df: pd.DataFrame) -> dict:
    """포인트 데이터의 address 컬럼에서 구를 추출해 구별 평균 좌표(중심점)를 근사한다."""
    tmp = point_df[["lat", "lon", "address"]].copy()
    tmp["district"] = tmp["address"].map(extract_district)
    tmp = tmp.dropna(subset=["district"])
    if tmp.empty:
        return {}
    grouped = tmp.groupby("district")[["lat", "lon"]].mean()
    return {d: (row["lat"], row["lon"]) for d, row in grouped.iterrows()}


def _assign_nearest_district(lats, lons, centroids: dict):
    if not centroids:
        return np.full(len(lats), None, dtype=object)
    names = list(centroids.keys())
    clat = np.array([centroids[n][0] for n in names])
    clon = np.array([centroids[n][1] for n in names])
    lat_arr = np.asarray(lats, dtype=float)[:, None]
    lon_arr = np.asarray(lons, dtype=float)[:, None]
    dist2 = (lat_arr - clat) ** 2 + (lon_arr - clon) ** 2
    idx = dist2.argmin(axis=1)
    return np.array(names, dtype=object)[idx]


def meters_to_degrees(cell_size_m: float, ref_lat: float = 37.5665):
    """미터 단위 셀 크기를 위도/경도 도(degree) 단위로 환산한다(서울 위도 기준)."""
    lat_deg = cell_size_m / 111_320
    lon_deg = cell_size_m / (111_320 * math.cos(math.radians(ref_lat)))
    return lat_deg, lon_deg


def _bin_counts(df: pd.DataFrame, lat_deg: float, lon_deg: float, lat_min: float, lon_min: float) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    row = np.floor((df["lat"] - lat_min) / lat_deg).astype(int)
    col = np.floor((df["lon"] - lon_min) / lon_deg).astype(int)
    idx = pd.MultiIndex.from_arrays([row, col], names=["row", "col"])
    return pd.Series(1, index=idx).groupby(level=["row", "col"]).sum()


def _normalize_0_5(series: pd.Series, clip_percentile: float = 0.95) -> pd.Series:
    """0을 하한으로, 상위 clip_percentile 분위수를 상한으로 0~5점 정규화한다.
    소수의 극단치 셀(예: CCTV가 몰린 교차로)이 나머지 전체 점수를 눌러버리지 않도록
    막기 위해 최댓값 대신 분위수를 상한으로 쓴다."""
    if series.empty:
        return series
    cap = series.quantile(clip_percentile)
    if cap <= 0:
        cap = series.max() or 1
    return (series.clip(upper=cap) / cap) * 5


def _smoothed_density(rows, cols, counts: pd.Series, radius: int = 1) -> np.ndarray:
    """(row,col) 반경 radius 이웃 셀들의 카운트 합을 구한다.

    가로등처럼 셀당 포인트가 희소한 데이터는 정확히 같은 200m 셀 안에 없으면
    카운트가 0이 되어 "무조건 어두움"으로 오판되기 쉽다. 인근 (2*radius+1)^2개
    셀을 합산해 완화(스무딩)한다.
    """
    lookup = counts.to_dict()
    offsets = [(dr, dc) for dr in range(-radius, radius + 1) for dc in range(-radius, radius + 1)]
    out = np.zeros(len(rows))
    for i, (r, c) in enumerate(zip(rows, cols)):
        out[i] = sum(lookup.get((r + dr, c + dc), 0) for dr, dc in offsets)
    return out


def compute_grid_scores(
    datasets: dict,
    district_crime_scores: dict,
    lat_min: float,
    lon_min: float,
    cell_size_m: float = CELL_SIZE_M,
) -> pd.DataFrame:
    """카테고리별 데이터셋을 격자로 집계해 셀마다 안전 점수(0~100)를 계산한다."""
    lat_deg, lon_deg = meters_to_degrees(cell_size_m)

    cctv_df = datasets.get("CCTV", {}).get("df")
    light_df = datasets.get("가로등", {}).get("df")
    bell_df = datasets.get("비상벨", {}).get("df")
    facility_df = datasets.get("안전시설물", {}).get("df")

    cctv_counts = _bin_counts(cctv_df, lat_deg, lon_deg, lat_min, lon_min)
    light_counts = _bin_counts(light_df, lat_deg, lon_deg, lat_min, lon_min)
    bell_counts = _bin_counts(bell_df, lat_deg, lon_deg, lat_min, lon_min)
    facility_counts = _bin_counts(facility_df, lat_deg, lon_deg, lat_min, lon_min)
    safety_infra_counts = bell_counts.add(facility_counts, fill_value=0)

    all_index = cctv_counts.index.union(light_counts.index).union(safety_infra_counts.index)
    if all_index.empty:
        return pd.DataFrame()

    grid = pd.DataFrame(index=all_index)
    grid["cctv_count"] = cctv_counts
    grid["light_count"] = light_counts
    grid["safety_infra_count"] = safety_infra_counts
    grid = grid.fillna(0)
    grid = grid.reset_index()  # row, col 컬럼 생성

    grid["lat_center"] = lat_min + (grid["row"] + 0.5) * lat_deg
    grid["lon_center"] = lon_min + (grid["col"] + 0.5) * lon_deg

    # 셀 단위 카운트는 상세 표시용으로 남겨두고, 점수 계산에는 3x3 이웃(약 600m 반경)
    # 합산값을 써서 데이터가 희소한 셀에서도 안정적인 밀도를 반영한다.
    cctv_smoothed = pd.Series(_smoothed_density(grid["row"], grid["col"], cctv_counts))
    light_smoothed = pd.Series(_smoothed_density(grid["row"], grid["col"], light_counts))
    safety_infra_smoothed = pd.Series(_smoothed_density(grid["row"], grid["col"], safety_infra_counts))

    grid["cctv_score"] = _normalize_0_5(cctv_smoothed)
    grid["safety_infra_score"] = _normalize_0_5(safety_infra_smoothed)
    grid["dark_score"] = 5 - _normalize_0_5(light_smoothed)  # 가로등이 적을수록 위험 점수 상승

    centroids = compute_district_centroids(cctv_df) if cctv_df is not None else {}
    grid["district"] = _assign_nearest_district(grid["lat_center"], grid["lon_center"], centroids)
    default_crime_score = (
        sum(district_crime_scores.values()) / len(district_crime_scores) if district_crime_scores else 0.0
    )
    grid["crime_score"] = grid["district"].map(district_crime_scores).fillna(default_crime_score)

    raw_risk = (
        WEIGHT_CRIME * grid["crime_score"]
        + WEIGHT_DARK * grid["dark_score"]
        - WEIGHT_CCTV * grid["cctv_score"]
        - WEIGHT_SAFETY_INFRA * grid["safety_infra_score"]
    )
    # raw_risk 대략 [-10, +10] 범위 -> 0~100점으로 변환 (50점을 중간값으로)
    grid["safety_score"] = (50 - raw_risk * 5).clip(0, 100)
    grid.attrs["cell_size_m"] = cell_size_m
    grid.attrs["lat_min"] = lat_min
    grid.attrs["lon_min"] = lon_min
    return grid


def lookup_safety_score(lat: float, lon: float, grid: pd.DataFrame) -> dict | None:
    """주어진 좌표가 속한 격자의 안전 점수를 반환한다.

    해당 격자에 데이터가 없으면(공원/하천 등) 가장 가까운 격자 값으로 근사하고
    "estimated": True로 표시한다.
    """
    if grid is None or grid.empty:
        return None

    cell_size_m = grid.attrs.get("cell_size_m", CELL_SIZE_M)
    lat_min = grid.attrs.get("lat_min")
    lon_min = grid.attrs.get("lon_min")
    lat_deg, lon_deg = meters_to_degrees(cell_size_m)
    row = int((lat - lat_min) // lat_deg)
    col = int((lon - lon_min) // lon_deg)

    match = grid[(grid["row"] == row) & (grid["col"] == col)]
    estimated = False
    if match.empty:
        dist2 = (grid["row"] - row) ** 2 + (grid["col"] - col) ** 2
        match = grid.loc[[dist2.idxmin()]]
        estimated = True

    r = match.iloc[0]
    return {
        "safety_score": round(float(r["safety_score"]), 1),
        "district": r["district"],
        "crime_score": round(float(r["crime_score"]), 2),
        "dark_score": round(float(r["dark_score"]), 2),
        "cctv_score": round(float(r["cctv_score"]), 2),
        "safety_infra_score": round(float(r["safety_infra_score"]), 2),
        "cctv_count": int(r["cctv_count"]),
        "light_count": int(r["light_count"]),
        "safety_infra_count": int(r["safety_infra_count"]),
        "estimated": estimated,
    }


# 안전 점수(0~100)를 빨강(위험) -> 노랑 -> 초록(안전)으로 잇는 색상 스케일.
_COLOR_STOPS = [(0, (211, 47, 47)), (50, (251, 192, 45)), (100, (56, 142, 60))]


def score_to_color(score: float) -> str:
    score = max(0.0, min(100.0, score))
    for (s0, c0), (s1, c1) in zip(_COLOR_STOPS, _COLOR_STOPS[1:]):
        if s0 <= score <= s1:
            t = (score - s0) / (s1 - s0)
            r, g, b = (int(c0[i] + t * (c1[i] - c0[i])) for i in range(3))
            return f"#{r:02x}{g:02x}{b:02x}"
    return "#%02x%02x%02x" % _COLOR_STOPS[-1][1]


def grid_to_geojson(grid: pd.DataFrame, merge_factor: int = 1) -> dict:
    """격자 DataFrame을 folium.GeoJson에 바로 넣을 수 있는 GeoJSON으로 변환한다.
    각 셀을 사각형 폴리곤으로 표현해 칸 경계가 눈에 보이도록 한다.

    merge_factor > 1이면 그만큼 인접 셀을 합쳐(예: 2 -> 200m 셀 4개를 400m 셀 1개로)
    폴리곤 개수를 줄인다. 폴리곤 수만 개를 그대로 그리면 HTML이 수십 MB로 커지고
    브라우저 렌더링이 느려지므로, 지도에 표시할 때는 기본 격자보다 성기게 뭉쳐서 쓴다.
    """
    if grid is None or grid.empty:
        return {"type": "FeatureCollection", "features": []}

    cell_size_m = grid.attrs.get("cell_size_m", CELL_SIZE_M)
    lat_min = grid.attrs.get("lat_min")
    lon_min = grid.attrs.get("lon_min")
    lat_deg, lon_deg = meters_to_degrees(cell_size_m)

    df = grid[["row", "col", "safety_score", "district"]].copy()
    if merge_factor > 1:
        df["row"] = df["row"] // merge_factor
        df["col"] = df["col"] // merge_factor
        df = df.groupby(["row", "col"], as_index=False).agg(
            safety_score=("safety_score", "mean"),
            district=("district", lambda s: s.mode().iat[0]),
        )
        lat_deg *= merge_factor
        lon_deg *= merge_factor

    features = []
    for r in df.itertuples():
        lat0 = lat_min + r.row * lat_deg
        lat1 = lat0 + lat_deg
        lon0 = lon_min + r.col * lon_deg
        lon1 = lon0 + lon_deg
        score = round(float(r.safety_score), 1)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]],
                },
                "properties": {
                    "safety_score": score,
                    "district": r.district,
                    "color": score_to_color(score),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
