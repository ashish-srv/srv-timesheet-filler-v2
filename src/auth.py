"""
Authentication: real Google Sign-In (Authlib) restricted to the company domain
and the roster, plus a DEV/simulated login used only for local testing.

Session is stored in a signed cookie via Starlette SessionMiddleware (added in
main.py). current_user(request) returns the logged-in user dict or None.
"""
import os
from fastapi import Request
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

from . import db

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# The roster (employees table) is the allowlist: anyone in it can sign in,
# whether they use a company email or a personal Gmail (e.g. interns). Setting
# RESTRICT_DOMAIN to a domain would additionally require that domain; left blank
# by default so roster membership alone controls access.
RESTRICT_DOMAIN = os.environ.get("RESTRICT_DOMAIN", "").strip().lower()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
DEV_LOGIN = os.environ.get("DEV_LOGIN", "0") == "1"   # test-only simulated login


def is_admin(email, role=""):
    return role in ("admin", "hr") or (email or "").strip().lower() in ADMIN_EMAILS

_oauth = None


def _get_oauth():
    global _oauth
    if _oauth is None and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        from authlib.integrations.starlette_client import OAuth
        _oauth = OAuth()
        # No 'hd' hint: personal Gmail accounts (interns) must be able to
        # authenticate too. Access is decided by the roster, not the domain.
        _oauth.register(
            name="google",
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            client_kwargs={"scope": "openid email profile"},
        )
    return _oauth


def current_user(request: Request):
    return request.session.get("user")


def _establish_session(request: Request, email: str, name: str):
    """Access is gated by the roster: the email must be an active employee.
    Company emails and approved intern Gmail IDs both work, as long as they
    are in the roster. An optional RESTRICT_DOMAIN adds a domain requirement."""
    email = (email or "").strip().lower()
    if RESTRICT_DOMAIN and not email.endswith("@" + RESTRICT_DOMAIN):
        return None, f"Only {RESTRICT_DOMAIN} accounts are allowed."
    emp = db.get_employee(email)
    if not emp:
        # Bootstrap: a configured admin (ADMIN_EMAILS) can sign in on a fresh
        # system to upload the first roster, even before they're in it.
        if is_admin(email):
            user = {"email": email, "name": name or email,
                    "is_manager": False, "is_admin": True, "role": "admin"}
            request.session["user"] = user
            db.audit(email, "login_bootstrap_admin")
            return user, None
        return None, "Your account is not in the employee roster. Contact admin."
    if not emp.get("active", 1):
        return None, "Your account is inactive. Contact admin."
    user = {
        "email": email,
        "name": name or emp.get("name") or email,
        "is_manager": db.is_manager(email),
        "is_admin": is_admin(email, emp.get("role", "user")),
        "role": emp.get("role", "user"),
    }
    request.session["user"] = user
    db.audit(email, "login", {"is_manager": user["is_manager"]})
    return user, None


def register_auth(app):
    @app.get("/login")
    async def login(request: Request):
        oauth = _get_oauth()
        if not oauth:
            return HTMLResponse(
                "<h3>Google login is not configured.</h3>"
                "<p>Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, or use dev login.</p>",
                status_code=503,
            )
        redirect_uri = request.url_for("auth_callback")
        return await oauth.google.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        oauth = _get_oauth()
        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception as e:
            return HTMLResponse(f"<h3>Login failed.</h3><p>{e}</p>", status_code=400)
        info = token.get("userinfo") or {}
        user, err = _establish_session(request, info.get("email"), info.get("name"))
        if err:
            return HTMLResponse(f"<h3>Access denied.</h3><p>{err}</p>", status_code=403)
        return RedirectResponse(url="/app")

    @app.get("/logout")
    async def logout(request: Request):
        request.session.pop("user", None)
        return RedirectResponse(url="/")

    # ---- test-only simulated login, enabled only when DEV_LOGIN=1 ----
    if DEV_LOGIN:
        @app.get("/dev-login")
        async def dev_login(request: Request, email: str):
            user, err = _establish_session(request, email, "")
            if err:
                return JSONResponse({"ok": False, "error": err}, status_code=403)
            return JSONResponse({"ok": True, "user": user})
