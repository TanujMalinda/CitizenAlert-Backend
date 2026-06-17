"""
Auth routes — /api/auth
"""
import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from schemas.schemas import RegisterRequest, AuthorityRegisterRequest, LoginRequest, TokenResponse
from core.security import create_token, get_current_user
from core.errors import http_error
from db import database as db

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
    # Citizens only — authority registration goes through /register-authority
    if body.role in ("authority", "super_admin"):
        http_error(
            403,
            "Authority accounts cannot be self-registered.",
            "Use the CitizenAlert Dashboard authority registration form.",
        )

    # Check if email already exists
    existing = await db.fetchrow(
        "SELECT id FROM users WHERE email = $1", body.email
    )
    if existing:
        http_error(
            400,
            "An account with this email address already exists.",
            "Try logging in instead, or use a different email to register.",
        )

    # Hash password
    hashed = bcrypt.hashpw(
        body.password.encode(), bcrypt.gensalt()
    ).decode()

    # Insert — id is SERIAL so DB assigns it
    row = await db.fetchrow(
        """INSERT INTO users
             (email, password_hash, full_name, phone_number, district, role, account_status)
           VALUES ($1, $2, $3, $4, $5, 'citizen', 'active')
           RETURNING id, full_name, email, role, district""",
        body.email, hashed, body.full_name,
        body.phone_number, body.district,
    )

    if not row:
        http_error(500, "Registration failed due to a database error.", "Please try again later.")

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
        """SELECT id, full_name, email, password_hash, role, district, account_status
           FROM users WHERE email = $1""",
        body.email,
    )

    if not row:
        http_error(401, "Invalid email or password.", "Check your credentials and try again.")

    user = dict(row)

    # Block pending / rejected accounts before checking password
    if user.get("account_status") == "pending_approval":
        http_error(
            403,
            "Your authority account is pending admin approval.",
            "You will be notified once a super-admin reviews your registration.",
        )
    if user.get("account_status") == "rejected":
        http_error(
            403,
            "Your authority registration was rejected.",
            "Contact the CitizenAlert administrator for more information.",
        )

    # Verify password
    if not bcrypt.checkpw(
        body.password.encode(),
        user["password_hash"].encode()
    ):
        http_error(401, "Invalid email or password.", "Check your credentials and try again.")

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


@router.post(
    "/register-authority",
    summary="Register as an authority (pending admin approval)",
    description="""
Submitted via the CitizenAlert Authority Dashboard.
Account is created with status **pending_approval** — the user cannot log in until
a super-admin approves the registration at POST /api/authority/registrations/{id}/approve.
    """,
)
async def register_authority(body: AuthorityRegisterRequest):
    existing = await db.fetchrow("SELECT id FROM users WHERE email = $1", body.email)
    if existing:
        http_error(
            400,
            "An account with this email address already exists.",
            "Try logging in instead, or use a different email to register.",
        )

    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()

    row = await db.fetchrow(
        """INSERT INTO users
             (email, password_hash, full_name, phone_number, district,
              role, account_status, designation, department, employee_id)
           VALUES ($1, $2, $3, $4, $5,
                   'authority', 'pending_approval', $6, $7, $8)
           RETURNING id, full_name, email, role, account_status, designation, department""",
        body.email, hashed, body.full_name, body.phone_number, body.district,
        body.designation, body.department, body.employee_id,
    )

    if not row:
        http_error(500, "Registration failed due to a database error.", "Please try again later.")

    return {
        "success": True,
        "message": "Authority registration submitted. A super-admin will review and approve your account.",
        "user": {
            "id":           row["id"],
            "full_name":    row["full_name"],
            "email":        row["email"],
            "role":         row["role"],
            "status":       row["account_status"],
            "designation":  row["designation"],
            "department":   row["department"],
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
        http_error(404, "User account not found.", "Your token may reference a deleted account. Please register again.")
    return {"success": True, "user": dict(row)}