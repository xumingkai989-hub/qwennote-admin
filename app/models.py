"""Model configuration: explicit public projection and on-demand secret access."""
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from dotenv import dotenv_values

SECRET_KEY_PATH = Path('/etc/qwennote/secret.key')
CLAUDE_ENV_PATH = Path('/etc/claude/deepseek.qwennote.env')
PUBLIC_COLUMNS = 'id, name, provider, base_url, model_id, key_last4, enabled, is_default, created_at, updated_at'


class ModelError(Exception):
    """Only fixed, credential-free messages cross this boundary."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def cipher():
    try:
        return Fernet(SECRET_KEY_PATH.read_bytes().strip())
    except (OSError, ValueError):
        raise ModelError('模型加密密钥不可用，请检查 secret.key 与读取权限。', 503) from None


def encrypt_key(value):
    return cipher().encrypt(value.encode()).decode('ascii')


def decrypt_key(value):
    try:
        return cipher().decrypt(value.encode()).decode('utf-8')
    except (InvalidToken, ValueError, UnicodeError):
        raise ModelError('模型密钥无法解密，请检查 secret.key 是否匹配。', 503) from None


def normalized_url(value):
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or not parts.port and parts.netloc.endswith(':')):
            raise ValueError
        return value.rstrip('/')
    except ValueError:
        raise ModelError('端点须为完整 HTTP(S) URL，不能包含认证信息、查询参数或片段。') from None


def messages_url(base_url):
    base = base_url.rstrip('/')
    return base + ('/messages' if base.endswith('/v1') else '/v1/messages')


def claude_configuration():
    # No interpolation, execution, or fallback to this process's environment.
    # Only return the two allowlisted fields; never return the parsed mapping.
    try:
        if not CLAUDE_ENV_PATH.is_file():
            return None
        values = dotenv_values(CLAUDE_ENV_PATH, interpolate=False)
        model = values.get('ANTHROPIC_MODEL', '') or ''
        endpoint = values.get('ANTHROPIC_BASE_URL', '') or ''
        parts = urlsplit(endpoint)
        if (not model or not parts.hostname or parts.scheme not in ('http', 'https') or
                parts.username or parts.password or parts.query or parts.fragment or
                any(ord(c) < 32 for c in model) or len(model) > 200 or '${' in model or '${' in endpoint or
                (values.get('ANTHROPIC_AUTH_TOKEN') and any(values['ANTHROPIC_AUTH_TOKEN'] in value
                                                         for value in (model, endpoint)))):
            return None
        return {'model': model, 'endpoint': urlunsplit(('', parts.netloc, parts.path.rstrip('/'), '', '')).lstrip('//')}
    except (OSError, ValueError, UnicodeError):
        return None


@dataclass(frozen=True)
class Model:
    id: int
    name: str
    provider: str
    base_url: str
    model_id: str
    key_last4: str
    enabled: int
    is_default: int
    created_at: str
    updated_at: str

    def public(self):
        return asdict(self)


@dataclass(frozen=True)
class ModelCredentials:
    auth_token: str = field(repr=False)
    base_url: str
    model: str


def stamp():
    return datetime.now(timezone.utc).isoformat()


class ModelRegistry:
    def __init__(self, store):
        self.store = store

    def list(self):
        with self.store._connect() as conn:
            return [Model(**dict(row)) for row in conn.execute(f'SELECT {PUBLIC_COLUMNS} FROM models ORDER BY id')]

    def add(self, *, name, provider, base_url, model_id, api_key):
        fields = (name, provider, base_url, model_id)
        if (any(not isinstance(v, str) or not v or v != v.strip() or
                any(c.isspace() and c != ' ' or ord(c) < 32 for c in v) for v in fields) or
                len(name) > 100 or len(model_id) > 200 or len(base_url) > 2048 or
                provider not in ('anthropic', 'openai', 'deepseek') or
                not isinstance(api_key, str) or not 8 <= len(api_key) <= 4096 or
                any(not 33 <= ord(c) <= 126 for c in api_key) or
                any(api_key in v for v in fields)):
            raise ModelError('请填写名称、provider、端点、模型 ID 和有效 Key（至少 8 字符）。')
        base_url = normalized_url(base_url)
        encrypted = encrypt_key(api_key)
        at = stamp()
        try:
            with self.store._connect() as conn:
                ident = conn.execute('''INSERT INTO models
                    (name, provider, base_url, model_id, api_key_enc, key_last4, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (name, provider, base_url, model_id, encrypted, api_key[-4:], at, at)).lastrowid
        except sqlite3.IntegrityError:
            raise ModelError('模型名称已存在，请使用其他名称。', 409) from None
        return ident

    def mutate(self, ident, action):
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(f'SELECT {PUBLIC_COLUMNS} FROM models WHERE id = ?', (ident,)).fetchone()
            if row is None:
                raise ModelError('模型不存在，请刷新后重试。', 404)
            if action in ('delete', 'toggle') and row['is_default']:
                raise ModelError('请先切换默认模型，再删除或停用此模型。', 409)
            if action == 'default':
                if not row['enabled']:
                    raise ModelError('请先启用此模型。', 409)
                conn.execute('UPDATE models SET is_default = 0, updated_at = ? WHERE is_default = 1', (stamp(),))
                conn.execute('UPDATE models SET is_default = 1, updated_at = ? WHERE id = ?', (stamp(), ident))
            elif action == 'toggle':
                conn.execute('UPDATE models SET enabled = 1 - enabled, updated_at = ? WHERE id = ?', (stamp(), ident))
            elif action == 'delete':
                conn.execute('DELETE FROM models WHERE id = ?', (ident,))
            else:
                raise ValueError('Unknown model action')

    def credentials(self, ident=None):
        # This is the only SELECT of ciphertext, never used to render a response.
        with self.store._connect() as conn:
            row = conn.execute('SELECT api_key_enc, base_url, model_id FROM models WHERE ' +
                ('is_default = 1 AND enabled = 1' if ident is None else 'id = ?'),
                () if ident is None else (ident,)).fetchone()
        if row is None:
            if ident is not None:
                raise ModelError('模型不存在，请刷新后重试。', 404)
            return None
        return ModelCredentials(decrypt_key(row['api_key_enc']), row['base_url'], row['model_id'])


def resolve_model(store):
    from app.tag import TagSettings
    return ModelRegistry(store).credentials() or TagSettings.from_env()
