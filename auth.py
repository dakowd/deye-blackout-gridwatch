"""
Deye Cloud API authentication.

Deye's cloud login works like this:
1. Hash your password with SHA-256 (hex string, lowercase) — Deye never wants
   your raw password over the wire.
2. POST that hash + your appId/appSecret/email to /account/token
3. Deye gives back an access_token (and usually a refresh_token) you attach
   to every subsequent request as a Bearer token.

Tokens expire, so this module caches the token to disk and only re-requests
one when it's missing/expired.
"""

import hashlib
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("DEYE_REGION", "eu").lower()
BASE_URL = f"https://{'eu1' if REGION == 'eu' else 'us1'}-developer.deyecloud.com/v1.0"

APP_ID = os.getenv("DEYE_APP_ID")
APP_SECRET = os.getenv("DEYE_APP_SECRET")
EMAIL = os.getenv("DEYE_EMAIL")
PASSWORD = os.getenv("DEYE_PASSWORD")
COMPANY_ID = os.getenv("DEYE_COMPANY_ID", "0")

TOKEN_CACHE_FILE = Path(__file__).parent / ".token_cache.json"


def _hash_password(plain_password: str) -> str:
    return hashlib.sha256(plain_password.encode("utf-8")).hexdigest()


def _request_new_token() -> dict:
    if not all([APP_ID, APP_SECRET, EMAIL, PASSWORD]):
        raise RuntimeError(
            "Missing credentials. Copy .env.example to .env and fill in "
            "DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD."
        )

    url = f"{BASE_URL}/account/token"
    params = {"appId": APP_ID}
    headers = {"Content-Type": "application/json"}
    body = {
        "appSecret": APP_SECRET,
        "email": EMAIL,
        "companyId": COMPANY_ID,
        "password": _hash_password(PASSWORD),
    }

    response = requests.post(url, params=params, headers=headers, json=body, timeout=15)
    response.raise_for_status()
    data = response.json()

    if "accessToken" not in data and "access_token" not in data:
        raise RuntimeError(f"Unexpected token response, check your credentials: {data}")

    # Normalize key name — Deye sample code has used both stylings historically
    token = data.get("accessToken") or data.get("access_token")
    expires_in = int(data.get("expiresIn") or data.get("expires_in") or 3600)

    cache = {
        "token": token,
        "obtained_at": time.time(),
        "expires_in": expires_in,
        "raw": data,
    }
    TOKEN_CACHE_FILE.write_text(json.dumps(cache))
    return cache


def get_access_token(force_refresh: bool = True) -> str:
    """Returns a valid access token, reusing a cached one if it isn't expired."""
    if not force_refresh and TOKEN_CACHE_FILE.exists():
        cache = json.loads(TOKEN_CACHE_FILE.read_text())
        age = time.time() - cache["obtained_at"]
        expires_in = int(cache["expires_in"])  # API may return this as a string
        # refresh a bit early (60s buffer) rather than cutting it exactly to expiry
        if age < expires_in - 60:
            return cache["token"]

    cache = _request_new_token()
    return cache["token"]


if __name__ == "__main__":
    # Quick manual test: run `python auth.py` to confirm your credentials work
    token = get_access_token(force_refresh=True)
    print("Got token:", token[:20] + "..." if len(token) > 20 else token)
