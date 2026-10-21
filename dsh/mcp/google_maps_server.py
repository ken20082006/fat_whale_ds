"""Google Maps MCP server（stdio）—— 只用 **新版** API。

## 為什麼要自己寫

npm 上嘅 Google Maps MCP（`@pipeworx/mcp-google_maps`、HasData）
用**舊版** `/maps/api/...`：

- Geocoding（舊）
- Places API (Legacy)
- Directions API (Legacy)
- Distance Matrix API (Legacy)

Google 由 2025-03-01 起唔再畀**新 project** 啟用 legacy API
（可以填表申請，但唔應該依賴）。實測呢個 project：
Geocoding 通、Places (New) 通、Routes 通 —— 但 legacy Places / Directions /
Distance Matrix / Elevation 全部 `REQUEST_DENIED`。

所以用新版 API 自己寫一個，只依賴標準庫。

## 工具

    maps_geocode         地址 → 座標（Geocoding API）
    maps_place_search    關鍵字／附近搜尋（Places API (New) searchText）
    maps_place_details   店舖詳情：評分、評論數、地址、電話、營業時間（Places (New)）
    maps_directions      路線、時間、距離（Routes API v2）

## 金鑰

由 `GOOGLE_MAPS_API_KEY` 環境變數讀，**唔會**經工具參數或輸出外泄。

## 協議

MCP stdio：newline-delimited JSON-RPC 2.0（每行一個訊息，唔可以含換行）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()

_TIMEOUT = 25


# ── HTTP ────────────────────────────────────────────────


def _request(url: str, *, body: dict | None = None, headers: dict | None = None) -> dict:
    data = None
    hdrs = {"X-Goog-Api-Key": API_KEY}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"Google API {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"連唔到 Google API：{exc.reason}") from exc


def _require_key() -> None:
    if not API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY 未設定")


# ── 工具實作 ────────────────────────────────────────────


def geocode(address: str) -> dict:
    _require_key()
    url = (
        "https://maps.googleapis.com/maps/api/geocode/json?"
        + urllib.parse.urlencode({"address": address, "key": API_KEY})
    )
    data = _request(url)
    if data.get("status") != "OK":
        raise RuntimeError(f"{data.get('status')}: {data.get('error_message', '')}")
    return {
        "results": [
            {
                "address": r.get("formatted_address"),
                "lat": r["geometry"]["location"]["lat"],
                "lng": r["geometry"]["location"]["lng"],
                "place_id": r.get("place_id"),
                "types": r.get("types"),
            }
            for r in data.get("results", [])[:5]
        ]
    }


def place_search(query: str, latitude: float | None = None, longitude: float | None = None,
                 radius_m: int | None = None, language: str = "zh-HK") -> dict:
    _require_key()
    body: dict = {"textQuery": query, "languageCode": language}
    if latitude is not None and longitude is not None:
        circle: dict = {
            "center": {"latitude": latitude, "longitude": longitude},
        }
        if radius_m:
            circle["radius"] = float(radius_m)
        body["locationBias"] = {"circle": circle}
    fields = (
        "places.id,places.displayName,places.formattedAddress,places.rating,"
        "places.userRatingCount,places.location,places.types,places.businessStatus"
    )
    data = _request(
        "https://places.googleapis.com/v1/places:searchText",
        body=body,
        headers={"X-Goog-FieldMask": fields},
    )
    return {
        "places": [
            {
                "place_id": p.get("id"),
                "name": (p.get("displayName") or {}).get("text"),
                "address": p.get("formattedAddress"),
                "rating": p.get("rating"),
                "reviews": p.get("userRatingCount"),
                "lat": (p.get("location") or {}).get("latitude"),
                "lng": (p.get("location") or {}).get("longitude"),
                "types": p.get("types"),
                "status": p.get("businessStatus"),
            }
            for p in data.get("places", [])[:10]
        ]
    }


def place_details(place_id: str, language: str = "zh-HK") -> dict:
    _require_key()
    fields = (
        "id,displayName,formattedAddress,nationalPhoneNumber,internationalPhoneNumber,"
        "websiteUri,rating,userRatingCount,currentOpeningHours,regularOpeningHours,"
        "priceLevel,types,location,editorialSummary,reviews"
    )
    # ⚠️ 新版 Places API 嘅詳情路徑係 `/v1/places/<id>` ——
    # 少咗 `places/` 會 404（實測撞過）。
    url = (
        f"https://places.googleapis.com/v1/places/{place_id}"
        f"?languageCode={urllib.parse.quote(language)}"
    )
    data = _request(url, headers={"X-Goog-FieldMask": fields})

    reviews = []
    for r in (data.get("reviews") or [])[:5]:
        reviews.append({
            "rating": r.get("rating"),
            "text": ((r.get("text") or {}).get("text") or "")[:300],
            "relative_time": r.get("relativePublishTimeDescription"),
        })

    hours = data.get("currentOpeningHours") or data.get("regularOpeningHours") or {}
    return {
        "place_id": data.get("id"),
        "name": (data.get("displayName") or {}).get("text"),
        "address": data.get("formattedAddress"),
        "phone": data.get("nationalPhoneNumber") or data.get("internationalPhoneNumber"),
        "website": data.get("websiteUri"),
        "rating": data.get("rating"),
        "reviews_count": data.get("userRatingCount"),
        "open_now": hours.get("openNow"),
        "weekday_hours": hours.get("weekdayDescriptions"),
        "price_level": data.get("priceLevel"),
        "types": data.get("types"),
        "summary": (data.get("editorialSummary") or {}).get("text"),
        "reviews": reviews,
    }


def directions(origin: str, destination: str, mode: str = "DRIVE",
               language: str = "zh-HK") -> dict:
    _require_key()
    travel = {
        "DRIVE": "DRIVE", "WALK": "WALK", "BICYCLE": "BICYCLE", "TRANSIT": "TRANSIT",
    }.get(mode.upper(), "DRIVE")
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": travel,
        "languageCode": language,
        "computeAlternativeRoutes": True,
    }
    if travel == "DRIVE":
        body["routingPreference"] = "TRAFFIC_AWARE"
    fields = (
        "routes.duration,routes.distanceMeters,routes.description,"
        "routes.legs.steps.navigationInstruction,routes.legs.steps.distanceMeters,"
        "routes.legs.steps.staticDuration"
    )
    data = _request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        body=body,
        headers={"X-Goog-FieldMask": fields},
    )
    routes = []
    for r in (data.get("routes") or [])[:3]:
        steps = []
        for leg in r.get("legs") or []:
            for s in (leg.get("steps") or [])[:30]:
                nav = (s.get("navigationInstruction") or {}).get("instructions")
                if nav:
                    steps.append(nav)
        routes.append({
            "duration_sec": int(str(r.get("duration", "0s")).rstrip("s") or 0),
            "distance_m": r.get("distanceMeters"),
            "description": r.get("description"),
            "steps": steps,
        })
    return {"routes": routes}


# ── 工具宣告 ────────────────────────────────────────────

TOOLS = [
    {
        "name": "maps_geocode",
        "description": "地址轉座標（Google Geocoding）。回傳地址、經緯度、place_id。",
        "inputSchema": {
            "type": "object",
            "properties": {"address": {"type": "string", "description": "地址或地名"}},
            "required": ["address"],
        },
    },
    {
        "name": "maps_place_search",
        "description": (
            "用 Google Places（新版）搵地方／商戶。回傳名稱、地址、評分、評論數、"
            "place_id。要詳細資料就用 place_id 再叫 maps_place_details。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜尋字詞，例如「咖啡店」"},
                "latitude": {"type": "number", "description": "可選：搜尋中心緯度"},
                "longitude": {"type": "number", "description": "可選：搜尋中心經度"},
                "radius_m": {"type": "integer", "description": "可選：半徑（米）"},
                "language": {"type": "string", "description": "語言代碼，預設 zh-HK"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "maps_place_details",
        "description": (
            "用 place_id 攞店舖詳情：評分、評論數、地址、電話、網站、"
            "營業時間（含 open_now）、價位、頭幾條評論。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "place_id": {"type": "string", "description": "由 maps_place_search 攞到嘅 place_id"},
                "language": {"type": "string", "description": "語言代碼，預設 zh-HK"},
            },
            "required": ["place_id"],
        },
    },
    {
        "name": "maps_directions",
        "description": (
            "Google Routes（新版）路線：時間、距離、轉向指示。"
            "mode 可選 DRIVE / WALK / BICYCLE / TRANSIT。揸車會用即時車流估算。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "起點地址或地名"},
                "destination": {"type": "string", "description": "終點地址或地名"},
                "mode": {"type": "string", "description": "DRIVE / WALK / BICYCLE / TRANSIT"},
                "language": {"type": "string", "description": "語言代碼，預設 zh-HK"},
            },
            "required": ["origin", "destination"],
        },
    },
]

_HANDLERS = {
    "maps_geocode": lambda a: geocode(a["address"]),
    "maps_place_search": lambda a: place_search(
        a["query"], a.get("latitude"), a.get("longitude"),
        a.get("radius_m"), a.get("language", "zh-HK"),
    ),
    "maps_place_details": lambda a: place_details(a["place_id"], a.get("language", "zh-HK")),
    "maps_directions": lambda a: directions(
        a["origin"], a["destination"], a.get("mode", "DRIVE"), a.get("language", "zh-HK")
    ),
}


# ── JSON-RPC 處理 ───────────────────────────────────────


def _reply(msg_id, result=None, error=None) -> None:
    frame: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        frame["error"] = error
    else:
        frame["result"] = result
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # notification：冇 id，唔使回
    if msg_id is None:
        return

    if method == "initialize":
        _reply(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fatwhale-google-maps", "version": "1.0.0"},
        })
    elif method == "tools/list":
        _reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = _HANDLERS.get(name)
        if handler is None:
            _reply(msg_id, error={"code": -32601, "message": f"unknown tool: {name}"})
            return
        try:
            result = handler(args)
            _reply(msg_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False,
            })
        except Exception as exc:  # noqa: BLE001
            _reply(msg_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
    elif method == "ping":
        _reply(msg_id, {})
    else:
        _reply(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as exc:  # noqa: BLE001
            _reply(msg.get("id"), error={"code": -32603, "message": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
