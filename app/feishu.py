from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings


@dataclass(frozen=True)
class FeishuUser:
    open_id: str
    name: str
    avatar_url: str


class FeishuAuthError(Exception):
    """The identity provider could not authenticate the user."""


class FeishuAuth(Protocol):
    def exchange_code(self, code: str) -> FeishuUser: ...

    def search_users(self, query: str) -> list[FeishuUser]: ...


class HttpFeishuAuth:
    # Official OAuth documentation:
    # https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token
    TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
    USER_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def exchange_code(self, code: str) -> FeishuUser:
        # Connections are opened only on callback, never on import or startup.
        try:
            with httpx.Client(timeout=10, transport=self.transport, follow_redirects=False) as client:
                response = client.post(self.TOKEN_URL, json={
                    "grant_type": "authorization_code",
                    "client_id": self.settings.feishu_app_id,
                    "client_secret": self.settings.feishu_app_secret,
                    "code": code,
                    "redirect_uri": self.settings.feishu_redirect_uri,
                })
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("code", 0) != 0:
                    raise FeishuAuthError("Authorization failed")
                token = payload.get("access_token")
                if not isinstance(token, str) or not token:
                    raise FeishuAuthError("Authorization failed")

                response = client.get(self.USER_URL, headers={"Authorization": f"Bearer {token}"})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("code") != 0:
                    raise FeishuAuthError("Authorization failed")
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise FeishuAuthError("Authorization failed")
                open_id = data.get("open_id")
                name = data.get("name") or ""
                avatar_url = data.get("avatar_url") or ""
                if not isinstance(open_id, str) or not open_id.strip():
                    raise FeishuAuthError("Authorization failed")
                if not isinstance(name, str) or not isinstance(avatar_url, str):
                    raise FeishuAuthError("Authorization failed")
                return FeishuUser(open_id, name, avatar_url)
        except (httpx.HTTPError, ValueError):
            # Do not surface provider bodies, authorization codes or credentials.
            raise FeishuAuthError("Authorization failed") from None

    def search_users(self, query: str) -> list[FeishuUser]:
        raise NotImplementedError("Contact search belongs to a later work order")
