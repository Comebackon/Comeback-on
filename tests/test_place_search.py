"""장소 검색 결과 누락·오류 구분 회귀 검사. 실제 키/외부 호출을 쓰지 않는다."""
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

import routing


@pytest.fixture(autouse=True)
def isolated_search(monkeypatch):
    routing._search_cache.clear()
    monkeypatch.setattr(routing, "get_app_key", lambda: "synthetic-search-key")
    yield
    routing._search_cache.clear()


def response(monkeypatch, payload):
    monkeypatch.setattr(routing, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))


def pois(*items):
    return {"searchPoiInfo": {"totalCount": str(len(items)), "pois": {"poi": list(items)}}}


def test_zero_entry_coordinates_fall_back_to_valid_representative_pair(monkeypatch):
    response(monkeypatch, pois({"name": "학교", "pnsLat": "0", "pnsLon": "0", "noorLat": "37.466", "noorLon": "126.932"}))
    result = routing.search_places("학교")
    assert [(p["lat"], p["lon"]) for p in result] == [(37.466, 126.932)]


def test_incomplete_pairs_are_never_mixed(monkeypatch):
    response(monkeypatch, pois({"name": "학교", "pnsLat": "37.6", "pnsLon": "", "noorLat": "37.5", "noorLon": "127"}))
    result = routing.search_places("학교")
    assert (result[0]["lat"], result[0]["lon"]) == (37.5, 127)


@pytest.mark.parametrize("optional_address", [None, {}, {"newAddress": None}, {"newAddress": [None]}, []])
def test_optional_address_and_malformed_items_do_not_hide_valid_places(monkeypatch, optional_address):
    response(monkeypatch, pois(None, {"name": "역", "frontLat": "37.484", "frontLon": "126.929", "newAddressList": optional_address, "upperAddrName": "서울", "middleAddrName": "관악구"}))
    result = routing.search_places("신림역")
    assert len(result) == 1
    assert result[0]["address"] == "서울 관악구"


def test_zero_results_are_distinct_from_invalid_provider_response(monkeypatch):
    response(monkeypatch, {"searchPoiInfo": {"totalCount": "0", "pois": None}})
    assert routing.search_places("없는장소") == []


@pytest.mark.parametrize("payload", [{"error": {"message": "synthetic-search-key"}}, {}, [], {"searchPoiInfo": {"totalCount": "1", "pois": None}}])
def test_error_or_malformed_response_is_not_cached_as_empty_search(monkeypatch, payload):
    response(monkeypatch, payload)
    with pytest.raises(routing.RoutingError) as error:
        routing.search_places("학교")
    assert "응답 형식" in str(error.value)
    assert "synthetic-search-key" not in str(error.value)
    assert not routing._search_cache


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_provider_http_status_is_visible_without_secret_response_body(monkeypatch, code):
    def fail(*args, **kwargs):
        raise HTTPError(routing.SEARCH_URL, code, "error", {}, io.BytesIO(b'{"key":"synthetic-search-key"}'))
    monkeypatch.setattr(routing, "urlopen", fail)
    with pytest.raises(routing.RoutingError) as error:
        routing.search_places("학교")
    assert f"HTTP {code}" in str(error.value)
    assert "synthetic-search-key" not in str(error.value)


def test_network_failure_does_not_expose_raw_error_or_claim_invalid_key(monkeypatch):
    def fail(*args, **kwargs):
        raise URLError("synthetic-search-key")
    monkeypatch.setattr(routing, "urlopen", fail)
    with pytest.raises(routing.RoutingError, match="인터넷 연결") as error:
        routing.search_places("학교")
    assert "synthetic-search-key" not in str(error.value)


def test_nearby_search_requests_distance_order_and_sorts_before_limiting(monkeypatch):
    requests = []
    payload = pois(*[
        {"name": f"카페 {index}", "pnsLat": 37.5 + index * .001, "pnsLon": 127}
        for index in range(7, 0, -1)
    ])

    def provider(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    result = routing.search_places("카페", lat=37.5, lon=127)

    assert [item["name"] for item in result] == [f"카페 {index}" for index in range(1, 6)]
    assert [item["distance_m"] for item in result] == sorted(item["distance_m"] for item in result)
    assert result[0]["distance_m"] == pytest.approx(111.2, abs=1)
    params = parse_qs(urlsplit(requests[0].full_url).query)
    assert params["searchKeyword"] == ["카페"]
    assert params["searchtypCd"] == ["R"]
    assert params["radius"] == ["0"]
    assert float(params["centerLat"][0]) == 37.5
    assert float(params["centerLon"][0]) == 127
    assert params["reqCoordType"] == ["WGS84GEO"]
    assert params["resCoordType"] == ["WGS84GEO"]
    assert params["count"] == ["20"]


def test_search_without_location_preserves_provider_relevance_order(monkeypatch):
    requests = []
    payload = pois(*[
        {"name": f"카페 {index}", "noorLat": 37.5 + index * .001, "noorLon": 127}
        for index in range(7, 0, -1)
    ])

    def provider(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    result = routing.search_places("카페")

    assert [item["name"] for item in result] == [f"카페 {index}" for index in range(7, 2, -1)]
    assert all("distance_m" not in item for item in result)
    params = parse_qs(urlsplit(requests[0].full_url).query)
    assert params.get("searchtypCd", ["A"]) == ["A"]
    assert "centerLat" not in params and "centerLon" not in params


def test_search_cache_keeps_origins_and_relevance_results_separate(monkeypatch):
    requests = []
    payload = pois(
        {"name": "남쪽 카페", "noorLat": 37.5, "noorLon": 127},
        {"name": "북쪽 카페", "noorLat": 37.6, "noorLon": 127},
    )

    def provider(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    south = routing.search_places("카페", lat=37.5, lon=127)
    north = routing.search_places("카페", lat=37.6, lon=127)
    relevance = routing.search_places("카페")

    assert south[0]["name"] == "남쪽 카페"
    assert north[0]["name"] == "북쪽 카페"
    assert south[0]["distance_m"] == north[0]["distance_m"] == 0
    assert all("distance_m" not in item for item in relevance)
    assert routing.search_places("카페", lat=37.5, lon=127) == south
    assert routing.search_places("카페", lat=37.6, lon=127) == north
    assert routing.search_places("카페") == relevance
    assert len(requests) == 3


@pytest.mark.parametrize("lat,lon", [
    (37.5, None), (None, 127), ("", 127), ("not-a-number", 127),
    (float("nan"), 127), (37.5, float("inf")), (91, 127),
    (37.5, 181), (True, 127), (37.5, False),
])
def test_invalid_search_origin_is_rejected_before_provider(monkeypatch, lat, lon):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Invalid coordinates must not reach TMap")

    monkeypatch.setattr(routing, "urlopen", unexpected_call)
    with pytest.raises(routing.RoutingError):
        routing.search_places("카페", lat=lat, lon=lon)
    assert not routing._search_cache


@pytest.mark.parametrize("origin", [{}, {"lat": 37.5, "lon": 127}])
def test_http_204_empty_body_means_no_search_results(monkeypatch, origin):
    def provider(*args, **kwargs):
        result = io.BytesIO(b"")
        result.status = 204
        return result

    monkeypatch.setattr(routing, "urlopen", provider)
    assert routing.search_places("없는장소", **origin) == []


def test_no_content_cache_is_separate_for_each_origin_and_regular_search(monkeypatch):
    requests = []

    def provider(request, **kwargs):
        requests.append(request)
        result = io.BytesIO(b"")
        result.status = 204
        return result

    monkeypatch.setattr(routing, "urlopen", provider)
    origins = [{}, {"lat": 37.5, "lon": 127}, {"lat": 37.6, "lon": 127}]
    for origin in origins:
        assert routing.search_places("없는장소", **origin) == []
    for origin in origins:
        assert routing.search_places("없는장소", **origin) == []
    assert len(requests) == 3


@pytest.mark.parametrize("body", [b"", b" \n", b"<html>synthetic-search-key</html>", b'{"incomplete":', b"\xff"])
def test_http_200_empty_or_malformed_body_still_errors_and_is_not_cached(monkeypatch, body):
    def provider(*args, **kwargs):
        result = io.BytesIO(body)
        result.status = 200
        return result

    monkeypatch.setattr(routing, "urlopen", provider)
    with pytest.raises(routing.RoutingError) as error:
        routing.search_places("학교")
    assert "응답" in str(error.value)
    assert "synthetic-search-key" not in str(error.value)
    assert not routing._search_cache


def test_hangul_and_space_keyword_is_utf8_encoded_once(monkeypatch):
    requests = []
    query = "서울역 1번 출구"

    def provider(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(pois()).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    assert routing.search_places(query) == []
    encoded_query = urlsplit(requests[0].full_url).query
    assert parse_qs(encoded_query)["searchKeyword"] == [query]
    assert "%EC%84%9C%EC%9A%B8%EC%97%AD" in encoded_query
    assert "%25EC" not in encoded_query


@pytest.mark.parametrize("query", ["미림마이", "미림마이스", "미림마이스터고등학교", " 미림 마이 ", "미림마고", "미림마이스터고"])
def test_known_school_prefix_searches_full_name_without_extra_request(monkeypatch, query):
    requests = []

    def provider(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(pois({"name": "미림마이스터고등학교", "pnsLat": 37.466, "pnsLon": 126.932})).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    result = routing.search_places(query)
    assert result[0]["name"] == "미림마이스터고등학교"
    assert len(requests) == 1
    assert parse_qs(urlsplit(requests[0].full_url).query)["searchKeyword"] == ["미림마이스터고등학교"]


@pytest.mark.parametrize("query", ["미림", "미림고등학교", "미림마이 카페"])
def test_ambiguous_or_unrelated_queries_are_not_rewritten(monkeypatch, query):
    requests = []

    def provider(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(pois()).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    assert routing.search_places(query) == []
    assert len(requests) == 1
    assert parse_qs(urlsplit(requests[0].full_url).query)["searchKeyword"] == [query]


@pytest.mark.parametrize("empty_status", [200, 204])
@pytest.mark.parametrize("origin", [{}, {"lat": 37.5, "lon": 127}])
def test_school_search_tries_former_name_only_after_no_results_and_caches_aliases(monkeypatch, empty_status, origin):
    requests = []
    payload = pois(
        {"name": "학교 후문", "pnsLat": 37.47, "pnsLon": 126.93},
        {"name": "미림여자정보과학고등학교", "pnsLat": 37.49, "pnsLon": 126.99},
    )

    def provider(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            result = io.BytesIO(b"" if empty_status == 204 else json.dumps(pois()).encode())
            result.status = empty_status
            return result
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    result = routing.search_places("미림마이", **origin)
    assert len(requests) == 2
    params = [parse_qs(urlsplit(request.full_url).query) for request in requests]
    assert [p.pop("searchKeyword") for p in params] == [["미림마이스터고등학교"], ["미림여자정보과학고등학교"]]
    assert params[0] == params[1]
    assert all(request.get_header("Appkey") == "synthetic-search-key" for request in requests)
    assert {(p["lat"], p["lon"]) for p in result} == {(37.47, 126.93), (37.49, 126.99)}
    if origin:
        assert result[0]["name"] == "미림여자정보과학고등학교"
        assert result[0]["distance_m"] < result[1]["distance_m"]
        assert params[1]["searchtypCd"] == ["R"]
        assert float(params[1]["centerLat"][0]) == origin["lat"]
        assert float(params[1]["centerLon"][0]) == origin["lon"]
    else:
        assert all("distance_m" not in p for p in result)
    assert routing.search_places("미림마이스터고등학교", **origin) == result
    assert routing.search_places("미림마고", **origin) == result
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["auth", "network", "malformed", "invalid_schema", "invalid_coordinates"])
def test_school_search_does_not_hide_provider_errors_with_fallback(monkeypatch, failure):
    requests = []

    def provider(request, **kwargs):
        requests.append(request)
        if failure == "auth":
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        if failure == "network":
            raise URLError("Unavailable")
        if failure == "malformed":
            return io.BytesIO(b"not-json")
        payload = {} if failure == "invalid_schema" else pois({"name": "학교", "pnsLat": 0, "pnsLon": 0})
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    with pytest.raises(routing.RoutingError):
        routing.search_places("미림마이")
    assert len(requests) == 1
    assert not routing._search_cache


def test_school_search_stops_after_both_names_have_no_results(monkeypatch):
    requests = []

    def provider(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(pois()).encode())

    monkeypatch.setattr(routing, "urlopen", provider)
    assert routing.search_places("미림마이") == []
    assert routing.search_places("미림마이스터고등학교") == []
    assert len(requests) == 2

