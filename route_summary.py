"""경로 추천 이유 한 줄 요약.

멘토링 1회차 방향: 생성형 AI는 경로를 정하지 않고, 이미 계산된 근거를 쉬운 말로 요약하는
보조 역할만 한다. 경로 선택(로지스틱 위험 지표 + 다익스트라)에는 관여하지 않는다.

- ANTHROPIC_API_KEY가 있으면 Claude API로 요약하고, 없거나 실패하면 템플릿 문장을 쓴다.
- 모델에는 서버가 검증한 숫자만 보낸다(사용자가 쓴 글, 장소 이름은 보내지 않는다).
"""

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
log = logging.getLogger(__name__)
TIMEOUT_S = 15
MAX_CHARS = 300
FACILITY_KINDS = ("CCTV", "가로등", "비상벨", "경찰시설", "편의점", "안전시설물")
ROUTE_IDS = {"safe": "안심 경로", "basic": "최단 경로"}

SYSTEM_PROMPT = (
    "너는 보행자 안심 귀가 앱의 안내 문장 작성기야. 주어진 JSON 숫자만 사용해서 한국어로 "
    "추천 이유를 1~2문장, 120자 이내로 요약해. 규칙: 1) JSON에 없는 사실은 쓰지 마. "
    "2) '안전하다', '위험하지 않다'처럼 안전을 보장하는 표현은 쓰지 마. "
    "3) 시설 정보가 없다는 것을 위험하다는 뜻으로 해석하지 마. "
    "4) risk_percent는 실제 범죄 확률이 아니라 등록 정보 기반 상대 지표라고 다뤄. "
    "5) 경로 이름은 '안심 경로', '최단 경로'만 써. 이모지와 마크다운은 쓰지 마."
)

_cache = OrderedDict()
_lock = threading.Lock()


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def clean_facts(routes):
    """클라이언트가 보낸 경로 목록에서 화이트리스트 숫자만 남긴다. 형식이 틀리면 None."""
    if not isinstance(routes, list) or not 1 <= len(routes) <= 2:
        return None
    facts = []
    for route in routes:
        if not isinstance(route, dict) or route.get("id") not in ROUTE_IDS:
            return None
        try:
            duration, distance = float(route["duration_s"]), float(route["distance_m"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 < duration < 36000 and 0 < distance < 60000):
            return None
        counts = route.get("facility_counts") if isinstance(route.get("facility_counts"), dict) else {}
        clean_counts = {}
        for kind in FACILITY_KINDS:
            value = counts.get(kind, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 5000:
                value = 0
            if value:
                clean_counts[kind] = int(value)
        risk = route.get("risk") if isinstance(route.get("risk"), dict) else {}
        percent = risk.get("percent")
        percent = int(percent) if isinstance(percent, (int, float)) and not isinstance(percent, bool) and 0 <= percent <= 100 else None
        item = {"route": ROUTE_IDS[route["id"]], "minutes": round(duration / 60), "meters": round(distance), "facilities": clean_counts}
        if percent is not None:
            item["risk_percent"] = percent
        if route.get("same_as_shortest") is True:
            item["same_as_shortest"] = True
        facts.append(item)
    return facts


def _duration_text(minutes):
    hours, rest = divmod(int(minutes), 60)
    if not hours:
        return f"{rest}분"
    return f"{hours}시간 {rest}분" if rest else f"{hours}시간"


def template_summary(facts):
    """항상 쓸 수 있는 기본 문장. LLM이 없거나 실패했을 때의 대체 문장이기도 하다."""
    def facility_text(item):
        parts = [f"{kind} {count}곳" for kind, count in item["facilities"].items() if kind in ("CCTV", "가로등", "비상벨", "경찰시설")]
        return ", ".join(parts)

    first = facts[0]
    text = facility_text(first)
    if first.get("same_as_shortest"):
        base = f"비교한 후보 중 최단 경로가 가장 알맞아 그대로 안내해요({_duration_text(first['minutes'])}, {first['meters']}m)."
        return base + (f" 경로 주변 50m에 등록된 {text}을 확인했어요." if text else "")
    if len(facts) == 2:
        second = facts[1]
        extra = first["minutes"] - second["minutes"]
        gap = f"최단 경로보다 {_duration_text(extra)} 더 걸리지만 " if extra > 0 else ""
        return f"{gap}등록 시설 정보를 함께 비교해 안심 경로를 골랐어요." + (f" 주변 50m에 {text}이 있어요." if text else "")
    return f"이 경로는 {_duration_text(first['minutes'])}, {first['meters']}m예요." + (f" 주변 50m에 등록된 {text}을 확인했어요." if text else "")


def _key(facts):
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def llm_summary(facts):
    """Claude API 요약. 키가 없거나 실패·부적절한 응답이면 None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    key = _key(facts)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
        "max_tokens": 1500,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
    }).encode()
    request = Request(API_URL, data=body, method="POST", headers={"content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    payload = {}
    try:
        with urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read(65536).decode())
        text = "".join(block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict) and block.get("type") == "text").strip()
    except HTTPError as exc:
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        log.warning("AI summary failed: HTTP %s %s", exc.code, detail)
        return None
    except (URLError, OSError, ValueError, AttributeError) as exc:
        log.warning("AI summary failed: %s", exc)
        return None
    if not text:
        blocks = payload.get("content", []) if isinstance(payload, dict) else []
        log.warning("AI summary rejected: empty response (stop_reason=%s, blocks=%s)", payload.get("stop_reason") if isinstance(payload, dict) else None,
                    [b.get("type") if isinstance(b, dict) else type(b).__name__ for b in blocks])
        return None
    if len(text) > MAX_CHARS:
        log.warning("AI summary rejected: too long (%d chars)", len(text))
        return None
    hit = next((b for b in ("안전합니다", "안전해요", "위험하지 않", "보장해요", "```") if b in text), None)
    if hit:
        log.warning("AI summary rejected: banned phrase %r", hit)
        return None
    with _lock:
        _cache[key] = text
        while len(_cache) > 256:
            _cache.popitem(last=False)
    return text


def summarize(routes):
    """(문장, 출처). 출처는 'llm' 또는 'template'. 입력이 올바르지 않으면 None."""
    facts = clean_facts(routes)
    if facts is None:
        return None
    text = llm_summary(facts)
    return (text, "llm") if text else (template_summary(facts), "template")
