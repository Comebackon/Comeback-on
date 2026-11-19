"""외부 API에 실제 요청하지 않는 경로·제약 검증."""
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import routing
import tmap_client


def feature(a, b, coordinates, **properties):
    return {"type": "Feature", "properties": {"from": a, "to": b, "walkable": True, **properties}, "geometry": {"type": "LineString", "coordinates": coordinates}}


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def north_facilities(lat, lon, **kwargs):
    return [{"type": "CCTV"}, {"type": "가로등"}, {"type": "비상벨"}] if lat > 37.0002 else []


class RouteTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(routing, "VIA_CANDIDATES", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_observations_have_no_penalty(self):
        graph, _ = routing.build_graph(collection(feature("a", "b", [[127, 37], [127.001, 37]])))
        edge = graph["a"][0]
        self.assertEqual(edge.cost, edge.duration)

    def test_unverified_penalty_is_ignored(self):
        graph, _ = routing.build_graph(collection(feature("a", "b", [[127, 37], [127.001, 37]], attention_penalty_s=100, attention_reason="미확인 제보")))
        self.assertEqual(graph["a"][0].cost, graph["a"][0].duration)

    def test_verified_penalty_changes_choice(self):
        graph, _ = routing.build_graph(collection(
            feature("a", "b", [[127, 37], [127.001, 37]], verified_attention=True, attention_penalty_s=500, attention_reason="현장 확인된 공사 구간"),
            feature("a", "c", [[127, 37], [127.0005, 37.0003]]),
            feature("c", "b", [[127.0005, 37.0003], [127.001, 37]])))
        result = routing.dijkstra(graph, "a", "b")
        self.assertEqual([edge.target for edge in result], ["c", "b"])

    def test_pareto_labels_find_feasible_path_even_if_cheapest_too_slow(self):
        # At b the cheap path is slow. Preserve the more costly fast path too.
        e = lambda target, duration, cost: routing.Edge(target, [], duration, duration, cost)
        graph = {"a": [e("b", 8, 1), e("c", 2, 4)], "c": [e("b", 2, 1)], "b": [e("d", 5, 1)], "d": []}
        path = routing.dijkstra(graph, "a", "d", max_duration=10)
        self.assertEqual([edge.target for edge in path], ["c", "b", "d"])
        self.assertLessEqual(sum(edge.duration for edge in path), 10)

    def test_disconnected_graph_never_creates_a_line(self):
        graph, _ = routing.build_graph(collection(
            feature("a", "b", [[127, 37], [127.001, 37]]),
            feature("c", "d", [[127.002, 37], [127.003, 37]])))
        self.assertIsNone(routing.dijkstra(graph, "a", "d"))

    def test_nonwalkable_and_private_links_excluded(self):
        graph, _ = routing.build_graph(collection(
            feature("a", "b", [[127, 37], [127.001, 37]], walkable=False),
            feature("c", "d", [[127.002, 37], [127.003, 37]], access="private")))
        self.assertEqual(graph, {})

    def test_stairs_and_one_way(self):
        graph, _ = routing.build_graph(collection(feature("a", "b", [[127, 37], [127.001, 37]], oneway=True, stairs=True)))
        self.assertIsNone(routing.dijkstra(graph, "b", "a"))
        self.assertIsNone(routing.dijkstra(graph, "a", "b", avoid_stairs=True))
        self.assertIsNotNone(routing.dijkstra(graph, "a", "b"))

    def test_mismatched_node_locations_rejected(self):
        with self.assertRaises(routing.RoutingError):
            routing.build_graph(collection(
                feature("a", "b", [[127, 37], [127.001, 37]]),
                feature("b", "c", [[127.002, 37], [127.003, 37]])))

    @patch("routing.get_app_key", return_value="")
    def test_no_key_is_explicit_not_demo_route(self, key):
        with self.assertRaisesRegex(routing.RoutingError, "TMAP_APP_KEY"):
            routing.compare_routes(self.payload())

    def payload(self):
        return {"start": {"name": "출발", "lat": 37, "lon": 127}, "end": {"name": "도착", "lat": 37, "lon": 127.001}}

    def tmap_response(self, duration=1000):
        return {"features": [
            {"properties": {"totalDistance": 1000, "totalTime": duration}, "geometry": {"type": "Point", "coordinates": [127, 37]}},
            {"properties": {}, "geometry": {"type": "LineString", "coordinates": [[127, 37], [127.001, 37]]}}]}

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({}, ["등록 정보 부족"]))
    @patch("routing.request_pedestrian_route")
    def test_identical_provider_candidates_return_only_actual_route(self, provider, evidence):
        provider.return_value = self.tmap_response()
        result = routing.compare_routes(self.payload())
        self.assertTrue(result["safe_available"])
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(result["routes"][0]["id"], "safe")
        self.assertEqual(result["routes"][0]["name"], "안심 경로 · 최단 경로")
        self.assertTrue(result["routes"][0]["same_as_shortest"])
        self.assertEqual(result["routes"][0]["coordinates"], [[37, 127], [37, 127.001]])
        self.assertEqual(result["detour"]["max_duration_s"], 1300)
        self.assertIn("같은 경로", result["warnings"][0])
        self.assertTrue(result["safe_is_shortest"])
        self.assertIn("최단 경로가 곧 안심 경로", result["safe_message"])
        self.assertEqual([call.kwargs["search_option"] for call in provider.call_args_list], ["10", "4"])

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({}, []))
    @patch("routing.request_pedestrian_route")
    def test_both_detour_caps_and_stair_option(self, provider, evidence):
        provider.return_value = self.tmap_response(duration=6000)
        result = routing.compare_routes({**self.payload(), "avoid_stairs": True})
        self.assertEqual(result["detour"]["max_duration_s"], 7200)
        self.assertEqual(provider.call_args.kwargs["search_option"], "30")
        self.assertEqual(provider.call_count, 1)

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({"CCTV": 3}, ["등록 CCTV 3곳"]))
    @patch("routing.nearby_facilities")
    @patch("routing.request_pedestrian_route")
    def test_real_candidate_with_observed_support_can_be_selected(self, provider, facilities, evidence):
        alt = self.tmap_response(duration=1110)  # 후보 거리에 비례한 시간(같은 보행 속도)
        alt["features"][1]["geometry"]["coordinates"] = [[127, 37], [127.0005, 37.0002], [127.001, 37]]
        provider.side_effect = [self.tmap_response(), alt]
        facilities.side_effect = lambda lat, lon, **kwargs: ([{"type": "CCTV"}, {"type": "가로등"}, {"type": "비상벨"}] if lat > 37.00005 else [])
        result = routing.compare_routes(self.payload())
        self.assertTrue(result["safe_available"])
        self.assertEqual(result["routes"][0]["selection_mode"], "provider_candidates")
        self.assertEqual(result["routes"][0]["coordinates"], [[37, 127], [37.0002, 127.0005], [37, 127.001]])
        self.assertLessEqual(result["routes"][0]["duration_s"], result["detour"]["max_duration_s"])
        self.assertEqual(provider.call_count, 2)

    @patch("routing.nearby_facilities", return_value=[])
    def test_shared_road_costs_the_same_in_every_candidate(self, facilities):
        # 같은 길인데 후보마다 TMAP 속도 추정이 달라도 비용은 같아야 한다(회귀: 공통 구간이 선택을 뒤집음).
        coords = [[37, 127], [37, 127.001], [37, 127.002]]
        slow = {"coordinates": coords, "duration_s": 200}
        fast = {"coordinates": coords, "duration_s": 180}
        graph = routing._candidate_graph([slow, fast])
        def total(route_index):
            edges, node, seen = [], "start", set()
            for edge in graph["start"]:
                if edge.reason.startswith(str(route_index)):
                    edges.append(edge); node = edge.target
                    while graph.get(node):
                        edge = graph[node][0]; edges.append(edge); node = edge.target
                    break
            return sum(e.cost for e in edges)
        self.assertAlmostEqual(total(0), total(1), places=6)

    def _run_with_risks(self, base_risk, safe_risk):
        alt = self.tmap_response(duration=1110)
        alt["features"][1]["geometry"]["coordinates"] = [[127, 37], [127.0005, 37.0002], [127.001, 37]]
        def risk(probability, zones):
            return ({"model": "logistic", "probability": probability, "percent": round(probability * 100), "level": "보통", "crime_grade": None, "note": ""}, [{"lat": 37, "lon": 127, "length_m": 50, "percent": 31}] * zones)
        with patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""}), \
             patch("routing.route_evidence", return_value=({"CCTV": 3}, ["등록 CCTV 3곳"])), \
             patch("routing.nearby_facilities", side_effect=lambda lat, lon, **kw: [{"type": "CCTV"}, {"type": "가로등"}, {"type": "비상벨"}] if lat > 37.00005 else []), \
             patch("routing.request_pedestrian_route", side_effect=[self.tmap_response(), alt]), \
             patch("routing.safety_model.assess_route", side_effect=lambda coords, nearby: risk(*base_risk) if coords == [[37, 127], [37, 127.001]] else risk(*safe_risk)):
            return routing.compare_routes(self.payload())

    def test_safe_label_requires_lower_risk(self):
        worse = self._run_with_risks((0.15, 5), (0.16, 6))
        self.assertTrue(worse["safe_is_shortest"])
        self.assertEqual(len(worse["routes"]), 1)
        self.assertEqual(worse["routes"][0]["selection_mode"], "same_as_shortest")

    def test_safe_label_requires_no_more_caution_zones(self):
        more_zones = self._run_with_risks((0.20, 1), (0.10, 2))
        self.assertTrue(more_zones["safe_is_shortest"])
        self.assertEqual(len(more_zones["routes"]), 1)

    def test_lower_risk_candidate_is_labelled_safe(self):
        better = self._run_with_risks((0.20, 2), (0.10, 1))
        self.assertFalse(better["safe_is_shortest"])
        self.assertEqual(better["routes"][0]["id"], "safe")
        self.assertEqual(better["routes"][0]["selection_mode"], "provider_candidates")
        self.assertEqual(len(better["routes"]), 2)

    def line_response(self, lonlat, distance, duration):
        return {"features": [
            {"properties": {"totalDistance": distance, "totalTime": duration}, "geometry": {"type": "Point", "coordinates": lonlat[0]}},
            {"properties": {}, "geometry": {"type": "LineString", "coordinates": lonlat}}]}

    def test_via_points_prefer_facility_rich_places_and_skip_short_trips(self):
        with patch("routing.nearby_facilities", side_effect=north_facilities), patch.object(routing, "VIA_CANDIDATES", 2):
            points = routing._via_points((37, 127), (37, 127.01))
            self.assertTrue(1 <= len(points) <= 2)
            self.assertTrue(all(lat > 37.0002 for lat, lon, score in points))
            self.assertEqual(routing._via_points((37, 127), (37, 127.001)), [])  # 90m: 우회할 이유 없음
        with patch("routing.nearby_facilities", return_value=[]), patch.object(routing, "VIA_CANDIDATES", 2):
            self.assertEqual(routing._via_points((37, 127), (37, 127.01)), [])  # 시설이 없으면 후보도 없음

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({"CCTV": 3}, ["등록 CCTV 3곳"]))
    @patch("routing.request_pedestrian_route")
    def test_facility_via_candidate_gives_a_different_safe_route(self, provider, evidence):
        base = self.line_response([[127, 37], [127.01, 37]], 890, 800)
        via = self.line_response([[127, 37], [127.005, 37.002], [127.01, 37]], 995, 900)
        def respond(*args, **kwargs):
            if kwargs.get("pass_list"):
                return via
            return base  # 최단·대로우선은 같은 길
        provider.side_effect = respond
        payload = {"start": {"name": "출발", "lat": 37, "lon": 127}, "end": {"name": "도착", "lat": 37, "lon": 127.01}}
        with patch("routing.nearby_facilities", side_effect=north_facilities), patch.object(routing, "VIA_CANDIDATES", 2):
            result = routing.compare_routes(payload)
        self.assertFalse(result["safe_is_shortest"])
        self.assertEqual([r["id"] for r in result["routes"]], ["safe", result["routes"][1]["id"]])
        self.assertEqual(result["routes"][0]["coordinates"], [[37, 127], [37.002, 127.005], [37, 127.01]])
        self.assertEqual(result["routes"][0]["selection_mode"], "provider_candidates")
        self.assertLess(result["routes"][0]["risk"]["probability"], result["routes"][1]["risk"]["probability"])
        self.assertLessEqual(result["routes"][0]["duration_s"], result["detour"]["max_duration_s"])
        self.assertTrue(any(call.kwargs.get("pass_list") for call in provider.call_args_list))

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({}, []))
    @patch("routing.request_pedestrian_route")
    def test_via_candidate_over_time_budget_is_ignored(self, provider, evidence):
        base = self.line_response([[127, 37], [127.01, 37]], 890, 800)
        slow = self.line_response([[127, 37], [127.005, 37.002], [127.01, 37]], 995, 5000)
        provider.side_effect = lambda *a, **k: slow if k.get("pass_list") else base
        payload = {"start": {"name": "출발", "lat": 37, "lon": 127}, "end": {"name": "도착", "lat": 37, "lon": 127.01}}
        with patch("routing.nearby_facilities", side_effect=north_facilities), patch.object(routing, "VIA_CANDIDATES", 2):
            result = routing.compare_routes(payload)
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(result["routes"][0]["coordinates"], [[37, 127], [37, 127.01]])

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({}, []))
    @patch("routing.request_pedestrian_route")
    def test_via_request_failure_keeps_working(self, provider, evidence):
        base = self.line_response([[127, 37], [127.01, 37]], 890, 800)
        def respond(*a, **k):
            if k.get("pass_list"):
                raise OSError("down")
            return base
        provider.side_effect = respond
        payload = {"start": {"name": "출발", "lat": 37, "lon": 127}, "end": {"name": "도착", "lat": 37, "lon": 127.01}}
        with patch("routing.nearby_facilities", side_effect=north_facilities), patch.object(routing, "VIA_CANDIDATES", 2):
            result = routing.compare_routes(payload)
        self.assertTrue(result["safe_is_shortest"])

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({}, []))
    @patch("routing.nearby_facilities", return_value=[])
    @patch("routing.request_pedestrian_route")
    def test_candidate_without_observations_is_neutral(self, provider, facilities, evidence):
        alt = self.tmap_response(duration=1100)
        alt["features"][1]["geometry"]["coordinates"] = [[127, 37], [127.0005, 37.001], [127.001, 37]]
        provider.side_effect = [self.tmap_response(), alt]
        result = routing.compare_routes(self.payload())
        self.assertTrue(result["safe_is_shortest"])
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(result["routes"][0]["selection_mode"], "same_as_shortest")

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test", "ROUTING_GRAPH_FILE": ""})
    @patch("routing.route_evidence", return_value=({}, []))
    @patch("routing.request_pedestrian_route")
    def test_over_budget_candidate_not_offered(self, provider, evidence):
        provider.side_effect = [self.tmap_response(), self.tmap_response(duration=1301)]
        result = routing.compare_routes(self.payload())
        self.assertFalse(result["safe_available"])
        self.assertIn("우회 시간", result["warnings"][0])
        self.assertFalse(result["safe_is_shortest"])
        self.assertEqual(result["safe_message"], "")

    def test_unrelated_verified_penalty_does_not_create_safe_claim(self):
        doc = collection(
            feature("a", "b", [[127, 37], [127.001, 37]]),
            feature("x", "y", [[127.01, 37], [127.011, 37]], verified_attention=True, attention_penalty_s=500, attention_reason="다른 지역 공사"))
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "roads.json"
            filename.write_text(json.dumps(doc))
            with patch.dict(os.environ, {"ROUTING_GRAPH_FILE": str(filename)}):
                safe, warning = routing._safe_route({}, (37, 127), (37, 127.001), 1000, False)
        self.assertIsNone(safe)
        self.assertIn("별도의", warning)

    def test_input_bounds_and_nonfinite_rejected_before_network(self):
        for change in ({"detour_percent": -1}, {"max_extra_minutes": 61}, {"detour_percent": float("nan")}, {"avoid_stairs": "yes"}, {"start": {"lat": float("inf"), "lon": 127}}):
            with self.subTest(change=change), self.assertRaises(routing.RoutingError):
                routing.compare_routes({**self.payload(), **change})

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test-key"})
    @patch("tmap_client.urlopen")
    def test_existing_tmap_client_default_is_backwards_compatible(self, network):
        network.side_effect = lambda *args, **kwargs: io.BytesIO(b'{"features":[]}')
        tmap_client.request_pedestrian_route(127, 37, 127.001, 37)
        request = network.call_args.args[0]
        self.assertEqual(json.loads(request.data)["searchOption"], "0")
        tmap_client.request_pedestrian_route(127, 37, 127.001, 37, search_option="30")
        self.assertEqual(json.loads(network.call_args.args[0].data)["searchOption"], "30")

    @patch.dict(os.environ, {"TMAP_APP_KEY": "test-key"})
    @patch("routing.urlopen")
    def test_search_uses_pedestrian_entrance(self, network):
        routing._search_cache.clear()
        response = {"searchPoiInfo": {"pois": {"poi": [{"name": "미림마이스터고등학교", "pnsLat": "37.466", "pnsLon": "126.932", "noorLat": "37.467", "noorLon": "126.933", "newAddressList": {"newAddress": [{"fullAddressRoad": "서울 관악구"}]}}]}}}
        network.return_value = io.BytesIO(json.dumps(response).encode())
        result = routing.search_places("미림마고")
        self.assertEqual(result[0]["lat"], 37.466)
        self.assertEqual(result[0]["address"], "서울 관악구")
        self.assertEqual(routing.search_places("미림마고"), result)
        self.assertEqual(network.call_count, 1)

    @patch("routing._facilities")
    def test_facility_radius_and_distances(self, facilities):
        rows = [{"id": "near", "type": "CCTV", "lat": 37.0, "lon": 127.0}, {"id": "far", "type": "CCTV", "lat": 37.0, "lon": 127.01}]
        facilities.return_value = (rows, {(math.floor(37/.005), math.floor(127/.005)): [0], (math.floor(37/.005), math.floor(127.01/.005)): [1]})
        found = routing.nearby_facilities(37, 127, radius=100)
        self.assertEqual([item["id"] for item in found], ["near"])
        self.assertEqual(found[0]["distance_m"], 0)


if __name__ == "__main__":
    unittest.main()


class FacilityPointsTests(unittest.TestCase):
    @patch("routing.nearby_facilities")
    def test_points_are_deduplicated_and_capped(self, facilities):
        items = [{"id": f"c:{i}", "type": "CCTV", "lat": 37 + i * 1e-5, "lon": 127, "name": "CCTV"} for i in range(300)]
        facilities.return_value = items
        points = routing.route_facility_points([[37, 127], [37.01, 127]], cap=50)
        self.assertEqual(len(points), 50)
        self.assertEqual(len({(p["lat"], p["lon"]) for p in points}), 50)
        facilities.return_value = items[:3]
        self.assertEqual(len(routing.route_facility_points([[37, 127], [37.01, 127]])), 3)


class StepTests(unittest.TestCase):
    def test_tmap_turn_points_become_steps(self):
        raw = {"features": [
            {"properties": {"totalDistance": 300, "totalTime": 300, "turnType": 200, "description": "출발"}, "geometry": {"type": "Point", "coordinates": [127, 37]}},
            {"properties": {"turnType": 13, "description": "  우회전 후   직진 "}, "geometry": {"type": "Point", "coordinates": [127.001, 37]}},
            {"properties": {"turnType": 201, "description": "도착"}, "geometry": {"type": "Point", "coordinates": [127.002, 37]}},
            {"properties": {}, "geometry": {"type": "LineString", "coordinates": [[127, 37], [127.001, 37], [127.002, 37]]}}]}
        route = routing._tmap_route(raw)
        self.assertEqual([(s["index"], s["turn"], s["text"]) for s in route["steps"]], [(1, 13, "우회전 후 직진"), (2, 201, "도착")])

    def test_missing_or_bad_turn_data_gives_no_steps(self):
        raw = {"features": [
            {"properties": {"totalDistance": 100, "totalTime": 100}, "geometry": {"type": "Point", "coordinates": [127, 37]}},
            {"properties": {}, "geometry": {"type": "LineString", "coordinates": [[127, 37], [127.001, 37]]}}]}
        self.assertEqual(routing._tmap_route(raw)["steps"], [])
