"""Google sign-in.

Sessions belong to people, so every route that touches one needs to know who
is asking. This module provides that, plus the decorator the routes use.

Setup — you do this once in Google Cloud Console:

  1. console.cloud.google.com → APIs & Services → Credentials
  2. Create Credentials → OAuth client ID → Web application
  3. Authorised redirect URI:  http://127.0.0.1:5001/auth/callback
     (add your real domain there too when you deploy)
  4. Put the client ID and secret in .env

Until those exist the app still runs; it just shows a setup page instead of a
sign-in button.
"""

import functools
import os

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import db
import i18n

bp = Blueprint("auth", __name__)
oauth = OAuth()

GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"


def configured():
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def allowlist():
    """Google accounts allowed to sign in. Empty means open to everyone.

    ALLOWED_EMAILS="me@gmail.com, friend@x.com"  and/or  ALLOWED_DOMAINS="columbia.edu"
    This is the money lock for a portfolio deploy: strangers can browse the
    landing page, but only invited people can reach anything that calls a
    paid API.
    """
    emails = {e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()}
    domains = {d.strip().lower().lstrip("@") for d in os.getenv("ALLOWED_DOMAINS", "").split(",") if d.strip()}
    return emails, domains


def invite_only():
    emails, domains = allowlist()
    return bool(emails or domains)


def email_allowed(email):
    emails, domains = allowlist()
    if not (emails or domains):
        return True
    email = (email or "").lower()
    return email in emails or email.split("@")[-1] in domains


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def request_is_local():
    """True only for a request that actually arrived over loopback.

    This is the part that cannot be got wrong by a misconfigured deploy. Env
    vars live in a dashboard and can be forgotten; the client address cannot.
    A hosted server is never reached by a stranger over 127.0.0.1, so binding
    the escape hatch to loopback closes it everywhere that matters.
    """
    if request.headers.get("X-Forwarded-For"):
        # Behind a proxy at all means this is not a laptop. Render, Heroku,
        # Fly and friends all set it.
        return False
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    remote = (request.remote_addr or "").strip("[]").lower()
    return host in LOOPBACK and remote in LOOPBACK


def dev_login_allowed():
    """A local-only escape hatch so the app is usable before Google is set up.

    Three locks, and every one of them has to be open:
      1. ALLOW_DEV_LOGIN=1        — explicitly opted in
      2. FLASK_ENV != production  — not a deploy that declares itself one
      3. the request came from loopback

    Lock 3 exists because locks 1 and 2 both depend on env vars being set
    correctly on the host, and on 2026-09-08 they were not: the deployed site
    was serving the dev-login button to the public internet and handing out
    working sessions to anyone who pressed it.
    """
    if os.getenv("ALLOW_DEV_LOGIN") != "1":
        return False
    if os.getenv("FLASK_ENV") == "production":
        return False
    return request_is_local()


def init_app(app):
    oauth.init_app(app)
    if configured():
        oauth.register(
            name="google",
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            server_metadata_url=GOOGLE_METADATA,
            client_kwargs={"scope": "openid email profile"},
        )
    app.register_blueprint(bp)


# ------------------------------------------------------------------ current user

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = db.get_user(user_id)
    if not user:
        # The account was deleted out from under the cookie.
        session.pop("user_id", None)
    return user


def owner_required(view):
    """Owner-only pages and APIs. Others get 404, so the admin surface is
    not even confirmed to exist."""
    import plans

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            if request.path.startswith("/api/"):
                return jsonify({"error": "로그인이 필요합니다."}), 401
            return redirect(url_for("auth.login", next=request.path))
        if not plans.is_owner(user):
            from flask import abort
            abort(404)
        return view(*args, **kwargs)

    return wrapped


def login_required(view):
    """Guard a page. API routes get JSON, pages get redirected to sign-in."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "로그인이 필요합니다.", "login": url_for("auth.login")}), 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


# ------------------------------------------------------------------ routes

@bp.route("/login")
def login():
    if current_user():
        return redirect(url_for("review"))
    return render_template(
        "login.html",
        configured=configured(),
        dev_login=dev_login_allowed(),
        next_url=request.args.get("next", ""),
        invite_only=invite_only(),
    )


@bp.route("/auth/google")
def google_start():
    if not configured():
        return redirect(url_for("auth.login"))
    # Remember where they were headed before we bounced them to Google.
    session["after_login"] = request.args.get("next") or url_for("review")
    return oauth.google.authorize_redirect(
        url_for("auth.callback", _external=True)
    )


@bp.route("/auth/callback")
def callback():
    if not configured():
        return redirect(url_for("auth.login"))
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        return render_template("login.html", configured=True, dev_login=dev_login_allowed(),
                               error="구글 로그인에 실패했습니다. 다시 시도해주세요."), 400

    info = token.get("userinfo") or {}
    if not info.get("sub") or not info.get("email"):
        return render_template("login.html", configured=True, dev_login=dev_login_allowed(),
                               error=i18n._("구글에서 계정 정보를 받지 못했습니다.")), 400

    if not email_allowed(info["email"]):
        # Not on the list: no account is created, nothing is stored.
        return render_template("login.html", configured=True, dev_login=dev_login_allowed(),
                               invite_only=True,
                               error=i18n._("초대받은 계정만 로그인할 수 있습니다. ({email})").format(email=info["email"])), 403

    user = db.upsert_user(
        google_sub=info["sub"],
        email=info["email"],
        name=info.get("name"),
        picture=info.get("picture"),
    )
    session["user_id"] = user["id"]
    session.permanent = True

    _adopt_orphans(user["id"])
    return redirect(session.pop("after_login", None) or url_for("review"))


@bp.route("/auth/dev", methods=["POST"])
def dev_login():
    """Sign in as a local test account. Only when explicitly enabled."""
    if not dev_login_allowed():
        return redirect(url_for("auth.login"))
    user = db.upsert_user(
        google_sub="dev-local-account", email="dev@localhost", name="로컬 테스트 계정"
    )
    session["user_id"] = user["id"]
    session.permanent = True
    _adopt_orphans(user["id"])
    return redirect(url_for("review"))


def _adopt_orphans(user_id):
    """Sessions made before accounts existed have no owner; give them to the
    first person who signs in, so nothing recorded so far is stranded."""
    if db.get_user(user_id):
        db.claim_orphan_sessions(user_id)


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("landing"))
