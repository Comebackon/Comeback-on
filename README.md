# 귀가ON (Comeback-on)

[AI 지니어스 아카데미]

> 1인 가구 및 치안 취약계층을 위한 **AI 기반 안전 보행 경로 안내 서비스**
> 최단시간이 아니라 가로등·CCTV·비상벨·범죄 위험도 데이터를 종합 연산해 가장 안전한 골목길 경로를 안내합니다.

## 이 저장소 현재 단계: 안전 인프라 데이터 시각화

지도 분석 단위를 **골목길(Link) 단위**로 할지 **격자(Grid) 단위**로 할지 판단하기 위해,
공공데이터의 위경도 좌표 분포와 밀도를 한눈에 확인하는 웹 지도(`app.py`)입니다.

### 주요 기능

- Folium + Streamlit 기반 인터랙티브 지도
- 주소 입력 시 해당 위치로 지도 이동 및 마커 표시 (geopy/Nominatim)
- CCTV·가로등·비상벨·안심귀갓길·범죄위험 레이어를 지도 우측 상단 체크박스로 On/Off (`LayerControl`)
- 안전 인프라 밀도 확인용 Heatmap 레이어
- `MarkerCluster`로 대용량 포인트도 가볍게 렌더링
- 사이드바에서 현재 지도를 `.html` 파일로 저장 → 브라우저로 바로 열람 가능
- 컬럼명이 데이터셋마다 달라도(`WGS84위도`/`위도`, `소재지도로명주소`/`소재지번주소` 등) 자동 매핑
- 좌표가 없는 주소 전용 데이터, 자치구별 집계 통계표처럼 이 지도에 맞지 않는 파일은 자동으로 건너뛰고 경고 표시

## 폴더 구조

```
Comeback-on/
├── app.py              # 메인 Streamlit 앱
├── requirements.txt    # 의존 라이브러리 목록
├── data/                # 공공데이터 CSV 저장 위치 (data/README.md 참고)
└── README.md
```

## 실행 방법

### 1. 가상환경 생성 및 활성화

```bash
python -m venv venv

# Windows (PowerShell)
venv\Scripts\Activate.ps1

# macOS / Linux
source venv/bin/activate
```

### 2. 라이브러리 설치

```bash
pip install -r requirements.txt
```

### 3. 데이터 준비

공공데이터포털에서 받은 CSV(CCTV, 보안등, 비상벨, 안심귀갓길, 범죄 관련 등)를 `data/` 폴더에 넣습니다.
컬럼명 매핑 규칙, 카테고리 자동 분류 기준, 지원하지 않는 데이터 형태는 [data/README.md](data/README.md)를 참고하세요.

### 4. 앱 실행

```bash
streamlit run app.py
```

브라우저가 자동으로 열리며 지도가 표시됩니다. 사이드바의 "현재 지도를 HTML로 저장" 버튼을 누르면
`output_map.html` 파일이 생성되어 Streamlit 없이도 브라우저에서 바로 열어볼 수 있습니다.

## 팀원