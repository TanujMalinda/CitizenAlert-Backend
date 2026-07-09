"""
Geo helpers — affected-area defaults.

Every alert carries an affected geometry: a drawn polygon (affected_geom) when
the shape matters, otherwise a severity-based default circle radius
(affected_radius_km). Follows the CAP-1.2 area model (polygon | circle).
"""

_DEFAULT_RADIUS_KM = {
    "disaster": {"extreme": 5.0, "severe": 3.0, "medium": 1.5, "low": 0.5},
    "health":   {"extreme": 10.0, "severe": 5.0, "medium": 3.0, "low": 1.0},
    "traffic":  {"extreme": 1.0, "severe": 0.7, "medium": 0.4, "low": 0.4},
    "crime":    {"extreme": 0.4, "severe": 0.4, "medium": 0.4, "low": 0.4},
    # missing_person: no area — a person is not a zone
}


def default_affected_radius(alert_type: str, severity: str) -> float | None:
    """Severity-based default circle radius in km (None = marker only)."""
    by_sev = _DEFAULT_RADIUS_KM.get(alert_type)
    if not by_sev:
        return None
    return by_sev.get(severity, by_sev["medium"])
