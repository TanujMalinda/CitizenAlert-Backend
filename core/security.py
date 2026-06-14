from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

SECRET_KEY = "secret_key_change_me"
ALGORITHM = "HS256"

security = HTTPBearer()

# ---------------------------
# Create JWT token
# ---------------------------
def create_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=24)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# ---------------------------
# Fake token decoder (simple version for now)
# ---------------------------
def decode_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ---------------------------
# Get current user
# ---------------------------
def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = decode_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    return payload


# ---------------------------
# Authority check
# ---------------------------
def require_authority(user=Depends(get_current_user)):
    if user.get("role") != "authority":
        raise HTTPException(status_code=403, detail="Not authorized")
    return user