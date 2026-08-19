import json

from cryptography.fernet import Fernet

from app.config import settings

_fernet = Fernet(settings.SESSION_ENCRYPTION_KEY)


def encrypt_json(obj: dict) -> bytes:
    return _fernet.encrypt(json.dumps(obj).encode("utf-8"))


def decrypt_json(data: bytes) -> dict:
    return json.loads(_fernet.decrypt(data).decode("utf-8"))
