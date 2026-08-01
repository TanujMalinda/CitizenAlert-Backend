"""
Routing Service — incident-aware detour routing
================================================
Computes a driving route from A to B that AVOIDS the affected areas of active,
verified traffic/disaster alerts (the CAP polygons/circles this system already
stores). Returns both the normal route and the detour so the app can show the
comparison ("normal 12 min through the flood zone vs detour 17 min around it").

Engines (both free):
  - OpenRouteService  — used when ORS_API_KEY is set in .env (2k req/day free)
  - Valhalla public   — default, no key (valhalla1.openstreetmap.de, best effort)

Only traffic + disaster areas are avoided: health/crime zones are advisory and
often too large to route around sensibly.
"""
import json
import os
import urllib.request

from db import database as db

ORS_KEY = os.getenv("ORS_API_KEY", "").strip()
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
VALHALLA_URL = os.getenv("VALHALLA_URL", "https://valhalla1.openstreetmap.de/route")

MAX_AVOID_POLYGONS = 12


async def fetch_avoid_areas(start: tuple, end: tuple) -> list[dict]:
    """
    Active verified traffic/disaster alerts whose affected area lies near the
    start-end corridor. Circles are polygonized; shapes are simplified so the
    routing engines get small payloads.
    Returns [{id, title, alert_type, ring: [[lng,lat],...]}]
    """
    (slat, slng), (elat, elng) = start, end
    rows = await db.fetch(
        """SELECT a.id, a.title, a.alert_type,
                  ST_AsGeoJSON(
                    ST_SimplifyPreserveTopology(
                      COALESCE(
                        a.affected_geom,
                        ST_Buffer(
                          ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)::geography,
                          a.affected_radius_km * 1000, 4
                        )::geometry
                      ), 0.0004)
                  ) AS gj
           FROM alerts a
           WHERE a.status = 'active'
             AND a.alert_type IN ('traffic', 'disaster')
             AND COALESCE(a.tvm_status, 'verified') IN ('verified', 'passed')
             AND (a.affected_geom IS NOT NULL OR a.affected_radius_km IS NOT NULL)
             AND ST_Intersects(
                   COALESCE(a.affected_geom,
                     ST_Buffer(
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)::geography,
                       a.affected_radius_km * 1000, 4)::geometry),
                   ST_Expand(ST_MakeEnvelope(
                     LEAST($1::float8, $2::float8), LEAST($3::float8, $4::float8),
                     GREATEST($1::float8, $2::float8), GREATEST($3::float8, $4::float8),
                     4326), 0.15))
             -- a zone containing the start or destination cannot be avoided
             -- (the trip begins/ends inside it) — excluding it keeps routing solvable
             AND NOT ST_Intersects(
                   COALESCE(a.affected_geom,
                     ST_Buffer(
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)::geography,
                       a.affected_radius_km * 1000, 4)::geometry),
                   ST_SetSRID(ST_MakePoint($1::float8, $3::float8), 4326))
             AND NOT ST_Intersects(
                   COALESCE(a.affected_geom,
                     ST_Buffer(
                       ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)::geography,
                       a.affected_radius_km * 1000, 4)::geometry),
                   ST_SetSRID(ST_MakePoint($2::float8, $4::float8), 4326))
           LIMIT $5""",
        slng, elng, slat, elat, MAX_AVOID_POLYGONS,
    )

    areas = []
    for r in rows or []:
        try:
            g = json.loads(r["gj"])
            if g["type"] == "Polygon":
                ring = g["coordinates"][0]
            elif g["type"] == "MultiPolygon":
                ring = g["coordinates"][0][0]
            else:
                continue
            areas.append({"id": r["id"], "title": r["title"],
                          "alert_type": r["alert_type"], "ring": ring})
        except Exception:
            continue
    return areas


# ── engines ───────────────────────────────────────────────────────────────────

def _http_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


def _decode_polyline6(shape: str) -> list[list[float]]:
    """Decode Valhalla's precision-6 encoded polyline into [[lat,lng],...]."""
    coords, lat, lng, i = [], 0, 0, 0
    while i < len(shape):
        for which in (0, 1):
            result, shift = 0, 0
            while True:
                b = ord(shape[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lng += delta
        coords.append([lat / 1e6, lng / 1e6])
    return coords


def _route_ors(start, end, rings) -> dict:
    payload = {"coordinates": [[start[1], start[0]], [end[1], end[0]]]}
    if rings:
        payload["options"] = {"avoid_polygons": {
            "type": "MultiPolygon", "coordinates": [[r] for r in rings]}}
    data = _http_json(ORS_URL, payload, {"Authorization": ORS_KEY})
    feat = data["features"][0]
    summ = feat["properties"]["summary"]
    return {
        "coordinates": [[c[1], c[0]] for c in feat["geometry"]["coordinates"]],
        "distance_km": round(summ["distance"] / 1000, 2),
        "duration_min": round(summ["duration"] / 60, 1),
    }


def _route_valhalla(start, end, rings) -> dict:
    payload = {
        "locations": [{"lat": start[0], "lon": start[1]},
                      {"lat": end[0], "lon": end[1]}],
        "costing": "auto",
    }
    if rings:
        payload["exclude_polygons"] = rings
    data = _http_json(VALHALLA_URL, payload, {})
    trip = data["trip"]
    coords: list[list[float]] = []
    for leg in trip["legs"]:
        coords.extend(_decode_polyline6(leg["shape"]))
    return {
        "coordinates": coords,
        "distance_km": round(trip["summary"]["length"], 2),
        "duration_min": round(trip["summary"]["time"] / 60, 1),
    }


def compute_route(start, end, rings) -> dict:
    """
    Route with the configured engine. ORS (keyed) is preferred; on any ORS
    failure that is not a routing "no route" (e.g. transient network error)
    we retry once on the public Valhalla server before giving up.
    """
    if ORS_KEY:
        try:
            return _route_ors(start, end, rings)
        except urllib.error.HTTPError:
            raise   # genuine routing answer (e.g. 404 no-route) — don't mask it
        except Exception:
            pass    # network hiccup → fall through to Valhalla
    return _route_valhalla(start, end, rings)


def engine_name() -> str:
    return "openrouteservice" if ORS_KEY else "valhalla-public"
