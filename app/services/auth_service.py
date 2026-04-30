from __future__ import annotations

from passlib.context import CryptContext

# pbkdf2_sha256: pure Python, stable across Python/bcrypt wheel versions (avoids bcrypt 4.x + passlib quirks).
_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(plain, password_hash)
    except Exception:
        return False
