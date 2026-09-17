"""귀가ON - 안전 인프라(CCTV/가로등/비상벨/안심귀갓길/범죄위험) 공공데이터 시각화 웹앱.

data/ 폴더의 CSV들을 읽어 카테고리별 레이어와 밀도(Heatmap)를 지도에 표시한다.
분석 단위를 '골목길(Link)'로 할지 '격자(Grid)'로 할지 판단하기 위한 사전 탐색용 도구.

지원하지 않는 파일(좌표 컬럼이 없는 집계 통계표 등)은 자동으로 건너뛴다.
자세한 내용은 data/README.md 참고.

실행: streamlit run app.py
"""

import glob
import os

import folium
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
    location = geolocator.geocode(address, timeout=5)
    if location is None:
        return None
    return location.latitude, location.longitude, location.address


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


def build_map(datasets, center, zoom, searched_marker=None):
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
            popup=label,
            tooltip="검색 위치",
            icon=folium.Icon(color="darkred", icon="home", prefix="fa"),
        ).add_to(fmap)
        folium.Circle(
            location=[lat, lon], radius=150, color="darkred", fill=True, fill_opacity=0.1
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

        st.divider()
        st.header("📊 데이터 현황")
        if datasets:
            for category, info in datasets.items():
                st.write(f"- **{category}**: {len(info['df']):,}건")
        else:
            st.info("`data/` 폴더에 CSV 파일을 추가해주세요. (data/README.md 참고)")

        st.divider()
        export_clicked = st.button("현재 지도를 HTML로 저장", use_container_width=True)

    fmap = build_map(
        datasets,
        st.session_state.map_center,
        st.session_state.map_zoom,
        st.session_state.searched_marker,
    )

    if export_clicked:
        output_path = os.path.join(os.path.dirname(__file__), "output_map.html")
        fmap.save(output_path)
        st.success(f"저장 완료: {output_path} (브라우저에서 직접 열 수 있습니다)")

    st_folium(fmap, width=None, height=720)


if __name__ == "__main__":
    main()
