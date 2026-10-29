import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.feishu import FeishuAuth, FeishuAuthError, HttpFeishuAuth
from app.session import SESSION_MAX_AGE, STATE_MAX_AGE, SignedCookie
from app.store import SQLiteStore, Store

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_feishu_auth(request: Request) -> FeishuAuth:
    return request.app.state.feishu_auth


def create_app(
    *, settings: Settings | None = None,
    feishu_auth: FeishuAuth | None = None,
    store: Store | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    repository = store if store is not None else SQLiteStore(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store.migrate()
        app.state.store.seed_admin(settings.admin_open_id)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = repository
    app.state.feishu_auth = feishu_auth if feishu_auth is not None else HttpFeishuAuth(settings)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    sessions = SignedCookie(settings.session_secret, salt="web-session", max_age=SESSION_MAX_AGE)
    states = SignedCookie(settings.session_secret, salt="oauth-state", max_age=STATE_MAX_AGE)

    def set_cookie(response: Response, name: str, value: str, max_age: int):
        response.set_cookie(
            name, value, max_age=max_age, httponly=True,
            secure=settings.secure_cookies, samesite="lax", path="/",
        )

    def finish_callback(response: Response) -> Response:
        response.delete_cookie(
            "oauth_state", path="/", httponly=True,
            secure=settings.secure_cookies, samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def error_page(request: Request, status: int, title: str, message: str) -> Response:
        return finish_callback(templates.TemplateResponse(
            request=request, name="error.html",
            context={"title": title, "message": message}, status_code=status,
        ))

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, store: Annotated[Store, Depends(get_store)]):
        open_id = sessions.decode(request.cookies.get("session"))
        user = store.get_user(open_id) if open_id else None
        if user is not None and user.is_active:
            response = templates.TemplateResponse(request=request, name="home.html", context={"user": user})
            response.headers["Cache-Control"] = "no-store"
            return response
        state = secrets.token_urlsafe(32)
        response = RedirectResponse(AUTHORIZE_URL + "?" + urlencode({
            "client_id": settings.feishu_app_id,
            "redirect_uri": settings.feishu_redirect_uri,
            "response_type": "code",
            "state": state,
        }), status_code=302)
        set_cookie(response, "oauth_state", states.encode(state), STATE_MAX_AGE)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/auth/callback")
    def callback(
        request: Request,
        auth: Annotated[FeishuAuth, Depends(get_feishu_auth)],
        store: Annotated[Store, Depends(get_store)],
        code: str | None = None,
        state: str | None = None,
    ):
        if not code or not code.strip():
            return error_page(request, 400, "登录失败", "缺少授权码，请重新登录。")
        expected_state = states.decode(request.cookies.get("oauth_state"))
        if not state or not expected_state or not secrets.compare_digest(state.encode(), expected_state.encode()):
            return error_page(request, 400, "登录失败", "登录请求已失效，请重新登录。")
        try:
            identity = auth.exchange_code(code)
        except FeishuAuthError:
            return error_page(request, 502, "登录失败", "飞书认证暂时失败，请重新登录。")
        user = store.get_user(identity.open_id)
        if user is None or not user.is_active:
            return error_page(request, 403, "无访问权限", "你的账号不在访问白名单中。")
        store.update_profile(identity)
        response = RedirectResponse("/", status_code=302)
        set_cookie(response, "session", sessions.encode(identity.open_id), SESSION_MAX_AGE)
        return finish_callback(response)

    return app
