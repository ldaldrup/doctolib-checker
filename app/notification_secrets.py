"""Authenticated notification secrets; the external key never enters SQLite."""
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken


class SecretUnavailable(ValueError):
    pass


class NotificationSecrets:
    def __init__(self, key=""):
        self.cipher = None
        self.key = None
        if key:
            try:
                self.cipher = Fernet(key.encode("ascii"))
                import base64
                self.key = base64.urlsafe_b64decode(key)
            except (ValueError, UnicodeError):
                raise ValueError("NOTIFICATION_SECRET_KEY must be a Fernet key") from None

    @property
    def available(self):
        return self.cipher is not None

    def encrypt(self, value):
        if not self.available:
            raise SecretUnavailable("notification_secret_key_unavailable")
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        if not self.available:
            raise SecretUnavailable("notification_secret_key_unavailable")
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except (InvalidToken, ValueError, UnicodeError, AttributeError):
            raise SecretUnavailable("notification_secret_unreadable") from None

    def identity(self, chat_id):
        if not self.available:
            raise SecretUnavailable("notification_secret_key_unavailable")
        return hmac.new(self.key, chat_id.encode(), hashlib.sha256).hexdigest()
