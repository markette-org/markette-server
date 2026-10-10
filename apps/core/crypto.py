"""Symmetric encryption for third-party credentials stored in the database."""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = settings.CREDENTIALS_KEY
    if not key:
        # Derive a stable key from SECRET_KEY so development works out of the box.
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_json(value):
    return _fernet().encrypt(json.dumps(value or {}).encode()).decode()


def decrypt_json(token):
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as exc:
        raise ValueError("Credentials cannot be decrypted; was CREDENTIALS_KEY changed?") from exc
