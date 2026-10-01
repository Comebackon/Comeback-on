"""귀가ON - 안전 인프라(CCTV/가로등/비상벨/안심귀갓길/범죄위험) 공공데이터 시각화 웹앱.

data/ 폴더의 CSV들을 읽어 카테고리별 레이어와 밀도(Heatmap)를 지도에 표시한다.
분석 단위를 '골목길(Link)'로 할지 '격자(Grid)'로 할지 판단하기 위한 사전 탐색용 도구.

지원하지 않는 파일(좌표 컬럼이 없는 집계 통계표 등)은 자동으로 건너뛴다.
자세한 내용은 data/README.md 참고.

실행: streamlit run app.py
"""

import glob
import html
import math
import os
import re

import folium
import numpy as np
import pandas as pd
import streamlit as st
from folium.plugins import FastMarkerCluster, HeatMap
from geopy.geocoders import Nominatim
from streamlit_folium import st_folium

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DEFAULT_CENTER = (37.5665, 126.9780)  # 서울시청 기준 기본 좌표
DEFAULT_ZOOM = 12  # 서울 시내 전체가 한눈에 들어오는 줌 레벨

# 서울시 위경도 바운딩 박스. 이 범위 밖 좌표(전국 CCTV/가로등 데이터 등)는
# load_public_csv 단계에서 원천적으로 제외해 렌더링 대상 포인트 수와 로딩 시간을 줄인다.
SEOUL_LAT_RANGE = (37.41, 37.71)
SEOUL_LON_RANGE = (126.73, 127.27)

# 컬럼명은 데이터셋마다 순서/표기가 달라서(예: WGS84위도 vs 위도, 위도-경도 순서 반대 등)
# 위치가 아니라 이름으로 찾는다.
LAT_CANDIDATES = ["WGS84위도", "위도", "lat", "latitude"]
LON_CANDIDATES = ["WGS84경도", "경도", "lon", "longitude"]
ADDRESS_CANDIDATES = ["소재지도로명주소", "소재지번주소", "도로명주소", "주소", "address"]

# (카테고리명, 파일명 판별 키워드, 마커 색상)
CATEGORY_RULES = [
    ("CCTV", ["cctv"], "blue"),
    ("가로등", ["가로등", "보안등", "led"], "orange"),
    ("비상벨", ["비상벨", "긴급벨"], "red"),
    ("안심귀갓길", ["안심귀갓길", "안심i"], "green"),
    ("안전시설물", ["안전시설물"], "darkgreen"),
    ("범죄위험", ["범죄", "crime", "치안"], "purple"),
]
DEFAULT_CATEGORY = ("기타", "gray")


# 검색 위치 주변 분석 대상 인프라 카테고리 (범죄위험은 좌표 데이터가 아니라 자치구 통계로 따로 다룬다)
INFRA_CATEGORIES = ["CCTV", "가로등", "비상벨", "안전시설물", "안심귀갓길"]
INFRA_ICONS = {"CCTV": "📹", "가로등": "💡", "비상벨": "🚨", "안전시설물": "🛡️", "안심귀갓길": "🚶"}
DEFAULT_RADIUS_M = 500

# 서울시 자치구별 면적(km², 서울 열린데이터광장 기준 근사값).
# 구 단위 범죄 통계를 '반경 내 추정치'와 '면적당 범죄 밀도'로 환산하는 데 쓴다.
SEOUL_DISTRICT_AREA_KM2 = {
    "종로구": 23.91, "중구": 9.96, "용산구": 21.87, "성동구": 16.86, "광진구": 17.06,
    "동대문구": 14.22, "중랑구": 18.50, "성북구": 24.58, "강북구": 23.60, "도봉구": 20.65,
    "노원구": 35.44, "은평구": 29.71, "서대문구": 17.63, "마포구": 23.85, "양천구": 17.41,
    "강서구": 41.44, "구로구": 20.12, "금천구": 13.02, "영등포구": 24.55, "동작구": 16.35,
    "관악구": 29.57, "서초구": 46.98, "강남구": 39.50, "송파구": 33.87, "강동구": 24.59,
}
SEOUL_AREA_KM2 = sum(SEOUL_DISTRICT_AREA_KM2.values())
# '동대문구'가 '대문구'보다, '서대문구'가 '중구'보다 먼저 매칭되도록 긴 이름부터 검사
_DISTRICT_PATTERN = re.compile("|".join(sorted(SEOUL_DISTRICT_AREA_KM2, key=len, reverse=True)))

CRIME_TYPES = ["살인", "강도", "강간·강제추행", "절도", "폭력"]
CRIME_FILE = "지역범죄데이터.csv"
PLACE_CRIME_FILE = "시설물범죄발생데이터.csv"


def _find_column(columns, candidates):
    """컬럼명 후보 중 정확히 일치하거나 부분 포함되는 첫 컬럼명을 반환."""
    normalized = {str(col).strip().lower(): col for col in columns}
    for cand in candidates:
        if cand.lower() in normalized:
            return normalized[cand.lower()]
    for cand in candidates:
        for lower_col, original_col in normalized.items():
            if cand.lower() in lower_col:
                return original_col
    return None


def classify_category(filename: str):
    name = filename.lower()
    for category, keywords, color in CATEGORY_RULES:
        if any(keyword in name for keyword in keywords):
            return category, color
    return DEFAULT_CATEGORY


def load_public_csv(filepath: str) -> pd.DataFrame:
    """공공데이터 CSV를 읽어 lat/lon/address로 표준화된 DataFrame을 반환한다.

    좌표 컬럼을 찾지 못하면(집계 통계표 등) ValueError를 던진다.
    """
    df = None
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            df = pd.read_csv(filepath, encoding=encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if df is None:
        raise ValueError("인코딩을 인식할 수 없습니다 (utf-8-sig/cp949/euc-kr 모두 실패)")

    lat_col = _find_column(df.columns, LAT_CANDIDATES)
    lon_col = _find_column(df.columns, LON_CANDIDATES)
    if lat_col is None or lon_col is None:
        raise ValueError("위도/경도 컬럼을 찾을 수 없습니다 (집계 통계표일 수 있음)")

    address_col = _find_column(df.columns, ADDRESS_CANDIDATES)

    result = pd.DataFrame(
        {
            "lat": pd.to_numeric(df[lat_col], errors="coerce"),
            "lon": pd.to_numeric(df[lon_col], errors="coerce"),
            "address": df[address_col] if address_col else "",
        }
    )
    result = result.dropna(subset=["lat", "lon"])
    # 서울시 영역 밖 값(다른 지자체 데이터, 좌표 오류, 위경도 컬럼이 뒤바뀐 경우 등)은 제외
    result = result[result["lat"].between(*SEOUL_LAT_RANGE) & result["lon"].between(*SEOUL_LON_RANGE)]
    return result.reset_index(drop=True)


@st.cache_data(show_spinner="데이터 불러오는 중...")
def load_all_datasets():
    """data/ 폴더의 모든 CSV를 로드해 카테고리별로 병합한다."""
    datasets = {}
    warnings = []
    for filepath in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv"))):
        filename = os.path.basename(filepath)
        category, color = classify_category(filename)
        try:
            df = load_public_csv(filepath)
        except ValueError as exc:
            warnings.append(f"{filename}: {exc}")
            continue
        if df.empty:
            warnings.append(f"{filename}: 유효한 좌표 데이터가 없습니다")
            continue
        df["source_file"] = filename

        if category in datasets:
            datasets[category]["df"] = pd.concat([datasets[category]["df"], df], ignore_index=True)
        else:
            datasets[category] = {"df": df, "color": color}
    return datasets, warnings


@st.cache_data(show_spinner="주소 검색 중...")
def geocode_address(address: str):
    geolocator = Nominatim(user_agent="gwiga-on-safety-map")
    try:
        location = geolocator.geocode(address, timeout=5, country_codes="kr")
    except Exception:  # 네트워크 오류·요청 제한 등으로 앱 전체가 멈추지 않도록
        return None
    if location is None:
        return None
    return location.latitude, location.longitude, location.address


def _read_csv_any_encoding(filepath, **kwargs):
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(filepath, encoding=encoding, **kwargs)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"인코딩을 인식할 수 없습니다: {filepath}")


def _to_int(value):
    """통계표의 '-'(해당 없음)나 쉼표를 처리해 정수로 변환."""
    num = pd.to_numeric(str(value).replace(",", "").strip(), errors="coerce")
    return 0 if pd.isna(num) else int(num)


@st.cache_data(show_spinner=False)
def load_district_crime():
    """지역범죄데이터.csv(자치구별 2단 헤더 통계표)를 구 단위 표로 변환.

    반환: index=자치구, columns=[총발생, 총검거, 살인, 강도, 강간·강제추행, 절도, 폭력] (발생 건수)
    파일이 없으면 None.
    """
    path = os.path.join(DATA_DIR, CRIME_FILE)
    if not os.path.exists(path):
        return None
    raw = _read_csv_any_encoding(path, header=None, skiprows=4)
    rows = {}
    for _, row in raw.iterrows():
        name = str(row[1]).strip()
        record = {"총발생": _to_int(row[2]), "총검거": _to_int(row[3])}
        # 컬럼 순서: 소계(발생,검거), 살인(발생,검거), 강도, 강간·강제추행, 절도, 폭력
        for i, crime in enumerate(CRIME_TYPES):
            record[crime] = _to_int(row[4 + i * 2])
        rows["서울 전체" if name == "소계" else name] = record
    return pd.DataFrame.from_dict(rows, orient="index")


@st.cache_data(show_spinner=False)
def load_road_crime_share():
    """시설물범죄발생데이터.csv에서 '도로(골목길 포함)' 발생 비중(%)을 구한다. 없으면 None."""
    path = os.path.join(DATA_DIR, PLACE_CRIME_FILE)
    if not os.path.exists(path):
        return None
    raw = _read_csv_any_encoding(path, header=None)
    places = [str(v) for v in raw.iloc[2]]
    road_idx = next((i for i, v in enumerate(places) if v.startswith("도로")), None)
    if road_idx is None:
        return None
    total_row = raw[raw[1].astype(str).str.strip() == "소계"].iloc[0]
    total, road = _to_int(total_row[2]), _to_int(total_row[road_idx])
    return round(road / total * 100, 1) if total else None


def _haversine_m(lat, lon, lats, lons):
    """기준점(lat, lon)에서 배열 좌표들까지의 거리(m)를 벡터 연산으로 계산."""
    lat1, lon1 = np.radians(lat), np.radians(lon)
    lat2, lon2 = np.radians(lats), np.radians(lons)
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6_371_000 * 2 * np.arcsin(np.sqrt(a))


def analyze_infrastructure(datasets, lat, lon, radius_m):
    """카테고리별 반경 내 개수, 가장 가까운 시설까지 거리, 서울 평균 대비 비율을 계산."""
    circle_km2 = math.pi * (radius_m / 1000) ** 2
    result = {}
    for category in INFRA_CATEGORIES:
        if category not in datasets:
            continue
        df = datasets[category]["df"]
        dists = _haversine_m(lat, lon, df["lat"].to_numpy(), df["lon"].to_numpy())
        count = int((dists <= radius_m).sum())
        # 서울 전체 시설이 면적에 고르게 퍼져 있다고 가정했을 때 같은 반경에 기대되는 개수
        expected = len(df) * circle_km2 / SEOUL_AREA_KM2
        result[category] = {
            "count": count,
            "nearest_m": float(dists.min()) if len(dists) else None,
            "ratio": count / expected if expected else None,
        }
    return result


def detect_district(label, datasets, lat, lon):
    """검색 결과 주소 문자열에서 자치구를 찾고, 없으면 가장 가까운 시설 주소에서 추정."""
    match = _DISTRICT_PATTERN.search(label or "")
    if match:
        return match.group(0)
    best = None
    for info in datasets.values():
        df = info["df"]
        has_addr = df["address"].astype(str).str.contains(_DISTRICT_PATTERN, na=False)
        sub = df[has_addr]
        if sub.empty:
            continue
        dists = _haversine_m(lat, lon, sub["lat"].to_numpy(), sub["lon"].to_numpy())
        i = int(dists.argmin())
        if best is None or dists[i] < best[0]:
            best = (dists[i], str(sub["address"].iloc[i]))
    if best and best[0] < 3000:
        found = _DISTRICT_PATTERN.search(best[1])
        if found:
            return found.group(0)
    return None


def analyze_crime(district, radius_m):
    """자치구 범죄 통계를 비중·순위·검거율·면적당 밀도·반경 내 추정치로 환산."""
    crime = load_district_crime()
    if crime is None or district not in crime.index:
        return None
    seoul = crime.loc["서울 전체"]
    districts = crime.drop(index="서울 전체")
    row = crime.loc[district]
    area = SEOUL_DISTRICT_AREA_KM2.get(district)
    density = row["총발생"] / area if area else None
    seoul_density = seoul["총발생"] / SEOUL_AREA_KM2
    circle_km2 = math.pi * (radius_m / 1000) ** 2
    return {
        "district": district,
        "total": int(row["총발생"]),
        "share_pct": row["총발생"] / seoul["총발생"] * 100,
        "rank": int(districts["총발생"].rank(ascending=False, method="min")[district]),
        "n_districts": len(districts),
        "arrest_pct": row["총검거"] / row["총발생"] * 100 if row["총발생"] else None,
        "density": density,
        "density_ratio": density / seoul_density if density else None,
        "radius_estimate": density * circle_km2 if density else None,
        "by_type": {c: int(row[c]) for c in CRIME_TYPES},
        "by_type_pct": {c: row[c] / row["총발생"] * 100 for c in CRIME_TYPES} if row["총발생"] else {},
    }


def _fmt_distance(meters):
    if meters is None:
        return "-"
    return f"{meters / 1000:.1f}km" if meters >= 1000 else f"{meters:.0f}m"


def render_area_report(label, district, infra, crime, radius_m, road_share):
    """검색 위치 주변 안전 정보 패널(Streamlit)을 그린다."""
    st.subheader("🔎 검색 위치 안전 리포트")
    st.markdown(f"**{label}**")
    st.caption(f"반경 {radius_m:,}m 기준 · 자치구: {district or '확인 불가'}")

    st.markdown("##### 주변 안전 인프라")
    cols = st.columns(2)
    for i, (category, data) in enumerate(infra.items()):
        ratio = data["ratio"]
        delta = f"{(ratio - 1) * 100:+.0f}% (서울 평균 대비)" if ratio is not None else None
        with cols[i % 2]:
            st.metric(
                label=f"{INFRA_ICONS.get(category, '')} {category}",
                value=f"{data['count']:,}개",
                delta=delta,
                help=f"가장 가까운 {category}: {_fmt_distance(data['nearest_m'])}\n"
                "서울 평균 = 서울 전체 개수 × (반경 원 면적 ÷ 서울 면적)",
            )
            st.caption(f"가장 가까운 곳 {_fmt_distance(data['nearest_m'])}")
    st.caption("※ 공공데이터가 일부 자치구만 제공되는 항목(예: 가로등)은 데이터가 없는 지역에서 0개로 나올 수 있습니다.")

    st.markdown("##### 범죄 위험도")
    if crime is None:
        st.info("이 위치의 자치구를 확인할 수 없거나 범죄 통계가 없습니다.")
        return
    c1, c2 = st.columns(2)
    c1.metric(f"{crime['district']} 연간 범죄", f"{crime['total']:,}건",
              help="2024년 5대 범죄(살인·강도·강간강제추행·절도·폭력) 발생 건수")
    c2.metric("서울 전체 중 비중", f"{crime['share_pct']:.1f}%",
              help=f"서울 {crime['n_districts']}개 구 중 발생 건수 {crime['rank']}위")
    c3, c4 = st.columns(2)
    if crime["density_ratio"] is not None:
        c3.metric("면적당 범죄 밀도", f"{crime['density']:.0f}건/km²",
                  delta=f"{(crime['density_ratio'] - 1) * 100:+.0f}% (서울 평균 대비)",
                  delta_color="inverse")
    if crime["arrest_pct"] is not None:
        c4.metric("검거율", f"{crime['arrest_pct']:.1f}%",
                  help="검거 건수에는 이전 연도 발생 사건이 포함돼 100%를 넘을 수 있습니다.")
    if crime["radius_estimate"] is not None:
        st.metric(f"반경 {radius_m:,}m 내 추정 범죄", f"약 {crime['radius_estimate']:.0f}건/년",
                  help="구 전체 발생 건수를 구 면적에 고르게 나눠 원 면적만큼 환산한 추정치입니다.")
    st.caption(f"서울 {crime['n_districts']}개 구 중 범죄 발생 **{crime['rank']}위** (1위가 가장 많음)")

    type_df = pd.DataFrame(
        {"비중(%)": [round(v, 1) for v in crime["by_type_pct"].values()],
         "건수": list(crime["by_type"].values())},
        index=list(crime["by_type_pct"].keys()),
    )
    st.markdown("###### 범죄 유형별 비중")
    st.bar_chart(type_df["비중(%)"], height=180, horizontal=True)
    st.dataframe(type_df, use_container_width=True)

    if road_share is not None:
        st.caption(f"참고: 서울 전체 범죄의 {road_share}%가 도로·골목길에서 발생합니다 (시설물별 범죄 통계).")


def build_report_popup(label, district, infra, crime, radius_m):
    """지도 마커 팝업에 넣을 요약 HTML."""
    lines = [f"<b>{html.escape(label)}</b>", f"<small>반경 {radius_m:,}m · {district or '-'}</small><hr style='margin:4px 0'>"]
    for category, data in infra.items():
        lines.append(f"{INFRA_ICONS.get(category, '')} {category}: <b>{data['count']:,}개</b> "
                     f"(최근접 {_fmt_distance(data['nearest_m'])})")
    if crime:
        lines.append(f"<hr style='margin:4px 0'>🚔 {crime['district']} 범죄 {crime['total']:,}건 "
                     f"(서울의 <b>{crime['share_pct']:.1f}%</b>, {crime['rank']}위)")
        if crime["radius_estimate"] is not None:
            lines.append(f"반경 내 추정 약 {crime['radius_estimate']:.0f}건/년")
    return folium.Popup("<br>".join(lines), max_width=320)


# 개별 folium.Marker(Icon+Popup)로 수십만 건을 렌더링하면 HTML에 마커마다 파이썬이 생성한
# JS 객체가 그대로 박혀 출력이 수백 MB까지 커져 브라우저가 멈춘다(예: CCTV 37만 건 -> 500MB+).
# FastMarkerCluster는 좌표/주소 배열만 JSON으로 내보내고 마커 생성은 브라우저 JS에서 하므로
# 대용량 포인트 데이터에도 출력 크기와 렌더링 속도가 안정적이다.
_FAST_CLUSTER_CALLBACK = """
    function (row) {
        var marker = L.circleMarker(new L.LatLng(row[0], row[1]), {
            radius: 6,
            color: row[3],
            fillColor: row[3],
            fillOpacity: 0.8,
            weight: 1
        });
        if (row[2]) { marker.bindPopup(row[2]); }
        marker.bindTooltip(row[4]);
        return marker;
    }
"""


def build_map(datasets, center, zoom, searched_marker=None, radius_m=DEFAULT_RADIUS_M, popup=None):
    fmap = folium.Map(location=center, zoom_start=zoom, tiles="OpenStreetMap")

    all_points = []
    for category, info in datasets.items():
        df = info["df"]
        if df.empty:
            continue

        layer = folium.FeatureGroup(name=f"{category} ({len(df):,}건)", show=True)
        cluster_data = df[["lat", "lon", "address"]].copy()
        cluster_data["color"] = info["color"]
        cluster_data["category"] = category
        FastMarkerCluster(
            data=cluster_data.values.tolist(), callback=_FAST_CLUSTER_CALLBACK
        ).add_to(layer)
        layer.add_to(fmap)
        all_points.extend(df[["lat", "lon"]].values.tolist())

    if all_points:
        heat_layer = folium.FeatureGroup(name="안전 인프라 밀도 (Heatmap)", show=False)
        HeatMap(all_points, radius=18, blur=22).add_to(heat_layer)
        heat_layer.add_to(fmap)

    if searched_marker:
        lat, lon, label = searched_marker
        folium.Marker(
            location=[lat, lon],
            popup=popup or label,
            tooltip="검색 위치 (클릭하면 요약 정보)",
            icon=folium.Icon(color="darkred", icon="home", prefix="fa"),
        ).add_to(fmap)
        folium.Circle(
            location=[lat, lon],
            radius=radius_m,
            color="darkred",
            weight=2,
            dash_array="6",
            fill=True,
            fill_opacity=0.08,
            tooltip=f"분석 반경 {radius_m:,}m",
        ).add_to(fmap)

    folium.LayerControl(collapsed=False, position="topright").add_to(fmap)
    return fmap


def main():
    st.set_page_config(page_title="귀가ON | 안전 인프라 밀도 분석", layout="wide")
    st.title("🏠 귀가ON — 안전 인프라 데이터 시각화")
    st.caption(
        "가로등·CCTV·비상벨·안심귀갓길·범죄위험 데이터의 위경도 분포와 밀도를 확인하여 "
        "지도 분석 단위를 '골목길(Link)' 또는 '격자(Grid)' 중 무엇으로 할지 판단하기 위한 도구입니다."
    )

    datasets, _warnings = load_all_datasets()

    if "map_center" not in st.session_state:
        st.session_state.map_center = DEFAULT_CENTER
        st.session_state.map_zoom = DEFAULT_ZOOM
        st.session_state.searched_marker = None

    with st.sidebar:
        st.header("📍 주소로 이동")
        address = st.text_input("주소를 입력하세요", placeholder="예) 서울특별시 강남구 테헤란로 152")
        if st.button("이동", use_container_width=True) and address:
            result = geocode_address(address)
            if result:
                lat, lon, label = result
                st.session_state.map_center = (lat, lon)
                st.session_state.map_zoom = 17
                st.session_state.searched_marker = (lat, lon, label)
                st.success(f"이동 완료: {label}")
            else:
                st.error("주소를 찾을 수 없습니다. 다시 입력해주세요.")

        radius_m = st.slider(
            "분석 반경 (m)", min_value=100, max_value=1000, value=DEFAULT_RADIUS_M, step=50,
            help="검색 위치를 중심으로 이 반경 안의 CCTV·가로등·비상벨 등을 집계합니다.",
        )
        if st.session_state.searched_marker and st.button("검색 위치 초기화", use_container_width=True):
            st.session_state.searched_marker = None
            st.session_state.map_center = DEFAULT_CENTER
            st.session_state.map_zoom = DEFAULT_ZOOM
            st.rerun()

        st.divider()
        st.header("📊 데이터 현황")
        if datasets:
            for category, info in datasets.items():
                st.write(f"- **{category}**: {len(info['df']):,}건")
        else:
            st.info("`data/` 폴더에 CSV 파일을 추가해주세요. (data/README.md 참고)")

        st.divider()
        export_clicked = st.button("현재 지도를 HTML로 저장", use_container_width=True)

    searched = st.session_state.searched_marker
    popup = None
    if searched:
        lat, lon, label = searched
        infra = analyze_infrastructure(datasets, lat, lon, radius_m)
        district = detect_district(label, datasets, lat, lon)
        crime = analyze_crime(district, radius_m) if district else None
        popup = build_report_popup(label, district, infra, crime, radius_m)
        # 검색 반경이 화면에 다 들어오도록 줌 조정
        st.session_state.map_zoom = 17 if radius_m <= 200 else 16 if radius_m <= 450 else 15

    fmap = build_map(
        datasets,
        st.session_state.map_center,
        st.session_state.map_zoom,
        searched,
        radius_m=radius_m,
        popup=popup,
    )

    if export_clicked:
        output_path = os.path.join(os.path.dirname(__file__), "output_map.html")
        fmap.save(output_path)
        st.success(f"저장 완료: {output_path} (브라우저에서 직접 열 수 있습니다)")

    if searched:
        map_col, report_col = st.columns([3, 2], gap="medium")
        with map_col:
            st_folium(fmap, width=None, height=760, returned_objects=[])
        with report_col:
            with st.container(height=760, border=False):
                render_area_report(label, district, infra, crime, radius_m, load_road_crime_share())
    else:
        st.info("👈 사이드바에 주소를 검색하면 주변 CCTV·가로등·비상벨 개수와 해당 지역 범죄율이 지도 옆에 표시됩니다.")
        st_folium(fmap, width=None, height=720, returned_objects=[])


if __name__ == "__main__":
    main()
