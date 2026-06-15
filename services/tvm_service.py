"""
Tiered Verification Mechanism (TVM)
=====================================
CORE RESEARCH CONTRIBUTION — CitizenAlert

Implements the 3-tier verification pipeline described in:
  - Research Proposal Section 4.4 & 4.5
  - Research Question 2

Tier 1 — Initial Filter     : geo validation, duplicate check, content check
Tier 2 — Confidence Scoring : weighted multi-factor score (0.0 → 1.0)
Tier 3 — Authority Review   : human review for borderline cases

VIVA NOTE: The score weights below are grounded in literature:
  - reporter_trust   (0.30): Abid et al. 2023 — reporter credibility
  - location_plaus.  (0.25): Nielsen et al. 2020 — spatial plausibility
  - time_plaus.      (0.20): derived from human mobility research
  - corroboration    (0.25): crowd consensus principle from Waze studies
  - cctv_boost       (+0.15): NOVEL CONTRIBUTION of this research
"""

import math
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from db import database as db

# ── Thresholds ────────────────────────────────────────────────────────────────
TVM_AUTO_VERIFY_THRESHOLD    = 0.80
TVM_AUTHORITY_REVIEW_THRESHOLD = 0.50
TVM_CCTV_BOOST               = 0.15
CCTV_TIME_WINDOW_MINUTES     = 30
CCTV_PROXIMITY_METERS        = 200

# Sri Lanka bounding box (WGS84)
SL_BOUNDS = {"min_lat": 5.916, "max_lat": 9.836, "min_lng": 79.695, "max_lng": 81.879}

# ── Score weights (must sum to 1.0) ───────────────────────────────────────────
WEIGHTS = {
    "reporter_trust":        0.30,
    "location_plausibility": 0.25,
    "time_plausibility":     0.20,
    "report_corroboration":  0.25,
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Tier1Result:
    passed: bool
    reason: str
    failures: list[str] = field(default_factory=list)


@dataclass
class ScoreComponents:
    reporter_trust: float = 0.0
    location_plausibility: float = 0.0
    time_plausibility: float = 0.0
    report_corroboration: float = 0.0
    cctv_boost: float = 0.0
    cctv_signal: str | None = None


@dataclass
class Tier2Result:
    score: float
    components: ScoreComponents
    action: str          # "auto_verified" | "authority_review" | "auto_rejected"
    cctv_corroborated: bool = False


@dataclass
class TVMResult:
    tier: int
    status: str
    score: float
    components: ScoreComponents | None = None
    message: str = ""


# ── Tier 1: Initial Filter ────────────────────────────────────────────────────

async def run_tier1(report: dict) -> Tier1Result:
    """
    Gate 1 — automated hard filters.
    Rejects obviously invalid reports before any scoring happens.
    """
    failures = []

    lat = float(report.get("latitude", 0))
    lng = float(report.get("longitude", 0))

    # 1a. Geo-spatial validation — must be inside Sri Lanka
    if not (SL_BOUNDS["min_lat"] <= lat <= SL_BOUNDS["max_lat"] and
            SL_BOUNDS["min_lng"] <= lng <= SL_BOUNDS["max_lng"]):
        failures.append("Location outside Sri Lanka boundaries")

    # 1b. Description length
    desc = report.get("description", "").strip()
    if len(desc) < 10:
        failures.append("Description too short (min 10 characters)")

    # 1c. Duplicate — same user, same alert, within 30 min
    # FIX: table is 'sightings' not 'sighting_reports'
    user_id  = report.get("reported_by")
    alert_id = report.get("alert_id")
    if user_id and alert_id:
        dup = await db.fetchval(
            """SELECT id FROM sightings
               WHERE reporter_id = $1
                 AND alert_id = $2
                 AND created_at > NOW() - INTERVAL '30 minutes'
               LIMIT 1""",
            user_id, alert_id
        )
        if dup:
            failures.append("Duplicate: same user reported this alert within 30 minutes")

    # 1d. Sighting time must not be in the future
    sighting_time = report.get("sighting_time")
    if sighting_time:
        if isinstance(sighting_time, str):
            sighting_time = datetime.fromisoformat(sighting_time)
        if sighting_time.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
            failures.append("Sighting time cannot be in the future")

    passed = len(failures) == 0
    return Tier1Result(
        passed=passed,
        reason="All Tier 1 checks passed" if passed else "; ".join(failures),
        failures=failures,
    )


# ── Tier 2: Confidence Scoring ────────────────────────────────────────────────

async def run_tier2(report: dict, alert: dict, reporter: dict) -> Tier2Result:
    """
    Multi-factor confidence score.
    Returns 0.000 – 1.000 and routing action.
    """
    c = ScoreComponents()

    lat    = float(report["latitude"])
    lng    = float(report["longitude"])
    s_time = report.get("sighting_time")

    # A. Reporter trust score (default neutral 0.50 if not set)
    c.reporter_trust = float(reporter.get("trust_score", 0.50))

    # B. Location plausibility vs last known location
    last_lat  = alert.get("last_seen_lat")
    last_lng  = alert.get("last_seen_lng")
    last_time = alert.get("last_seen_at")
    c.location_plausibility = _location_plausibility(lat, lng, last_lat, last_lng, s_time, last_time)

    # C. Time plausibility
    c.time_plausibility = _time_plausibility(s_time, last_time)

    # D. Corroboration from nearby sightings
    # FIX: uses 'sightings' table with correct column names
    c.report_corroboration = await _corroboration(alert.get("id"), lat, lng)

    # Weighted base score
    score = (
        c.reporter_trust        * WEIGHTS["reporter_trust"] +
        c.location_plausibility * WEIGHTS["location_plausibility"] +
        c.time_plausibility     * WEIGHTS["time_plausibility"] +
        c.report_corroboration  * WEIGHTS["report_corroboration"]
    )

    # CCTV boost — novel contribution (simulated for prototype)
    cctv     = await _check_cctv(lat, lng, s_time)
    cctv_hit = cctv is not None
    if cctv_hit:
        score        = min(1.0, score + TVM_CCTV_BOOST)
        c.cctv_boost  = TVM_CCTV_BOOST
        c.cctv_signal = cctv.get("signal_type")

    score = round(score, 3)

    # Route based on thresholds
    if score >= TVM_AUTO_VERIFY_THRESHOLD:
        action = "auto_verified"
    elif score >= TVM_AUTHORITY_REVIEW_THRESHOLD:
        action = "authority_review"
    else:
        action = "auto_rejected"

    return Tier2Result(score=score, components=c, action=action, cctv_corroborated=cctv_hit)


# ── Full TVM Pipeline ─────────────────────────────────────────────────────────

async def process_tvm(report: dict, alert: dict, reporter: dict) -> TVMResult:
    """
    Runs the complete TVM pipeline: Tier 1 → Tier 2 → (Tier 3 if needed).
    Logs every decision to tvm_log for Section 4.6 evaluation metrics.
    """
    alert_id = alert.get("id")

    # TIER 1
    t1 = await run_tier1(report)
    await _log_tvm(
        alert_id=alert_id,
        tier=1,
        action="filter_pass" if t1.passed else "filter_fail",
        notes=t1.reason,
    )

    if not t1.passed:
        return TVMResult(tier=1, status="rejected", score=0.0, message=t1.reason)

    # TIER 2
    t2 = await run_tier2(report, alert, reporter)
    await _log_tvm(
        alert_id=alert_id,
        tier=2,
        action="score_calculated",
        notes=f"score={t2.score} action={t2.action} components={json.dumps(t2.components.__dict__)}",
    )

    if t2.action == "auto_rejected":
        return TVMResult(tier=2, status="rejected", score=t2.score,
                         components=t2.components,
                         message="Score too low — report rejected automatically")

    if t2.action == "auto_verified":
        return TVMResult(tier=2, status="verified", score=t2.score,
                         components=t2.components,
                         message="Report verified and broadcast to authorities")

    # TIER 3 — escalate for human review
    await _log_tvm(
        alert_id=alert_id,
        tier=3,
        action="escalated_to_authority",
        notes=f"score={t2.score} — borderline, requires human review",
    )
    return TVMResult(
        tier=3,
        status="pending_authority_review",
        score=t2.score,
        components=t2.components,
        message="Report escalated to authority dashboard for manual review",
    )


# ── TVM for new alert submissions (crime / health / traffic) ─────────────────

async def process_tvm_for_alert(
    latitude: float,
    longitude: float,
    description: str,
    user_id: int | None,
    alert_id: int | None = None,
) -> TVMResult:
    """
    Runs Tier 1 + Tier 2 for a freshly submitted alert (not a sighting).
    Tier 3 routing is left to the caller — crime and health always escalate
    regardless of score; traffic uses consensus instead.
    The computed score is stored so authorities can prioritise their queue.
    """
    reporter_row = await db.fetchrow(
        "SELECT trust_score FROM users WHERE id = $1", user_id
    ) if user_id else None
    reporter = {
        "trust_score": float(reporter_row["trust_score"])
        if reporter_row and reporter_row.get("trust_score") else 0.50
    }

    report = {
        "latitude": latitude,
        "longitude": longitude,
        "description": description,
        "reported_by": user_id,
        # No alert_id / sighting_time — new submission, not a sighting
    }

    # Tier 1 — geo + content filter
    t1 = await run_tier1(report)
    await _log_tvm(
        alert_id=alert_id, tier=1,
        action="filter_pass" if t1.passed else "filter_fail",
        notes=t1.reason,
    )
    if not t1.passed:
        return TVMResult(tier=1, status="rejected", score=0.0, message=t1.reason)

    # Tier 2 — confidence scoring
    # alert={} → location_plausibility defaults to 0.70, corroboration to 0.50
    t2 = await run_tier2(report, {}, reporter)
    await _log_tvm(
        alert_id=alert_id, tier=2, action="score_calculated",
        notes=f"score={t2.score} cctv_boost={t2.components.cctv_boost}",
    )

    return TVMResult(
        tier=2,
        status="scored",
        score=t2.score,
        components=t2.components,
        message=f"TVM score: {t2.score}",
    )


# ── Scoring helper functions ──────────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two GPS coordinates."""
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(d_lng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _location_plausibility(lat, lng, last_lat, last_lng, s_time, last_time) -> float:
    if last_lat is None or last_lng is None:
        return 0.70  # no baseline — neutral

    dist_km = _haversine_km(lat, lng, float(last_lat), float(last_lng))

    hours = 1.0
    if s_time and last_time:
        if isinstance(s_time, str):    s_time    = datetime.fromisoformat(s_time)
        if isinstance(last_time, str): last_time = datetime.fromisoformat(last_time)
        hours = max(0.01, abs((s_time - last_time).total_seconds()) / 3600)

    max_plausible_km = hours * 80  # ~80 km/h generous max travel speed

    if dist_km > max_plausible_km: return 0.10
    if dist_km <= 1:               return 0.95
    if dist_km <= 5:               return 0.80
    if dist_km <= 20:              return 0.60
    return 0.30


def _time_plausibility(sighting_time, last_seen_at) -> float:
    if not last_seen_at:
        return 0.70
    if isinstance(sighting_time, str): sighting_time = datetime.fromisoformat(sighting_time)
    if isinstance(last_seen_at, str):  last_seen_at  = datetime.fromisoformat(last_seen_at)
    hours = abs((sighting_time - last_seen_at).total_seconds()) / 3600
    if hours <= 1:  return 1.00
    if hours <= 6:  return 0.90
    if hours <= 24: return 0.70
    if hours <= 72: return 0.50
    return 0.30


async def _corroboration(alert_id, lat: float, lng: float) -> float:
    """
    Count credible sightings within 500m of this report in last 24h.
    FIX: uses 'sightings' table with correct column names.
    """
    if not alert_id:
        return 0.50
    count = await db.fetchval(
        """SELECT COUNT(*) FROM sightings
           WHERE alert_id = $1
             AND tvm_tier >= 2
             AND created_at > NOW() - INTERVAL '24 hours'
             AND ST_DWithin(
                   ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography,
                   ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
                   500)""",
        alert_id, lng, lat
    )
    n = int(count or 0)
    return {0: 0.50, 1: 0.65, 2: 0.75}.get(n, 0.90)


async def _check_cctv(lat: float, lng: float, s_time) -> dict | None:
    """
    Simulated CCTV metadata check — privacy-preserving (no video accessed).
    Returns a mock signal for prototype demonstration.
    VIVA NOTE: In production this queries real CCTV metadata API.
               For evaluation, returns simulated signals based on known
               high-camera-density areas in Colombo.
    """
    # Colombo high-density CCTV zones (simulated)
    colombo_zones = [
        {"lat": 6.9271, "lng": 79.8612, "name": "Colombo Fort"},
        {"lat": 6.9147, "lng": 79.8772, "name": "Pettah"},
        {"lat": 6.9022, "lng": 79.8607, "name": "Majestic City"},
    ]
    for zone in colombo_zones:
        dist = _haversine_km(lat, lng, zone["lat"], zone["lng"])
        if dist <= 0.5:  # within 500m of a known CCTV zone
            return {"signal_type": "motion_detected", "confidence": 0.75,
                    "zone": zone["name"]}
    return None


async def _log_tvm(alert_id, tier: int, action: str, notes: str = "", actor_id=None):
    """
    Log TVM decision to tvm_log table.
    FIX: uses 'tvm_log' not 'tvm_audit_log', matches real schema.
    """
    await db.execute(
        """INSERT INTO tvm_log (alert_id, tier, action, actor_id, notes)
           VALUES ($1, $2, $3, $4, $5)""",
        alert_id, tier, action, actor_id, notes
    )