import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str = field(repr=False)
    feishu_redirect_uri: str
    session_secret: str = field(repr=False)
    db_path: Path
    admin_open_id: str

    def __post_init__(self):
        if len(self.session_secret.encode()) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 bytes")
        uri = urlsplit(self.feishu_redirect_uri)
        if uri.scheme not in {"http", "https"} or not uri.netloc:
            raise ValueError("FEISHU_REDIRECT_URI must be an absolute HTTP(S) URL")

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.feishu_redirect_uri).scheme == "https"

    @classmethod
    def from_env(cls) -> "Settings":
        keys = (
            "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_REDIRECT_URI",
            "SESSION_SECRET", "DB_PATH", "ADMIN_OPEN_ID",
        )
        values = {key: os.environ.get(key, "").strip() for key in keys}
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        return cls(
            feishu_app_id=values["FEISHU_APP_ID"],
            feishu_app_secret=values["FEISHU_APP_SECRET"],
            feishu_redirect_uri=values["FEISHU_REDIRECT_URI"],
            session_secret=values["SESSION_SECRET"],
            db_path=Path(values["DB_PATH"]).expanduser(),
            admin_open_id=values["ADMIN_OPEN_ID"],
        )
