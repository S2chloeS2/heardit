"""Heardit — capture a lecture or meeting, and turn it into notes.

Three ways in:
  system — audio playing on this computer (Zoom, YouTube, any tab)
  mic    — the room, via the microphone
  link   — paste a URL and let the server fetch it

Everything is scoped to a session, and sessions are stored in SQLite so the
review screen still has something in it tomorrow.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback

from dotenv import load_dotenv
from flask import Flask, abort, after_this_request, jsonify, render_template, request, send_file, url_for

load_dotenv()

import ai
import auth
import billing
import db
import engines
import i18n
import media
import plans

app = Flask(__name__, static_url_path="/static")

# Signs the session cookie. A fixed value in .env keeps people logged in across
# restarts; without one we generate a throwaway and everyone is signed out.
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

# Reject oversized bodies before Werkzeug reads them into memory or onto disk.
# Long recordings arrive as uploads, so this is generous — but not unbounded.
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024

# Recordings are kept so a line of the transcript can be played back. They
# live next to the database unless AUDIO_DIR points elsewhere (on Render, the
# persistent disk).
AUDIO_DIR = os.getenv("AUDIO_DIR") or os.path.join(os.path.dirname(db.DB_PATH), "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

db.init()
auth.init_app(app)
_seeded = billing.seed_promos()
if _seeded:
    app.logger.info("seeded promo codes: %s", ", ".join(_seeded))
i18n.init_app(app)


@app.context_processor
def inject_user():
    """Every template can show who is signed in and how much time is left."""
    user = auth.current_user()
    return {
        "current_user": user,
        "allowance": plans.allowance(user["id"], user) if user else None,
        "is_owner": plans.is_owner(user) if user else False,
        "cur": current_currency(),
        "currencies": plans.CURRENCIES,
        "yearly_pct": {k: plans.yearly_saving_pct(v, current_currency()) for k, v in plans.PLANS.items()},
    }


def current_currency():
    """KRW for the Korean UI, USD otherwise, unless the visitor picked one."""
    from flask import session as flask_session
    return plans.currency_for(i18n.current_lang(), flask_session.get("currency"))


@app.template_filter("money")
def money_filter(amount, currency=None, per_month=False):
    return plans.money(int(amount or 0), currency or current_currency(), i18n.current_lang(), per_month)


@app.route("/currency/<code>")
def set_currency(code):
    from flask import redirect, session as flask_session
    if code in plans.CURRENCIES:
        flask_session["currency"] = code
        flask_session.permanent = True
    target = request.referrer or url_for("pricing")
    if not target.startswith(request.host_url):
        target = url_for("pricing")
    return redirect(target)


# ------------------------------------------------------- production guards

IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"

if IS_PRODUCTION:
    # A deployed server must never start in a state that hands out accounts
    # for free or signs cookies with a throwaway key.
    if os.getenv("ALLOW_DEV_LOGIN") == "1":
        raise SystemExit("FLASK_ENV=production 에서는 ALLOW_DEV_LOGIN 을 켤 수 없습니다.")
    if not os.getenv("SECRET_KEY"):
        raise SystemExit("FLASK_ENV=production 에는 고정 SECRET_KEY 가 필요합니다.")
    if not (os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")):
        raise SystemExit("FLASK_ENV=production 에는 구글 로그인 자격증명이 필요합니다.")


# Per-user request throttle for the API. Plans cap *minutes transcribed*;
# this caps *requests*, so a runaway script cannot hammer the model APIs or
# the database even inside its allowance. In-memory: fine for one process,
# and gunicorn workers each keep their own window (so the real cap is a bit
# higher than the number below — acceptable for a first line of defence).
import collections
import time as _time

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "90"))
_hits = collections.defaultdict(collections.deque)
_hits_lock = threading.Lock()


@app.after_request
def security_headers(response):
    """Browser-side hardening. HSTS only where HTTPS is guaranteed."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), payment=()")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.before_request
def throttle_api():
    if not request.path.startswith("/api/"):
        return None
    user = auth.current_user()
    key = f"u{user['id']}" if user else f"ip{request.remote_addr}"
    now = _time.monotonic()
    with _hits_lock:
        window = _hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            retry = int(60 - (now - window[0])) + 1
            resp = jsonify({"error": i18n._("요청이 너무 잦습니다. {n}초 뒤 다시 시도해주세요.").format(n=retry)})
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry)
            return resp
        window.append(now)
    return None


def current_folders():
    user = auth.current_user()
    return db.list_folders(user["id"]) if user else []


def owned_folder(folder_id):
    user = auth.current_user()
    return db.get_folder(folder_id, user["id"]) if user else None

# Progress for link imports, keyed by session id. In-process is fine: a failed
# import is cheap to retry, and the transcript itself is already in SQLite.
_jobs = {}
_jobs_lock = threading.Lock()


def set_job(session_id, **fields):
    with _jobs_lock:
        _jobs.setdefault(session_id, {}).update(fields)


def get_job(session_id):
    with _jobs_lock:
        return dict(_jobs.get(session_id, {}))


def fail(message, code=400):
    return jsonify({"error": message}), code


def owned(session_id):
    """The session, but only if it belongs to whoever is asking.

    Returns None for someone else's session as well as for one that does not
    exist, so a stranger cannot tell the two apart.
    """
    user = auth.current_user()
    if not user:
        return None
    return db.get_session(session_id, user_id=user["id"])


def _keywords_of(session):
    try:
        return json.loads(session.get("keywords") or "[]")
    except (ValueError, TypeError):
        return []


def session_audio_dir(session_id):
    path = os.path.join(AUDIO_DIR, str(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def keep_audio(session_id, src, name):
    """Move a recording into the session's audio folder. Returns the relative
    name the segment rows point at."""
    dst = os.path.join(session_audio_dir(session_id), name)
    shutil.move(src, dst)
    return name


def drop_audio(session_id):
    shutil.rmtree(os.path.join(AUDIO_DIR, str(session_id)), ignore_errors=True)


def require(feature):
    """403 with an upgrade hint when the user's plan lacks `feature`."""
    user = auth.current_user()
    if plans.can(user, feature):
        return None
    return jsonify({"error": i18n._("이 기능은 스튜던트 플랜부터 쓸 수 있습니다."),
                    "upgrade": url_for("pricing")}), 403


def session_language(session):
    """The transcript's language code, detecting and caching it on first use."""
    lang = session.get("language")
    if lang:
        return lang
    text = db.get_transcript(session["id"])
    if len(text) < 80:
        return None
    lang = ai.detect_language(text)
    if lang:
        db.update_session(session["id"], language=lang)
    return lang


# --------------------------------------------------------------------- pages

@app.route("/")
def landing():
    return render_template("landing.html", plans=plans.PLANS, order=plans.ORDER, topups=plans.TOPUPS)


@app.route("/new")
@auth.login_required
def new_session():
    return render_template("new.html")


@app.route("/session/<int:session_id>")
@auth.login_required
def session_view(session_id):
    session = db.get_session(session_id, user_id=auth.current_user()["id"])
    if not session:
        return render_template("missing.html", session_id=session_id), 404
    speakers = db.speaker_stats(session_id)
    return render_template(
        "session.html",
        session=session,
        segments=db.get_segments(session_id),
        messages=db.get_messages(session_id),
        keywords=_keywords_of(session),
        speakers=speakers,
        speaker_order={sp["label"]: i for i, sp in enumerate(speakers)},
        speaker_names=db.get_speaker_names(session_id),
        keyword_notes=db.get_keyword_notes(session_id),
        folders=current_folders(),
        engines=engines.available(),
        languages=ai.LANGUAGE_NAMES,
        feat=plans.features(auth.current_user()),
    )


@app.route("/review")
@auth.login_required
def review():
    user_id = auth.current_user()["id"]
    raw = request.args.get("folder")
    folder_filter = None
    if raw == "none":
        folder_filter = "none"
    elif raw and raw.isdigit() and db.get_folder(int(raw), user_id):
        folder_filter = int(raw)

    sessions = db.list_sessions(user_id, folder_id=folder_filter)
    for s in sessions:
        s["keyword_list"] = _keywords_of(s)
        s["excerpt"] = _excerpt(s.get("summary"))
    return render_template(
        "review.html", sessions=sessions, folders=current_folders(),
        folder_filter=folder_filter,
    )


@app.route("/folder/<int:folder_id>")
@auth.login_required
def folder_view(folder_id):
    folder = owned_folder(folder_id)
    if not folder:
        return render_template("missing.html", session_id=None), 404
    sessions = db.list_sessions(auth.current_user()["id"], folder_id=folder_id)
    for s in sessions:
        s["keyword_list"] = _keywords_of(s)
        s["excerpt"] = _excerpt(s.get("summary"))
    return render_template(
        "folder.html", folder=folder, sessions=sessions,
        messages=db.get_folder_messages(folder_id),
    )


@app.route("/account")
@auth.login_required
def account():
    user = auth.current_user()
    return render_template(
        "account.html", plans=plans.PLANS, order=plans.ORDER, topups=plans.TOPUPS,
        budget=plans.budget_status(), orders=db.list_orders(user["id"]),
        payments_open=billing.stripe_ready() or auth.dev_login_allowed(),
        simulated=not billing.stripe_ready() and auth.dev_login_allowed(),
        has_portal=billing.stripe_ready() and bool(user.get("stripe_customer_id")),
        owner=plans.is_owner(user), inquiries=db.user_inquiries(user["id"]),
    )


@app.route("/pricing")
def pricing():
    user = auth.current_user()
    return render_template(
        "pricing.html", plans=plans.PLANS, order=plans.ORDER, topups=plans.TOPUPS,
        comparison=plans.COMPARISON, features=plans.FEATURES,
        current=plans.plan_key(user) if user else None,
        payments_open=billing.stripe_ready() or auth.dev_login_allowed(),
    )


def _excerpt(summary, limit=140):
    """First line of real content from a markdown summary, for the review list."""
    for line in (summary or "").splitlines():
        text = line.strip().lstrip("#-* ").strip()
        if text:
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


# ------------------------------------------------------------------ sessions

@app.route("/api/sessions", methods=["POST"])
@auth.login_required
def api_create_session():
    body = request.get_json(silent=True) or {}
    source = body.get("source", "mic")
    if source not in {"system", "mic", "both", "link"}:
        return fail(i18n._("Unknown source. Use system, mic, both, or link."))

    kind = body.get("kind", "lecture")
    title = (body.get("title") or "").strip() or ("Meeting" if kind == "meeting" else "Lecture")
    session_id = db.create_session(
        title=title, kind=kind, source=source, user_id=auth.current_user()["id"]
    )
    return jsonify({"id": session_id, "url": url_for("session_view", session_id=session_id)})


@app.route("/api/sessions/<int:session_id>", methods=["PATCH", "DELETE"])
@auth.login_required
def api_modify_session(session_id):
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)

    if request.method == "DELETE":
        db.delete_session(session_id)
        drop_audio(session_id)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    fields = {k: v for k, v in body.items() if k in {"title", "keywords", "kind"}}

    # Editing the notes by hand, and choosing their language, are paid features.
    if "summary" in body or "exam_sheet" in body:
        if (denied := require("edit_notes")):
            return denied
        for key in ("summary", "exam_sheet"):
            if key in body:
                fields[key] = (body[key] or "")[:200_000]
    if "notes_lang" in body:
        if (denied := require("notes_lang")):
            return denied
        code = (body["notes_lang"] or "").lower()[:2]
        fields["notes_lang"] = code if code in ai.LANGUAGE_NAMES else None

    # Filing into a folder: only into one of the caller's own, or out of any.
    if "folder_id" in body:
        target = body["folder_id"]
        if target in (None, "", "none"):
            fields["folder_id"] = None
        elif str(target).isdigit() and owned_folder(int(target)):
            fields["folder_id"] = int(target)
        else:
            return fail(i18n._("그 폴더를 찾을 수 없습니다."), 404)

    db.update_session(session_id, **fields)
    return jsonify({"ok": True})


# ------------------------------------------------------------------- folders

@app.route("/api/folders", methods=["POST"])
@auth.login_required
def api_create_folder():
    if (denied := require("folders")):
        return denied
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()[:80]
    if not name:
        return fail(i18n._("폴더 이름을 입력해주세요."))
    folder_id = db.create_folder(auth.current_user()["id"], name)
    return jsonify({"id": folder_id, "name": name, "url": url_for("folder_view", folder_id=folder_id)})


@app.route("/api/folders/<int:folder_id>", methods=["PATCH", "DELETE"])
@auth.login_required
def api_modify_folder(folder_id):
    if not owned_folder(folder_id):
        return fail(i18n._("폴더를 찾을 수 없습니다."), 404)
    if request.method == "DELETE":
        db.delete_folder(folder_id)
        return jsonify({"ok": True})
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()[:80]
    if not name:
        return fail(i18n._("폴더 이름을 입력해주세요."))
    db.rename_folder(folder_id, name)
    return jsonify({"ok": True})


@app.route("/api/folders/<int:folder_id>/chat", methods=["POST"])
@auth.login_required
def api_folder_chat(folder_id):
    """Ask across every recording in the folder — a whole course at once."""
    if not owned_folder(folder_id):
        return fail(i18n._("폴더를 찾을 수 없습니다."), 404)
    question = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not question:
        return fail(i18n._("질문을 입력해주세요."))
    try:
        reply = ai.answer_folder(
            question, db.folder_corpus(folder_id), history=db.get_folder_messages(folder_id)
        )
    except Exception as exc:
        app.logger.error("folder chat failed: %s", traceback.format_exc())
        return fail(str(exc), 502)
    db.add_folder_message(folder_id, "user", question)
    db.add_folder_message(folder_id, "assistant", reply)
    return jsonify({"reply": reply})


# ------------------------------------------------------------------- account

@app.route("/api/billing/quote", methods=["POST"])
@auth.login_required
def api_billing_quote():
    body = request.get_json(silent=True) or {}
    try:
        q = billing.quote(body.get("item"), auth.current_user(), code=(body.get("code") or "").strip() or None,
                          currency=current_currency())
    except billing.BillingError as exc:
        return fail(str(exc))
    lang = i18n.current_lang()
    return jsonify({k: v for k, v in q.items() if k != "item"} | {
        "item": q["item"]["key"], "name": i18n._(q["item"]["name"]),
        "list_price_label": plans.money(q["list_price"], q["currency"], lang),
        "discount_label": plans.money(q["discount"], q["currency"], lang),
        "amount_label": plans.money(q["amount"], q["currency"], lang)
                        + ((" / 년" if lang == "ko" else " / yr") if q["item"].get("interval") == "year"
                           else (" / 월" if lang == "ko" else " / mo") if q["kind"] == "subscription" else ""),
    })


@app.route("/api/billing/checkout", methods=["POST"])
@auth.login_required
def api_billing_checkout():
    """Start a purchase. Stripe when configured; a simulated instant purchase
    on a local dev build; refused on a production build without Stripe."""
    user = auth.current_user()
    body = request.get_json(silent=True) or {}
    try:
        q = billing.quote(body.get("item"), user, code=(body.get("code") or "").strip() or None,
                          currency=current_currency())
    except billing.BillingError as exc:
        return fail(str(exc))

    if billing.stripe_ready():
        try:
            url = billing.checkout_url(
                user, q,
                success_url=url_for("account", _external=True) + "?paid=1",
                cancel_url=url_for("pricing", _external=True),
            )
        except Exception as exc:
            app.logger.error("checkout failed: %s", traceback.format_exc())
            return fail(str(exc), 502)
        return jsonify({"url": url})

    if auth.dev_login_allowed():
        billing.fulfil(user, q, "simulated", f"sim:{user['id']}:{int(time.time() * 1000)}")
        return jsonify({"url": url_for("account") + "?paid=1", "simulated": True})

    return fail(i18n._("결제를 준비하고 있습니다. 열리면 계정 페이지에서 바로 결제할 수 있습니다."), 503)


@app.route("/api/billing/portal", methods=["POST"])
@auth.login_required
def api_billing_portal():
    try:
        return jsonify({"url": billing.portal_url(auth.current_user(), url_for("account", _external=True))})
    except Exception as exc:
        return fail(str(exc), 502)


@app.route("/api/billing/webhook", methods=["POST"])
def api_billing_webhook():
    if not billing.stripe_ready():
        abort(404)
    try:
        note = billing.handle_webhook(request.get_data(), request.headers.get("Stripe-Signature", ""))
    except billing.SignatureError as exc:
        app.logger.error("webhook signature rejected: %s", exc)
        return fail("bad signature: check STRIPE_WEBHOOK_SECRET", 400)
    except Exception:
        app.logger.error("webhook handler failed: %s", traceback.format_exc())
        return fail("handler failed; see server log", 500)
    return jsonify({"ok": True, "note": note})


@app.route("/api/billing/redeem", methods=["POST"])
@auth.login_required
def api_billing_redeem():
    """Comp codes: a plan for free, for the owner and invited reviewers."""
    code = ((request.get_json(silent=True) or {}).get("code") or "").strip()
    if not code:
        return fail(i18n._("코드를 입력해주세요."))
    try:
        plan, until = billing.redeem_comp(code, auth.current_user())
    except billing.BillingError as exc:
        return fail(str(exc))
    return jsonify({"ok": True, "plan": plan, "until": until})


# ------------------------------------------------------------- transcription

@app.route("/api/sessions/<int:session_id>/transcribe", methods=["POST"])
@auth.login_required
def api_transcribe(session_id):
    """Accept one audio clip from the browser and return its text."""
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)

    clip = request.files.get("file")
    if not clip:
        return fail(i18n._("No audio was attached."))

    data = clip.read()
    if not data:
        return fail(i18n._("The audio clip was empty."))
    if len(data) > ai.MAX_UPLOAD_BYTES:
        return fail(i18n._("That clip is too large. Keep clips under 24 MB."))

    user = auth.current_user()
    suffix = os.path.splitext(clip.filename or "")[1] or ".webm"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    kept = None
    try:
        tmp.write(data)
        tmp.close()

        # Meter before spending: a clip is ~6 s, but measure it rather than assume.
        seconds = media.duration_of(tmp.name) or 6.0
        ok, message, allowance = plans.check(user["id"], seconds, user)
        if not ok:
            return jsonify({"error": message, "quota": True,
                            "remaining_s": allowance["remaining_s"]}), 402
        gok, gmsg = plans.global_check(seconds)
        if not gok:
            return jsonify({"error": gmsg, "capacity": True}), 503

        # Live clips are transcribed one at a time for fast feedback, so no
        # diarization here: speaker A in one clip is not speaker A in the next.
        # The session can be re-run with diarization once recording stops.
        session = db.get_session(session_id)
        text = ai.transcribe_text(
            tmp.name,
            kind=session.get("kind", "lecture"),
            prompt=db.get_transcript(session_id),
        )
        plans.record_usage(user["id"], session_id, seconds, user)
        if text:
            # Keep the clip so this line can be played back later.
            name = f"clip-{int(time.time() * 1000)}{suffix}"
            kept = keep_audio(session_id, tmp.name, name)
    except Exception as exc:
        app.logger.error("transcribe failed: %s", traceback.format_exc())
        return fail(str(exc), 502)
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

    remaining = plans.allowance(user["id"], user)["remaining_s"]
    if not text:
        return jsonify({"text": "", "note": "no speech detected", "remaining_s": remaining})

    seg_id = db.add_segment(session_id, text, audio_path=kept, audio_offset_ms=0)
    return jsonify({
        "text": text, "id": seg_id, "remaining_s": remaining,
        "audio": url_for("api_audio", session_id=session_id, name=kept) if kept else None,
    })


# --------------------------------------------------------------- link import

@app.route("/api/import", methods=["POST"])
@auth.login_required
def api_import():
    """Start fetching a URL in the background; returns a session to watch."""
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not ai.looks_like_url(url):
        return fail(i18n._("That does not look like a link. It should start with http."))

    try:
        info = media.probe(url)
    except media.MediaError as exc:
        return fail(str(exc))

    user = auth.current_user()
    ok, message, allowance = plans.check(user["id"], info["duration"], user)
    if not ok:
        return jsonify({"error": message, "quota": True,
                        "remaining_s": allowance["remaining_s"]}), 402
    gok, gmsg = plans.global_check(info["duration"])
    if not gok:
        return jsonify({"error": gmsg, "capacity": True}), 503

    session_id = db.create_session(
        title=info["title"], kind=body.get("kind", "lecture"), source="link",
        source_url=url, user_id=user["id"],
    )
    set_job(session_id, state="starting", done=0, total=0, message="Preparing…")

    # A TED link resolves to its YouTube mirror; download from there.
    threading.Thread(
        target=_run_import, args=(session_id, info["webpage_url"]), daemon=True
    ).start()

    return jsonify(
        {
            "id": session_id,
            "title": info["title"],
            "duration": info["duration"],
            "via_youtube": info.get("via_youtube", False),
            "url": url_for("session_view", session_id=session_id),
        }
    )


@app.route("/api/import/file", methods=["POST"])
@auth.login_required
def api_import_file():
    """Accept a recording — a Zoom/Teams local recording, a voice memo, anything
    ffmpeg can decode — and run it through the same pipeline as a link."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return fail(i18n._("No file was attached."))

    workdir = media.workspace()
    suffix = os.path.splitext(upload.filename)[1] or ".m4a"
    path = os.path.join(workdir, f"upload{suffix}")
    upload.save(path)

    if os.path.getsize(path) == 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return fail(i18n._("That file is empty."))

    seconds = media.duration_of(path)
    if seconds <= 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return fail(i18n._("오디오 길이를 읽을 수 없습니다. 오디오·영상 파일이 맞는지 확인해주세요."))

    user = auth.current_user()
    ok, message, allowance = plans.check(user["id"], seconds, user)
    if not ok:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"error": message, "quota": True,
                        "remaining_s": allowance["remaining_s"]}), 402
    gok, gmsg = plans.global_check(seconds)
    if not gok:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"error": gmsg, "capacity": True}), 503

    title = os.path.splitext(os.path.basename(upload.filename))[0][:120]
    session_id = db.create_session(
        title=title, kind=request.form.get("kind", "meeting"), source="link",
        user_id=auth.current_user()["id"],
    )
    set_job(session_id, state="starting", done=0, total=0, message="Preparing…")

    threading.Thread(
        target=_run_pipeline, args=(session_id, path, workdir), daemon=True
    ).start()

    return jsonify(
        {"id": session_id, "title": title,
         "url": url_for("session_view", session_id=session_id)}
    )


def _run_import(session_id, url):
    workdir = media.workspace()
    try:
        set_job(session_id, state="downloading", message="Downloading audio…")
        path = media.download_audio(url, workdir)
    except Exception as exc:
        app.logger.error("download failed: %s", traceback.format_exc())
        set_job(session_id, state="error", message=str(exc))
        shutil.rmtree(workdir, ignore_errors=True)
        return
    _run_pipeline(session_id, path, workdir)


def _run_pipeline(session_id, path, workdir):
    """Split an audio file, transcribe every chunk, then summarise."""
    try:
        set_job(session_id, state="splitting", message="Splitting into chunks…")
        chunks = media.split(path, workdir)
        set_job(session_id, total=len(chunks), state="transcribing")

        session = db.get_session(session_id)
        kind = session.get("kind", "lecture")
        # The request was already checked against the plan; now record what it
        # actually cost, from the file itself.
        plans.record_usage(session.get("user_id"), session_id, media.duration_of(path))
        # Meetings want to know who spoke; lectures are one voice, so we skip
        # diarization there and use the cheaper engine.
        owner_user = db.get_user(session.get("user_id"))
        diarize = kind == "meeting" and plans.can(owner_user, "diarization")
        offset_ms = 0
        # The whole recording stays on disk so any line can be replayed.
        audio_name = keep_audio(session_id, path, "full" + os.path.splitext(path)[1])

        for index, chunk in enumerate(chunks, start=1):
            set_job(session_id, done=index - 1,
                    message=f"Transcribing part {index} of {len(chunks)}…")
            result = ai.transcribe(
                chunk, kind=kind, diarize=diarize, prompt=db.get_transcript(session_id)
            )
            for seg in result["segments"]:
                # Non-diarized engines return one block per chunk with no
                # timing; the chunk's own offset is still a usable start.
                start = (seg["start_ms"] + offset_ms) if seg.get("start_ms") is not None else offset_ms
                db.add_segment(
                    session_id,
                    seg["text"],
                    speaker=seg.get("speaker"),
                    start_ms=start,
                    end_ms=(seg["end_ms"] + offset_ms) if seg.get("end_ms") is not None else None,
                    audio_path=audio_name,
                    audio_offset_ms=start,
                )
            # Chunks are cut at a fixed length, so each one starts that much
            # further into the recording.
            offset_ms += media.CHUNK_SECONDS * 1000

        set_job(session_id, done=len(chunks), state="summarizing",
                message="Writing the summary…")
        _build_summary(session_id)
        set_job(session_id, state="done", message="Ready")

    except Exception as exc:
        app.logger.error("pipeline failed: %s", traceback.format_exc())
        set_job(session_id, state="error", message=str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.route("/api/sessions/<int:session_id>/progress")
@auth.login_required
def api_progress(session_id):
    # Ownership first: progress leaks a session's title and state otherwise.
    session = owned(session_id)
    if not session:
        return fail(i18n._("기록을 찾을 수 없습니다."), 404)
    job = get_job(session_id)
    if job:
        return jsonify(job)
    return jsonify({"state": "done" if session.get("summary") else "idle"})


# ------------------------------------------------------------------- summary

def _build_summary(session_id):
    session = db.get_session(session_id)
    # Meetings summarise better when the model knows who said what.
    transcript = db.get_transcript(session_id, with_speakers=True)
    if not transcript:
        raise ValueError("There is nothing transcribed in this session yet.")

    kind = session.get("kind", "lecture")
    user = db.get_user(session.get("user_id"))
    feat = plans.features(user)
    lang = session.get("notes_lang") if feat["notes_lang"] and session.get("notes_lang") else session_language(session)
    result = ai.summarize(
        transcript, kind=kind, lang=lang,
        slides=session.get("attachment_text") if feat["slides"] else None,
        premium=feat["premium_notes"],
    )
    fields = {"summary": result["summary"], "keywords": result["keywords"], "exam_sheet": None}
    if feat["exam_sheet"]:
        try:
            fields["exam_sheet"] = ai.exam_sheet(result["summary"], kind=kind, lang=lang)
        except Exception:
            # The notes are the product; a failed cram sheet must not lose them.
            app.logger.error("exam sheet failed: %s", traceback.format_exc())
    if result.get("language") and not session.get("language"):
        fields["language"] = result["language"]
    # Only adopt the generated title if the user has not set one of their own.
    if session["title"] in {"Lecture", "Meeting", "Untitled session"}:
        fields["title"] = result["title"]
    db.update_session(session_id, **fields)
    return result


@app.route("/api/sessions/<int:session_id>/summary", methods=["POST"])
@auth.login_required
def api_summary(session_id):
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)
    try:
        _build_summary(session_id)
    except ValueError as exc:
        return fail(str(exc))
    except Exception as exc:
        app.logger.error("summary failed: %s", traceback.format_exc())
        return fail(str(exc), 502)

    session = db.get_session(session_id)
    return jsonify({
        "summary": session["summary"],
        "exam_sheet": session.get("exam_sheet") or "",
        "keywords": _keywords_of(session),
        "title": session["title"],
    })


@app.route("/api/sessions/<int:session_id>/keyword")
@auth.login_required
def api_keyword(session_id):
    if not owned(session_id):
        return fail(i18n._("기록을 찾을 수 없습니다."), 404)
    keyword = (request.args.get("q") or "").strip()
    if not keyword:
        return fail(i18n._("키워드를 지정해주세요."))
    # Already looked up once? Serve the saved note — no second model call.
    cached = db.get_keyword_notes(session_id).get(keyword)
    if cached:
        return jsonify({"keyword": keyword, "explanation": cached, "cached": True})

    transcript = db.get_transcript(session_id)
    if not transcript:
        return fail(i18n._("아직 받아적은 내용이 없습니다."))
    try:
        explanation = ai.explain_keyword(
            keyword, transcript, lang=session_language(db.get_session(session_id))
        )
    except Exception as exc:
        app.logger.error("keyword failed: %s", traceback.format_exc())
        return fail(str(exc), 502)

    db.save_keyword_note(session_id, keyword, explanation)
    return jsonify({"keyword": keyword, "explanation": explanation, "cached": False})


# ---------------------------------------------------------------------- chat

@app.route("/api/sessions/<int:session_id>/chat", methods=["POST"])
@auth.login_required
def api_chat(session_id):
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)

    question = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not question:
        return fail(i18n._("Type a question first."))

    transcript = db.get_transcript(session_id)
    session = db.get_session(session_id)
    try:
        reply = ai.answer(
            question, transcript, history=db.get_messages(session_id),
            lang=session_language(session),
            slides=session.get("attachment_text") if plans.can(auth.current_user(), "slides") else None,
        )
    except Exception as exc:
        app.logger.error("chat failed: %s", traceback.format_exc())
        return fail(str(exc), 502)

    db.add_message(session_id, "user", question)
    db.add_message(session_id, "assistant", reply)
    return jsonify({"reply": reply})


@app.route("/api/sessions/<int:session_id>/speakers", methods=["GET", "PATCH"])
@auth.login_required
def api_speakers(session_id):
    """Read talk-time per speaker, or rename one."""
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)

    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        label = (body.get("label") or "").strip()
        name = (body.get("name") or "").strip()
        if not label:
            return fail("어떤 화자인지 지정해주세요.")
        db.set_speaker_name(session_id, label, name[:60])
        return jsonify({"ok": True})

    return jsonify({"speakers": db.speaker_stats(session_id)})


@app.route("/api/sessions/<int:session_id>/transcript")
@auth.login_required
def api_transcript(session_id):
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)
    return jsonify({"segments": db.get_segments(session_id)})


# ------------------------------------------------------------ attachments

ATTACHMENT_MAX_MB = int(os.getenv("ATTACHMENT_MAX_MB", "30"))
ATTACHMENT_TEXT_CAP = 60_000


def _pdf_text(path):
    """Text of a PDF, capped, for the notes. Scanned PDFs yield nothing."""
    from pypdf import PdfReader
    out = []
    used = 0
    for i, page in enumerate(PdfReader(path).pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        block = f"[p.{i}] {text}\n"
        if used + len(block) > ATTACHMENT_TEXT_CAP:
            break
        out.append(block)
        used += len(block)
    return "".join(out)


@app.route("/api/sessions/<int:session_id>/attachment", methods=["POST", "DELETE"])
@auth.login_required
def api_attachment(session_id):
    """Slides (PDF) kept beside a note: viewed alongside it, and read into
    the notes when they are generated."""
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)
    if (denied := require("slides")):
        return denied
    path = os.path.join(session_audio_dir(session_id), "slides.pdf")

    if request.method == "DELETE":
        if os.path.exists(path):
            os.unlink(path)
        db.update_session(session_id, attachment_name=None, attachment_text=None)
        return jsonify({"ok": True})

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return fail(i18n._("No file was attached."))
    if not upload.filename.lower().endswith(".pdf"):
        return fail(i18n._("PDF 파일만 올릴 수 있습니다."))
    upload.save(path)
    if os.path.getsize(path) > ATTACHMENT_MAX_MB * 1024 * 1024:
        os.unlink(path)
        return fail(i18n._("PDF는 {n}MB까지 올릴 수 있습니다.").format(n=ATTACHMENT_MAX_MB), 413)
    try:
        text = _pdf_text(path)
    except Exception as exc:
        os.unlink(path)
        return fail(i18n._("PDF를 읽지 못했습니다: {err}").format(err=str(exc)[:120]))
    name = os.path.basename(upload.filename)[:160]
    db.update_session(session_id, attachment_name=name, attachment_text=text)
    return jsonify({"name": name, "chars": len(text),
                    "url": url_for("api_attachment_file", session_id=session_id)})


@app.route("/api/sessions/<int:session_id>/attachment")
@auth.login_required
def api_attachment_file(session_id):
    if not owned(session_id):
        abort(404)
    if not plans.can(auth.current_user(), "slides"):
        abort(403)
    path = os.path.join(AUDIO_DIR, str(session_id), "slides.pdf")
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="application/pdf", conditional=True, max_age=3600)


# ----------------------------------------------------------- audio & translate

def _md_to_docx_bytes(title, sections):
    """Build a .docx from (heading, markdown) sections. Handles #/##/### heads,
    bullet lists, and **bold**. Korean and any script work (it is just XML)."""
    import io
    import re as _re
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading(title or "Heardit note", level=0)
    for section_title, md in sections:
        if not (md or "").strip():
            continue
        doc.add_heading(section_title, level=1)
        for raw in md.replace("\r", "").split("\n"):
            line = raw.rstrip()
            if not line.strip():
                continue
            m = _re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                doc.add_heading(m.group(2), level=min(4, len(m.group(1)) + 1))
                continue
            bullet = _re.match(r"^(\s*)[-*]\s+(.*)$", line)
            text, style = (bullet.group(2), "List Bullet") if bullet else (line.strip(), None)
            para = doc.add_paragraph(style=style)
            # **bold** runs
            for i, part in enumerate(_re.split(r"\*\*(.+?)\*\*", text)):
                run = para.add_run(part)
                if i % 2 == 1:
                    run.bold = True
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _session_audio_files(session_id):
    """Distinct recording files for a session, in transcript order."""
    seen, files = set(), []
    for seg in db.get_segments(session_id):
        name = seg.get("audio_path")
        if name and name not in seen:
            seen.add(name)
            path = os.path.join(AUDIO_DIR, str(session_id), name)
            if os.path.isfile(path):
                files.append(path)
    return files


@app.route("/api/sessions/<int:session_id>/note.docx")
@auth.login_required
def api_note_docx(session_id):
    """The notes as an editable Word document."""
    session = owned(session_id)
    if not session:
        abort(404)
    if (denied := require("edit_notes")):
        return denied
    sections = [(i18n._("요약"), session.get("summary") or "")]
    if session.get("exam_sheet"):
        sections.append((i18n._("시험 요약"), session["exam_sheet"]))
    sections.append((i18n._("스크립트"), db.get_transcript(session_id, with_speakers=True)))
    data = _md_to_docx_bytes(session.get("title"), sections)
    from flask import Response
    from urllib.parse import quote
    safe = re.sub(r"[^\w가-힣 -]", "", session.get("title") or "note").strip() or "note"
    # Header values are latin-1; a Korean name must be percent-encoded, with an
    # ASCII fallback for older clients.
    disp = f"attachment; filename=note.docx; filename*=UTF-8''{quote(safe + '.docx')}"
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": disp})


@app.route("/api/sessions/<int:session_id>/recording")
@auth.login_required
def api_recording(session_id):
    """Download the whole recording as one mp3. Live sessions are many clips,
    so they are concatenated; link/upload sessions are already one file."""
    session = owned(session_id)
    if not session:
        abort(404)
    if not plans.can(auth.current_user(), "replay"):
        return require("replay")
    files = _session_audio_files(session_id)
    if not files:
        return fail(i18n._("이 기록에는 저장된 녹음이 없습니다."), 404)

    safe = re.sub(r"[^\w가-힣 -]", "", session.get("title") or "recording").strip() or "recording"
    if len(files) == 1 and files[0].lower().endswith(".mp3"):
        return send_file(files[0], as_attachment=True, download_name=f"{safe}.mp3")

    workdir = media.workspace()
    out = os.path.join(workdir, "recording.mp3")
    try:
        listfile = os.path.join(workdir, "list.txt")
        with open(listfile, "w") as fh:
            for path in files:
                fh.write(f"file '{path}'\n")
        import subprocess
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
             "-acodec", "libmp3lame", "-b:a", "64k", out],
            capture_output=True, timeout=600, check=True,
        )
        return send_file(out, as_attachment=True, download_name=f"{safe}.mp3",
                         max_age=0, etag=False)
    except Exception as exc:
        app.logger.error("recording export failed: %s", traceback.format_exc())
        return fail(str(exc), 502)
    finally:
        # send_file streams before this runs; schedule cleanup after response.
        @after_this_request
        def _cleanup(response):
            shutil.rmtree(workdir, ignore_errors=True)
            return response


@app.route("/api/sessions/<int:session_id>/audio/<path:name>")
@auth.login_required
def api_audio(session_id, name):
    """Stream a kept recording. Range requests let the player seek."""
    if not owned(session_id):
        abort(404)
    if not plans.can(auth.current_user(), "replay"):
        abort(403)
    # Names are generated by us; anything with a separator is not ours.
    if "/" in name or "\\" in name or name.startswith("."):
        abort(404)
    path = os.path.join(AUDIO_DIR, str(session_id), name)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, conditional=True, max_age=3600)


@app.route("/api/sessions/<int:session_id>/translate", methods=["POST"])
@auth.login_required
def api_translate(session_id):
    """Translate transcript lines into a target language, caching per line."""
    if not owned(session_id):
        return fail(i18n._("No such session."), 404)
    if (denied := require("translation")):
        return denied
    body = request.get_json(silent=True) or {}
    target = (body.get("target") or "").lower()[:2]
    if target not in ai.LANGUAGE_NAMES:
        return fail(i18n._("Unsupported language."))
    try:
        ids = [int(i) for i in (body.get("ids") or [])][:60]
    except (TypeError, ValueError):
        return fail(i18n._("Bad segment ids."))
    if not ids:
        return jsonify({"translations": {}})

    result = db.cached_translations(session_id, ids, target)
    todo = db.segments_needing_translation(session_id, ids, target)
    if todo:
        try:
            fresh = ai.translate_segments({s["id"]: s["text"] for s in todo}, target)
        except Exception as exc:
            app.logger.error("translate failed: %s", traceback.format_exc())
            return fail(str(exc), 502)
        db.save_translations(session_id, target, fresh)
        result.update(fresh)
    return jsonify({"translations": {str(k): v for k, v in result.items()}})


# --------------------------------------------------------------- retention

AUDIO_RETENTION_DAYS = int(os.getenv("AUDIO_RETENTION_DAYS", "90"))


def _retention_sweep():
    """Daily housekeeping to bound storage:
    - free-tier notes older than their window are deleted whole;
    - for everyone else, recordings older than AUDIO_RETENTION_DAYS are
      removed but the notes and transcript stay (replay just stops)."""
    free_days = plans.FEATURES["free"]["retention_days"]
    while True:
        try:
            if free_days:
                for row in db.expired_sessions(free_days, exempt_emails=plans.OWNER_EMAILS):
                    if plans.plan_key(row) != "free":
                        continue
                    db.delete_session(row["id"])
                    drop_audio(row["id"])
            if AUDIO_RETENTION_DAYS:
                for row in db.sessions_with_old_audio(AUDIO_RETENTION_DAYS):
                    drop_audio(row["id"])
                    db.clear_segment_audio(row["id"])
        except Exception:
            app.logger.error("retention sweep failed: %s", traceback.format_exc())
        time.sleep(24 * 3600)


if os.getenv("RETENTION_SWEEP", "1") == "1":
    threading.Thread(target=_retention_sweep, daemon=True).start()


# ------------------------------------------------------------- contact

INQUIRY_TOPICS = ("billing", "bug", "feature", "other")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """Support inbox. Messages land in the database; the owner answers from
    /admin and the reply shows on the sender's account page."""
    user = auth.current_user()
    if request.method == "POST":
        email = (request.form.get("email") or (user or {}).get("email") or "").strip()[:200]
        topic = request.form.get("topic") or "other"
        message = (request.form.get("message") or "").strip()[:5000]
        if "@" not in email or not message:
            return render_template("contact.html", topics=INQUIRY_TOPICS, sent=False,
                                   error=i18n._("이메일과 내용을 채워주세요."), form=request.form), 400
        db.add_inquiry(email, topic if topic in INQUIRY_TOPICS else "other", message,
                       name=(user or {}).get("name"), user_id=(user or {}).get("id"))
        return render_template("contact.html", topics=INQUIRY_TOPICS, sent=True)
    return render_template("contact.html", topics=INQUIRY_TOPICS, sent=False, form={})


# --------------------------------------------------------------- admin

@app.route("/admin")
@auth.owner_required
def admin():
    since = plans.month_start()
    tab = request.args.get("tab", "overview")
    return render_template(
        "admin.html", tab=tab, stats=db.admin_stats(since),
        users=db.admin_users(request.args.get("q")) if tab == "users" else [],
        orders=db.admin_orders() if tab == "orders" else [],
        promos=db.list_promos() if tab == "promos" else [],
        inquiries=db.list_inquiries(request.args.get("status") or None) if tab == "inquiries" else [],
        plans=plans.PLANS, topups=plans.TOPUPS, budget=plans.budget_status(),
        q=request.args.get("q", ""), status=request.args.get("status", ""),
        stripe_ready=billing.stripe_ready(),
    )


@app.route("/api/admin/users/<int:user_id>", methods=["PATCH"])
@auth.owner_required
def api_admin_user(user_id):
    """Set a plan (with optional expiry) or add credit by hand."""
    body = request.get_json(silent=True) or {}
    if not db.get_user(user_id):
        return fail("No such user.", 404)
    if "plan" in body:
        if body["plan"] not in plans.PLANS:
            return fail("Unknown plan.")
        until = None
        if body["plan"] != "free" and body.get("months"):
            from datetime import datetime, timedelta, timezone
            until = (datetime.now(timezone.utc) + timedelta(days=30 * int(body["months"]))).isoformat(timespec="seconds")
        db.set_plan(user_id, body["plan"], until=until)
        db.add_order(user_id, "comp", body["plan"], amount=0, provider="comp",
                     provider_ref=f"admin:{user_id}:{int(time.time())}")
    if body.get("credit_minutes"):
        db.add_bonus_seconds(user_id, int(body["credit_minutes"]) * 60)
    return jsonify({"ok": True})


@app.route("/api/admin/promos", methods=["POST"])
@auth.owner_required
def api_admin_promo():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip().upper()
    kind = body.get("kind")
    if not code or kind not in ("percent", "fixed", "comp"):
        return fail("Code and kind are required.")
    if db.get_promo(code):
        return fail("That code already exists.")
    try:
        db.add_promo(
            code, kind, value=int(body.get("value") or 0),
            plan=body.get("plan") if body.get("plan") in plans.PLANS else None,
            months=int(body.get("months") or 1), max_uses=int(body["max_uses"]) if body.get("max_uses") else None,
            expires_at=f"{body['expires']}T23:59:59+00:00" if body.get("expires") else None,
            note=(body.get("note") or "")[:200],
        )
    except Exception as exc:
        return fail(str(exc))
    return jsonify({"ok": True})


@app.route("/api/admin/promos/<code>", methods=["DELETE"])
@auth.owner_required
def api_admin_promo_delete(code):
    db.delete_promo(code)
    return jsonify({"ok": True})


@app.route("/api/admin/inquiries/<int:inquiry_id>", methods=["PATCH"])
@auth.owner_required
def api_admin_inquiry(inquiry_id):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status and status not in ("open", "answered", "closed"):
        return fail("Bad status.")
    reply = body.get("reply")
    if reply is not None and not status:
        status = "answered"
    db.update_inquiry(inquiry_id, reply=reply, status=status)
    return jsonify({"ok": True})


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/robots.txt")
def robots():
    return ("User-agent: *\nDisallow: /api/\nDisallow: /session/\nDisallow: /folder/\n"
            "Disallow: /review\nDisallow: /account\nAllow: /\n"), 200, {"Content-Type": "text/plain"}


@app.route("/api/account/delete", methods=["POST"])
@auth.login_required
def api_delete_account():
    """Erase the account and everything under it. Irreversible by design."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "삭제":
        return fail(i18n._("확인 문구가 일치하지 않습니다."))
    user = auth.current_user()
    # Recordings on disk go with the rows.
    for session in db.list_sessions(user["id"]):
        drop_audio(session["id"])
    db.delete_user(user["id"])
    from flask import session as flask_session
    flask_session.clear()
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_):
    limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return fail(i18n._("파일이 너무 큽니다. {n}MB 이하만 올릴 수 있습니다.").format(n=limit), 413)


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "No such endpoint."}), 404
    return render_template("missing.html", session_id=None), 404


if __name__ == "__main__":
    # Local development entry point. In production the app is served by
    # gunicorn (see Procfile), never by this reloader.
    # 5001 because macOS runs its AirPlay Receiver on 5000.
    debug = os.getenv("FLASK_ENV") != "production" and os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug, port=int(os.getenv("PORT", 5001)))
