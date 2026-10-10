from django.db import models

from .crypto import decrypt_json, encrypt_json


class EncryptedJSONField(models.TextField):
    """A dict stored Fernet-encrypted. Not queryable, by design."""

    def from_db_value(self, value, expression, connection):
        return decrypt_json(value)

    def to_python(self, value):
        if isinstance(value, dict):
            return value
        return decrypt_json(value)

    def get_prep_value(self, value):
        return encrypt_json(value)
