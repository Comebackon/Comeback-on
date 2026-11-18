"""로지스틱 함수 기반 구간 위험 지표.

멘토링 3회차 조언(일반 AI보다 로지스틱 같은 회귀 방식)을 반영한다.

    z = b0 + w_crime * 범죄등급(-1~1) - Σ w_시설 * 시설밀도(0~1)
    위험 지표 p = 1 / (1 + e^(-z))      (0~1)

중요한 한계(화면과 발표 자료에도 같은 표현을 쓴다):
- 계수는 팀이 정한 값이다. 구간별 실제 범죄 발생 여부(정답) 데이터가 없어 학습하지 않았다.
- p는 실제 범죄 발생 확률이 아니라 "등록된 범죄 등급·시설 정보로 계산한 상대 지표"다.
- 시설이 등록되지 않은 구간은 가점도 감점도 없이 중립으로 계산한다(미등록 ≠ 위험).
- 범죄 등급은 자치구 단위라 같은 구 안에서는 모든 구간이 같다.
"""

import math
from collections import Counter
from functools import lru_cache

# 팀이 정한 계수 (학습값 아님)
INTERCEPT = -1.0
W_CRIME = 0.8
W_FACILITY = {"CCTV": 0.9, "가로등": 0.7, "비상벨": 0.9, "경찰시설": 0.9}
# 시설 수가 이 값 이상이면 더 늘어도 효과가 같다(포화).
FACILITY_CAP = {"CCTV": 3, "가로등": 3, "비상벨": 1, "경찰시설": 1}
# 안심 경로 비용: 시설이 있는 구간의 이동 비용을 최대 30%까지 낮춘다.
COST_SLOPE = 1.1
MIN_COST_FACTOR = 0.7

LOW_BELOW = 0.2
MEDIUM_BELOW = 0.4


def sigmoid(z):
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def risk_probability(crime_grade, counts):
    """crime_grade: 0~5 (None이면 중립), counts: {시설종류: 개수}."""
    z = INTERCEPT
    if crime_grade is not None:
        z += W_CRIME * (min(5.0, max(0.0, crime_grade)) - 2.5) / 2.5
    for kind, weight in W_FACILITY.items():
        z -= weight * min(counts.get(kind, 0), FACILITY_CAP[kind]) / FACILITY_CAP[kind]
    return sigmoid(z)


def cost_factor(crime_grade, counts):
    """시설이 없으면 정확히 1.0(중립). 시설이 있을수록 1.0보다 작아진다."""
    baseline = risk_probability(crime_grade, {})
    actual = risk_probability(crime_grade, counts)
    return max(MIN_COST_FACTOR, 1 - COST_SLOPE * (baseline - actual))


def level(probability):
    return "낮음" if probability < LOW_BELOW else "보통" if probability < MEDIUM_BELOW else "높음"


@lru_cache(maxsize=1)
def _crime_model():
    """(구별 범죄 등급, 구별 중심 좌표). 데이터를 못 읽으면 ({}, {}) → 범죄 항은 중립."""
    import routing
    import risk
    try:
        scores = risk.load_district_crime_scores(str(routing.DATA_DIR / "지역범죄데이터.csv"))
    except (OSError, ValueError, KeyError, IndexError):
        return {}, {}
    sums = {}
    for item in routing._facilities()[0]:
        if item["type"] != "CCTV":
            continue
        district = risk.extract_district(item.get("address", ""))
        if district:
            total = sums.setdefault(district, [0.0, 0.0, 0])
            total[0] += item["lat"]
            total[1] += item["lon"]
            total[2] += 1
    centroids = {name: (t[0] / t[2], t[1] / t[2]) for name, t in sums.items() if t[2]}
    return scores, centroids


def crime_grade_at(lat, lon):
    """가장 가까운 자치구 중심의 범죄 등급(0~5). 알 수 없으면 None."""
    scores, centroids = _crime_model()
    best, best_d = None, math.inf
    for name, (clat, clon) in centroids.items():
        if name in scores:
            d = (lat - clat) ** 2 + ((lon - clon) * math.cos(math.radians(lat))) ** 2
            if d < best_d:
                best, best_d = scores[name], d
    return best


def facility_counts(items):
    return Counter(item["type"] for item in items if "type" in item)


def route_risk(coordinates, nearby):
    """경로를 50m 이하 구간으로 나눠 길이 가중 평균 위험 지표를 구한다.

    nearby(lat, lon) → 그 지점 50m 안의 시설 목록.
    """
    import routing
    total = weighted = 0.0
    crimes = []
    for a, b in zip(coordinates, coordinates[1:]):
        length = routing.distance_m(a, b)
        if length <= 0:
            continue
        parts = max(1, math.ceil(length / 50))
        for part in range(parts):
            mid = (a[0] + (b[0] - a[0]) * (part + .5) / parts, a[1] + (b[1] - a[1]) * (part + .5) / parts)
            crime = crime_grade_at(*mid)
            if crime is not None:
                crimes.append(crime)
            p = risk_probability(crime, facility_counts(nearby(*mid)))
            weighted += p * length / parts
            total += length / parts
    if total <= 0:
        return None
    probability = weighted / total
    return {
        "model": "logistic",
        "probability": round(probability, 3),
        "percent": round(probability * 100),
        "level": level(probability),
        "crime_grade": round(sum(crimes) / len(crimes), 2) if crimes else None,
        "note": "등록된 범죄 등급과 시설 정보로 계산한 상대 지표예요. 실제 범죄 발생 확률이 아니에요.",
    }
