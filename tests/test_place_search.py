"""장소 검색 결과 누락·오류 구분 회귀 검사. 실제 키/외부 호출을 쓰지 않는다."""
import io
import json
from urllib.error import HTTPError, URLError

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

