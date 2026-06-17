from pydantic import BaseModel
from typing import Optional

# ---------------------------
# Auth Schemas
# ---------------------------
class RegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str
    phone_number: Optional[str] = None
    district: str = "Colombo"
    role: str = "citizen"


class AuthorityRegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str
    phone_number: Optional[str] = None
    district: str = "Colombo"
    designation: str            # e.g. "Police Officer", "Health Inspector"
    department: str             # e.g. "Sri Lanka Police", "Ministry of Health"
    employee_id: str            # official employee / badge ID

class LoginRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    token: str
    user: dict

# ---------------------------
# Missing Person Schema
# ---------------------------
class CreateMissingPersonRequest(BaseModel):
    person_name: str
    description: str
    last_seen_lat: float
    last_seen_lng: float
    last_seen_location_desc: Optional[str] = None
    district: Optional[str] = "Colombo"
    age: Optional[int] = None
    gender: Optional[str] = None
    height_cm: Optional[int] = None
    weight_kg: Optional[int] = None
    complexion: Optional[str] = None
    hair_color: Optional[str] = None
    distinguishing_marks: Optional[str] = None
    last_seen_at: Optional[str] = None
    last_seen_wearing: Optional[str] = None
    photo_url: Optional[str] = None
    reporter_relation: Optional[str] = None
    search_radius_km: Optional[float] = 10.0

# ---------------------------
# Sighting Schema
# ---------------------------
class SightingRequest(BaseModel):
    latitude: float
    longitude: float
    description: str
    sighting_time: str
    location_desc: Optional[str] = None