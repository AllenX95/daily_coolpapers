import base64
import logging
from pathlib import Path

from cryptography.fernet import Fernet

from .config import INSTANCE_DIR, ensure_directories

logger = logging.getLogger(__name__)


class SecretStore:
    def __init__(self, key_path: Path | None = None) -> None:
        ensure_directories()
        self.key_path = key_path or (INSTANCE_DIR / "fernet.key")

    def encrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip()
        if not value:
            return None
        dpapi = self._dpapi_encrypt(value)
        if dpapi:
            return dpapi
        token = self._fernet().encrypt(value.encode("utf-8")).decode("utf-8")
        return f"fernet:{token}"

    def decrypt(self, encrypted: str | None) -> str | None:
        if not encrypted:
            return None
        if encrypted.startswith("dpapi:"):
            return self._dpapi_decrypt(encrypted)
        if encrypted.startswith("fernet:"):
            token = encrypted.removeprefix("fernet:")
            return self._fernet().decrypt(token.encode("utf-8")).decode("utf-8")
        return encrypted

    def masked(self, encrypted: str | None) -> str:
        value = self.decrypt(encrypted)
        if not value:
            return "未设置"
        if len(value) <= 8:
            return "****"
        return f"{value[:4]}...{value[-4:]}"

    def _fernet(self) -> Fernet:
        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
        return Fernet(self.key_path.read_bytes())

    def _dpapi_encrypt(self, value: str) -> str | None:
        try:
            import win32crypt  # type: ignore

            blob = win32crypt.CryptProtectData(
                value.encode("utf-8"),
                "DailyCoolPapers",
                None,
                None,
                None,
                0,
            )
            return "dpapi:" + base64.b64encode(blob).decode("ascii")
        except Exception as exc:
            logger.debug("DPAPI encryption unavailable, using Fernet: %s", exc)
            return None

    def _dpapi_decrypt(self, encrypted: str) -> str:
        try:
            import win32crypt  # type: ignore

            blob = base64.b64decode(encrypted.removeprefix("dpapi:"))
            return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8")
        except Exception as exc:
            raise ValueError("无法解密 DPAPI API key") from exc


secret_store = SecretStore()
