from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings


@dataclass(frozen=True)
class FeishuUser:
    open_id: str
    name: str
    avatar_url: str
    department: str = ""
    email: str = ""


class FeishuAuthError(Exception):
    """The identity provider could not authenticate the user."""


class FeishuSearchNotConfigured(FeishuAuthError):
    """Contact search requires application credentials."""


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

    # directory/v1 supports tenant_access_token; the legacy search/v1/user
    # endpoint requires a user token. Never retain OAuth tokens for this page.
    # https://open.feishu.cn/document/directory-v1/employee/search
    TENANT_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    SEARCH_URL = "https://open.feishu.cn/open-apis/directory/v1/employees/search"

    def search_users(self, query: str) -> list[FeishuUser]:
        if not self.settings.feishu_app_id.strip() or not self.settings.feishu_app_secret.strip():
            raise FeishuSearchNotConfigured("通讯录搜索未配置：缺少 FEISHU_APP_ID/FEISHU_APP_SECRET。")
        query = query.strip()
        if not query:
            return []

        def payload(response: httpx.Response) -> dict:
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get('code') != 0:
                raise ValueError('Invalid provider response')
            return value

        def text(value) -> str:
            if value is None:
                return ''
            if not isinstance(value, str):
                raise ValueError('Invalid text field')
            return value

        def localized(value) -> str:
            if value is None:
                return ''
            return text(value.get('default_value'))

        try:
            with httpx.Client(timeout=10, transport=self.transport, follow_redirects=False) as client:
                data = payload(client.post(self.TENANT_TOKEN_URL, json={
                    'app_id': self.settings.feishu_app_id,
                    'app_secret': self.settings.feishu_app_secret,
                }))
                token = data.get('tenant_access_token')
                if not isinstance(token, str) or not token.strip():
                    raise ValueError('Missing application token')
                data = payload(client.post(self.SEARCH_URL,
                    headers={'Authorization': f'Bearer {token}'},
                    params={'employee_id_type': 'open_id', 'department_id_type': 'open_department_id'},
                    json={'query': query, 'page_request': {'page_size': 20}, 'required_fields': [
                        'base_info.employee_id', 'base_info.name', 'base_info.avatar',
                        'base_info.departments', 'base_info.email',
                    ]},
                ))
                employees = data['data']['employees']
                if not isinstance(employees, list):
                    raise ValueError('Invalid employee list')
                result = []
                for employee in employees[:20]:
                    base = employee['base_info']
                    open_id = text(base.get('employee_id'))
                    if not open_id.strip():
                        raise ValueError('Missing open_id')
                    result.append(FeishuUser(
                        open_id=open_id,
                        name=localized((base.get('name') or {}).get('name')),
                        avatar_url=text((base.get('avatar') or {}).get('avatar_240')),
                        department='、'.join(localized(d.get('name')) for d in base.get('departments', [])),
                        email=text(base.get('email')),
                    ))
                return result
        except httpx.HTTPError:
            raise FeishuAuthError('通讯录搜索失败：飞书网络请求失败，请稍后重试。') from None
        except (ValueError, KeyError, TypeError, AttributeError):
            # Provider responses may contain private data; expose only safe context.
            raise FeishuAuthError('通讯录搜索失败：请检查飞书应用的通讯录权限与配置，或稍后重试。') from None
