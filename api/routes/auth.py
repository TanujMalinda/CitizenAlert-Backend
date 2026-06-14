"""
Auth routes — /api/auth
"""
import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from app.schemas.schemas import RegisterRequest, LoginRequest, TokenResponse
from app.core.security import create_token, get_current_user
from app.db import database as db

router = APIRouter()


@router.get("/health", summary="Auth service health check")
async def auth_health():
    pool = db.get_pool()
    return {
        "status": "ok",
        "service": "auth",
        "database": "connected" if pool else "disconnected",
    }


@router.post(
    "/register",
    response_model=TokenResponse,
    summary="Register a new user",
    description="Creates a citizen or authority account. Returns JWT token.",
)
async def register(body: RegisterRequest):
    # Check if email already exists
    existing = await db.fetchrow(
        "SELECT id FROM users WHERE email = $1", body.email
    )
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Hash password
    hashed = bcrypt.hashpw(
        body.password.encode(), bcrypt.gensalt()
    ).decode()

    # Insert — id is SERIAL so DB assigns it
    row = await db.fetchrow(
        """INSERT INTO users
             (email, password_hash, full_name, phone_number, district, role)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id, full_name, email, role, district""",
        body.email, hashed, body.full_name,
        body.phone_number, body.district, body.role,
    )

    if not row:
        raise HTTPException(status_code=500,
                            detail="Registration failed — database error")

    user = dict(row)
    token = create_token({
        "id":       str(user["id"]),
        "email":    user["email"],
        "role":     user["role"],
        "district": user.get("district", "Colombo"),
    })
    return {"token": token, "user": user}


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login and get JWT token",
)
async def login(body: LoginRequest):
    row = await db.fetchrow(
        """SELECT id, full_name, email, password_hash, role, district
           FROM users WHERE email = $1""",
        body.email,
    )

    if not row:
        raise HTTPException(status_code=401,
                            detail="Invalid email or password")

    user = dict(row)

    # Verify password
    if not bcrypt.checkpw(
        body.password.encode(),
        user["password_hash"].encode()
    ):
        raise HTTPException(status_code=401,
                            detail="Invalid email or password")

    token = create_token({
        "id":       str(user["id"]),
        "email":    user["email"],
        "role":     user["role"],
        "district": user.get("district", "Colombo"),
    })

    return {
        "token": token,
        "user": {
            "id":        user["id"],
            "full_name": user["full_name"],
            "email":     user["email"],
            "role":      user["role"],
            "district":  user["district"],
        },
    }


@router.get("/me", summary="Get current user info")
async def me(user: dict = Depends(get_current_user)):
    # Fetch fresh from DB
    row = await db.fetchrow(
        "SELECT id, full_name, email, role, district FROM users WHERE id = $1",
        int(user["id"]) if str(user["id"]).isdigit() else 0,
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "user": dict(row)}