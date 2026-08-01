"""
Routing routes — /api/routing
=============================
POST /detour — incident-aware detour routing: a driving route that avoids the
affected areas of active traffic/disaster alerts, plus the normal route for
comparison.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core.security import get_current_user
from services import routing_service

router = APIRouter()


class DetourRequest(BaseModel):
    start_lat: float
    start_lng: float
    end_lat: float
    end_lng: float


@router.post(
    "/detour",
    summary="Route that avoids active incident areas",
    description="""
Computes two driving routes between start and end:

- **normal** — the ordinary fastest route
- **detour** — the fastest route that avoids the affected areas (CAP polygons /
  circles) of active, verified **traffic and disaster** alerts

`avoided` lists the alerts whose zones were routed around. When no incident
area lies near the corridor the two routes are identical.
""",
)
async def detour(body: DetourRequest, user: dict = Depends(get_current_user)):
    start = (body.start_lat, body.start_lng)
    end = (body.end_lat, body.end_lng)

    areas = await routing_service.fetch_avoid_areas(start, end)
    rings = [a["ring"] for a in areas]

    try:
        normal = await run_in_threadpool(
            routing_service.compute_route, start, end, [])
    except Exception as e:
        print(f"[ROUTING] normal route failed: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=502,
            detail="Routing engine unavailable — please try again shortly.")

    detour_route = None
    detour_note = None
    if rings:
        try:
            detour_route = await run_in_threadpool(
                routing_service.compute_route, start, end, rings)
        except Exception as e:
            # The only roads may pass through the zones — that is an answer,
            # not a server error.
            print(f"[ROUTING] detour unroutable: {type(e).__name__}: {e}")
            detour_note = ("No route exists that avoids all incident zones — "
                           "proceed with caution.")

    return {
        "success": True,
        "engine": routing_service.engine_name(),
        "normal": normal,
        "detour": detour_route,          # null when nothing to avoid / unroutable
        "detour_note": detour_note,
        "avoided": [{"id": a["id"], "title": a["title"],
                     "alert_type": a["alert_type"]} for a in areas],
    }
