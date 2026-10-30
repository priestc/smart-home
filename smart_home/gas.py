"""Daily gas price watcher.

Fetches gas station prices inside a user-drawn circle (the "gas area") from
the Google Places API (New) Text Search endpoint, which exposes per-station
fuel prices through its ``fuelOptions`` field. The same approach is used by
the RoadtripApp project's /api/places/gas-search route.

Text Search returns at most 60 results (3 pages of 20) per query, so the
circle's bounding box is searched as a quadtree: any tile that comes back
saturated is split into four and searched again, until every tile is under
the cap or the request budget is spent.

Spending is capped per calendar month (see MONTHLY_REQUEST_CAP): each run may
use at most an even share of what's left of the month's budget, so a dense
area degrades to partial coverage rather than running up the bill.
"""
from __future__ import annotations
import datetime
import json
import math
import sqlite3
import threading

import httpx

from smart_home import delegated

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,places.displayName,places.location,places.fuelOptions,"
    "places.formattedAddress,places.addressComponents,nextPageToken"
)
PAGE_SIZE = 20
MAX_PAGES = 3                       # Text Search hard limit: 60 results per query
MAX_DEPTH = 4                       # quadtree depth; 4 => tiles 1/16 the width of the circle
MAX_REQUESTS_PER_RUN = 80

# Cost guard. fuelOptions bills every request (each page counts) as
# "Text Search Enterprise + Atmosphere": $40 / 1000 after 1000 free per month.
# Note the free allowance is per billing account, so other apps on the same
# account (e.g. RoadtripApp) eat into it too.
GOOGLE_FREE_REQUESTS_PER_MONTH = 1000
PRICE_PER_1000_USD = 40.0
MONTHLY_COST_CAP_USD = 10.0
MONTHLY_REQUEST_CAP = GOOGLE_FREE_REQUESTS_PER_MONTH + int(MONTHLY_COST_CAP_USD / PRICE_PER_1000_USD * 1000)
DAILY_FETCH_HOUR = 7                # first daily fetch happens at/after this local hour
MAX_FAILED_RUNS_PER_DAY = 3

AREA_SETTINGS_KEY = "gas_area"
# Separate key for server-side Places calls: the Maps key used by the browser
# is typically locked to HTTP referrers, which server requests don't send.
SERVER_KEY_SETTINGS_KEY = "google_places_server_key"
FUEL_TYPES = ("REGULAR_UNLEADED", "MIDGRADE", "PREMIUM", "DIESEL")

_fetch_lock = threading.Lock()


class GasFetchError(Exception):
    pass


class _BudgetExhausted(Exception):
    pass


# ── Settings ────────────────────────────────────────────────────────────────

def get_api_key(conn: sqlite3.Connection) -> str | None:
    """Browser Maps JavaScript API key."""
    row = conn.execute("SELECT value FROM settings WHERE key='google_maps_api_key'").fetchone()
    return row[0] if row and row[0] else None


def get_server_api_key(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SERVER_KEY_SETTINGS_KEY,)).fetchone()
    return row[0] if row and row[0] else None


def set_server_api_key(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (SERVER_KEY_SETTINGS_KEY, key or None),
    )
    conn.commit()


def get_area(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (AREA_SETTINGS_KEY,)).fetchone()
    if not row or not row[0]:
        return None
    return json.loads(row[0])


def set_area(conn: sqlite3.Connection, lat: float, lon: float, radius_m: float) -> dict:
    area = {"lat": float(lat), "lon": float(lon), "radius_m": float(radius_m)}
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (AREA_SETTINGS_KEY, json.dumps(area)),
    )
    conn.commit()
    return area


# ── Geometry ────────────────────────────────────────────────────────────────

_EARTH_R = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def _bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = math.degrees(radius_m / _EARTH_R)
    dlon = math.degrees(radius_m / (_EARTH_R * math.cos(math.radians(lat))))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def _tile_intersects_circle(tile, lat: float, lon: float, radius_m: float) -> bool:
    s, w, n, e = tile
    nearest_lat = min(max(lat, s), n)
    nearest_lon = min(max(lon, w), e)
    return haversine_m(lat, lon, nearest_lat, nearest_lon) <= radius_m


def _split(tile):
    s, w, n, e = tile
    mlat, mlon = (s + n) / 2, (w + e) / 2
    return [(s, w, mlat, mlon), (s, mlon, mlat, e), (mlat, w, n, mlon), (mlat, mlon, n, e)]


# ── Places API ──────────────────────────────────────────────────────────────

def _money(m: dict) -> float:
    return float(m.get("units", 0) or 0) + (m.get("nanos", 0) or 0) / 1e9


def _extract_city(components: list[dict] | None) -> str | None:
    if not components:
        return None
    city = None
    for t in ("locality", "administrative_area_level_3", "administrative_area_level_2"):
        c = next((c for c in components if t in (c.get("types") or [])), None)
        if c and c.get("longText"):
            city = c["longText"]
            break
    if not city:
        return None
    state = next((c for c in components if "administrative_area_level_1" in (c.get("types") or [])), None)
    return f"{city}, {state['shortText']}" if state and state.get("shortText") else city


def _search_tile(client: httpx.Client, api_key: str, tile, budget: list[int]) -> tuple[list[dict], bool]:
    """budget is [remaining, spent], updated in place."""
    """Search one rectangle. Returns (places, saturated)."""
    s, w, n, e = tile
    places: list[dict] = []
    page_token = None
    for _ in range(MAX_PAGES):
        if budget[0] <= 0:
            raise _BudgetExhausted()
        body = {
            "textQuery": "gas station",
            "includedType": "gas_station",
            "strictTypeFiltering": True,
            "pageSize": PAGE_SIZE,
            "locationRestriction": {"rectangle": {
                "low": {"latitude": s, "longitude": w},
                "high": {"latitude": n, "longitude": e},
            }},
        }
        if page_token:
            body["pageToken"] = page_token
        budget[0] -= 1
        budget[1] += 1
        resp = delegated.request(
            "POST", SEARCH_URL, client=client,
            json=body,
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK},
            timeout=20,
        )
        if resp.status_code != 200:
            try:
                msg = resp.json().get("error", {}).get("message") or resp.text
            except ValueError:
                msg = resp.text
            if "referer" in msg.lower():
                msg += (" — this key is restricted to websites; set a server API key"
                        " (IP-restricted or API-restricted to Places API (New)) on the Gas page")
            raise GasFetchError(f"Places API HTTP {resp.status_code}: {msg[:400]}")
        data = resp.json()
        places.extend(data.get("places") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    # Google stops handing out page tokens at the 60-result cap, so a full
    # set of results means there are probably more stations in this tile.
    return places, len(places) >= MAX_PAGES * PAGE_SIZE


def fetch_area_stations(api_key: str, lat: float, lon: float, radius_m: float,
                        budget: list[int]) -> tuple[list[dict], bool]:
    """Return (stations, truncated) for every gas station inside the circle.

    budget is [remaining, spent] and is updated in place, so the caller knows
    how many billed requests were made even if this raises. truncated is True
    when the budget ran out before the whole area was covered.
    """
    found: dict[str, dict] = {}
    queue = [(_bbox(lat, lon, radius_m), 0)]
    truncated = False
    with httpx.Client() as client:
        while queue:
            tile, depth = queue.pop(0)
            try:
                places, saturated = _search_tile(client, api_key, tile, budget)
            except _BudgetExhausted:
                truncated = True
                break
            for p in places:
                found[p["id"]] = p
            if saturated and depth < MAX_DEPTH:
                queue.extend((t, depth + 1) for t in _split(tile)
                             if _tile_intersects_circle(t, lat, lon, radius_m))

    stations = []
    for p in found.values():
        loc = p.get("location") or {}
        plat, plon = loc.get("latitude"), loc.get("longitude")
        if plat is None or plon is None or haversine_m(lat, lon, plat, plon) > radius_m:
            continue
        prices = []
        for fp in (p.get("fuelOptions") or {}).get("fuelPrices") or []:
            if fp.get("type") and fp.get("price"):
                prices.append({
                    "fuel_type": fp["type"],
                    "price": round(_money(fp["price"]), 3),
                    "updated": fp.get("updateTime"),
                })
        stations.append({
            "place_id": p["id"],
            "name": (p.get("displayName") or {}).get("text") or "Unnamed gas station",
            "address": p.get("formattedAddress"),
            "city": _extract_city(p.get("addressComponents")),
            "lat": plat,
            "lon": plon,
            "prices": prices,
        })
    return stations, truncated


# ── Fetch run ───────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def requests_used(conn: sqlite3.Connection, since_date: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(n_requests), 0) FROM gas_fetch_runs WHERE fetch_date >= ?", (since_date,)
    ).fetchone()[0]


def budget_status(conn: sqlite3.Connection, now: datetime.datetime | None = None) -> dict:
    """This month's request usage and how many requests a run may make today.

    Today's allowance is an even split of what was left at the start of today
    across the remaining days of the month (minus anything already used today),
    so early-month manual fetches can't starve the rest of the month.
    """
    now = now or datetime.datetime.now()
    month_start = now.strftime("%Y-%m-01")
    today = now.strftime("%Y-%m-%d")
    used_month = requests_used(conn, month_start)
    used_today = requests_used(conn, today)
    next_month = (now.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    days_left = (next_month.date() - now.date()).days
    left_at_day_start = MONTHLY_REQUEST_CAP - (used_month - used_today)
    today_allowance = max(0, left_at_day_start // days_left - used_today)
    return {
        "month_cap": MONTHLY_REQUEST_CAP,
        "month_used": used_month,
        "cost_cap_usd": MONTHLY_COST_CAP_USD,
        "est_cost_usd": round(max(0, used_month - GOOGLE_FREE_REQUESTS_PER_MONTH) * PRICE_PER_1000_USD / 1000, 2),
        "run_allowance": min(MAX_REQUESTS_PER_RUN, today_allowance),
    }


def is_fetch_running() -> bool:
    return _fetch_lock.locked()


def run_fetch(db_path: str) -> dict:
    """Fetch and store today's prices. Records the outcome in gas_fetch_runs
    and re-raises on failure so callers can surface the error."""
    if not _fetch_lock.acquire(blocking=False):
        raise GasFetchError("A gas price fetch is already running")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        ts = _now()
        fetch_date = ts[:10]
        budget = [0, 0]   # [remaining, spent]
        try:
            api_key = get_server_api_key(conn)
            if not api_key:
                raise GasFetchError("No server API key configured for Places (set one on the Gas page)")
            area = get_area(conn)
            if not area:
                raise GasFetchError("No gas area drawn yet")
            bs = budget_status(conn)
            if bs["run_allowance"] <= 0:
                raise GasFetchError(
                    f"Request cap reached ({bs['month_used']} of {bs['month_cap']} this month, "
                    f"~${bs['cost_cap_usd']:.0f}/mo limit); no requests left for today"
                )
            budget[0] = bs["run_allowance"]
            stations, truncated = fetch_area_stations(api_key, area["lat"], area["lon"], area["radius_m"], budget)
        except Exception as e:
            conn.execute(
                "INSERT INTO gas_fetch_runs (ts, fetch_date, ok, n_requests, error) VALUES (?,?,0,?,?)",
                (ts, fetch_date, budget[1], f"{type(e).__name__}: {e}" if not isinstance(e, GasFetchError) else str(e)),
            )
            conn.commit()
            raise

        n_prices = 0
        for st in stations:
            conn.execute(
                """INSERT INTO gas_stations (place_id, name, address, city, lat, lon, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(place_id) DO UPDATE SET
                     name=excluded.name, address=excluded.address, city=excluded.city,
                     lat=excluded.lat, lon=excluded.lon, last_seen=excluded.last_seen""",
                (st["place_id"], st["name"], st["address"], st["city"], st["lat"], st["lon"], ts, ts),
            )
            for pr in st["prices"]:
                conn.execute(
                    "INSERT OR REPLACE INTO gas_prices (fetch_date, place_id, fuel_type, price, price_updated) "
                    "VALUES (?,?,?,?,?)",
                    (fetch_date, st["place_id"], pr["fuel_type"], pr["price"], pr["updated"]),
                )
                n_prices += 1
        n_requests = budget[1]
        # A truncated run still stores what it found, but records a warning
        # so the UI can say the area wasn't fully covered.
        warning = (f"Only part of the gas area was covered: hit today's request allowance "
                   f"({n_requests} requests) under the ${MONTHLY_COST_CAP_USD:.0f}/month cap. "
                   f"Shrink the circle for full coverage.") if truncated else None
        conn.execute(
            "INSERT INTO gas_fetch_runs (ts, fetch_date, ok, n_requests, n_stations, n_prices, error) "
            "VALUES (?,?,1,?,?,?,?)",
            (ts, fetch_date, n_requests, len(stations), n_prices, warning),
        )
        conn.commit()
        return {"ok": True, "ts": ts, "n_requests": n_requests, "n_stations": len(stations),
                "n_prices": n_prices, "warning": warning}
    finally:
        conn.close()
        _fetch_lock.release()


def daily_fetch_due(conn: sqlite3.Connection, now: datetime.datetime | None = None) -> bool:
    """True if today's scheduled fetch should run now."""
    now = now or datetime.datetime.now()
    if now.hour < DAILY_FETCH_HOUR:
        return False
    if not get_area(conn) or not get_server_api_key(conn):
        return False
    today = now.strftime("%Y-%m-%d")
    ok, failed = conn.execute(
        "SELECT COALESCE(SUM(ok), 0), COALESCE(SUM(1 - ok), 0) FROM gas_fetch_runs WHERE fetch_date=?",
        (today,),
    ).fetchone()
    return ok == 0 and failed < MAX_FAILED_RUNS_PER_DAY and budget_status(conn, now)["run_allowance"] > 0
