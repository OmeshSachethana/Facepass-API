import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

API_KEY_PREFIX = "fp_key_"
SESSION_TOKEN_PREFIX = "fp_ses_"  # noqa: S105
WEBHOOK_SECRET_PREFIX = "fp_whs_"  # noqa: S105


def utcnow() -> datetime:
    return datetime.now(UTC)


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def generate_session_token() -> str:
    return SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)


def generate_webhook_secret() -> str:
    return WEBHOOK_SECRET_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hash a token for storage. Tokens hold 256 random bits, so a slow KDF adds nothing."""
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet(secret_key: str) -> Fernet:
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"facepass-webhook-secret"
    ).derive(secret_key.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(value: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt(value.encode()).decode()


def decrypt_secret(value: str, secret_key: str) -> str:
    return _fernet(secret_key).decrypt(value.encode()).decode()


def encode_webhook_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def sign_webhook(secret: str, timestamp: int, body: bytes) -> str:
    """Build the `FacePass-Signature` header. The timestamp lets receivers reject replays."""
    signed = str(timestamp).encode() + b"." + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
