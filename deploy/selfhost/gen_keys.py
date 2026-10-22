"""Generate secrets for the self-hosted stack and print them as a .env file.

    python3 gen_keys.py > .env

ANON_KEY and SERVICE_ROLE_KEY are HS256 JWTs signed with JWT_SECRET, in the
same shape Supabase issues, so supabase-py and PostgREST accept them.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(secret: str, role: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64(json.dumps({
        "iss": "pasal-selfhost", "role": role, "iat": now, "exp": now + 10 * 365 * 24 * 3600,
    }).encode())
    sig = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"


def main() -> None:
    jwt_secret = secrets.token_urlsafe(48)
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}")
    print(f"AUTHENTICATOR_PASSWORD={secrets.token_urlsafe(32)}")
    print(f"JWT_SECRET={jwt_secret}")
    print(f"ANON_KEY={make_jwt(jwt_secret, 'anon')}")
    print(f"SERVICE_ROLE_KEY={make_jwt(jwt_secret, 'service_role')}")


if __name__ == "__main__":
    main()
