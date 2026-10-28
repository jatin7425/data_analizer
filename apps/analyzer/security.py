from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _credential_cipher():
    key = settings.ANALYZER_CREDENTIAL_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured(
            "Set ANALYZER_CREDENTIAL_ENCRYPTION_KEY before submitting "
            "database credentials."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured(
            "ANALYZER_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key."
        ) from exc


def encrypt_database_password(password):
    return _credential_cipher().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_database_password(encrypted_password):
    return _credential_cipher().decrypt(encrypted_password.encode("ascii")).decode(
        "utf-8"
    )
