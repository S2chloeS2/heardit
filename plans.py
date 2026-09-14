"""Plans, credits and monthly allowances.

Transcription is the one thing that costs real money per minute, so that is
what a plan meters. Everything else (notes, keywords, chat, translation)
rides along.

Two kinds of allowance stack:
  - the plan's monthly minutes, which reset on the 1st (UTC);
  - top-up credits, bought once, spent only after the month's minutes are
    gone, and never expiring.

Prices are KRW. `COST_PER_HOUR` is what an hour of audio costs us end to end
(AssemblyAI + gpt-4o notes + the small calls) and is the floor promo codes
must never cut through.
"""

import os
from datetime import datetime, timezone

import db

COST_PER_HOUR = 1000  # KRW, conservative

PLANS = {
    "free": {
        "name": "무료",
        "minutes": 2 * 60,
        "price": 0,
        "blurb": "써보기",
        "features": ["모든 입력 방식", "시험 노트 · 핵심 용어 · AI 챗", "문장 눌러 다시 듣기",
                     "기록 30일 보관"],
    },
    "student": {
        "name": "스튜던트",
        "minutes": 20 * 60,
        "price": 12900,
        "blurb": "매일 수업 듣는 학생",
        "features": ["무료의 모든 기능", "기록 무제한 보관", "폴더로 묶어 한꺼번에 질문",
                     "강의 자료 PDF 나란히 보기", "실시간 번역"],
    },
    "pro": {
        "name": "프로",
        "minutes": 45 * 60,
        "price": 24900,
        "blurb": "회의가 잦은 팀과 연구자",
        "features": ["스튜던트의 모든 기능", "회의 화자 분리", "긴 파일 우선 처리",
                     "추가 크레딧 20% 할인"],
    },
}

ORDER = ["free", "student", "pro"]

# One-time credit packs. Never expire; used after the month's minutes.
TOPUPS = {
    "topup5": {"name": "5시간 크레딧", "minutes": 5 * 60, "price": 5900},
    "topup15": {"name": "15시간 크레딧", "minutes": 15 * 60, "price": 14900},
}

# Legacy plan keys from before the tiers were renamed.
_ALIASES = {"standard": "student"}

# A discount code may not push a price below this share of the list price.
# With list prices at roughly 2x realistic cost, 55% keeps every sale in the
# black even for someone who uses their whole allowance.
PROMO_FLOOR = 0.55


def plan_key(user):
    key = (user or {}).get("plan") or "free"
    key = _ALIASES.get(key, key)
    if key not in PLANS:
        return "free"
    # A comped or cancelled paid plan lapses at plan_until.
    until = (user or {}).get("plan_until")
    if key != "free" and until and until < _now_iso():
        return "free"
    return key


def plan_of(user):
    return {"key": plan_key(user), **PLANS[plan_key(user)]}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month_start():
    """ISO timestamp for the first instant of this month, UTC — usage before
    this does not count against the current allowance."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def allowance(user_id, user=None):
    user = user or db.get_user(user_id) or {}
    plan = plan_of(user)
    limit_s = plan["minutes"] * 60
    used_s = db.usage_seconds(user_id, since=month_start())
    bonus_s = int(user.get("bonus_seconds") or 0)
    month_left = max(0, limit_s - used_s)
    remaining_s = month_left + bonus_s
    return {
        "plan": plan,
        "limit_s": limit_s,
        "used_s": used_s,
        "month_left_s": month_left,
        "bonus_s": bonus_s,
        "remaining_s": remaining_s,
        "used_pct": min(100, round(used_s / limit_s * 100)) if limit_s else 100,
        "remaining_label": _label(remaining_s),
        "limit_label": _label(limit_s),
        "used_label": _label(used_s),
        "bonus_label": _label(bonus_s),
        "plan_until": user.get("plan_until"),
    }


def check(user_id, needed_s, user=None):
    """(ok, message, allowance). Refuses when the request would overrun."""
    a = allowance(user_id, user)
    if needed_s <= a["remaining_s"]:
        return True, "", a
    import i18n
    if a["remaining_s"] <= 0:
        msg = i18n._("이번 달 {plan} 플랜의 {limit}을 다 썼습니다. 다음 달 1일에 초기화되거나, 플랜을 올리거나 크레딧을 더하면 바로 이어서 쓸 수 있습니다.").format(
            plan=i18n._(a['plan']['name']), limit=a['limit_label'])
    else:
        msg = i18n._("이 오디오는 {need}인데 남은 시간이 {left}입니다. 더 짧은 파일을 올리거나 크레딧을 더해주세요.").format(
            need=_label(needed_s), left=a['remaining_label'])
    return False, msg, a


def record_usage(user_id, session_id, seconds, user=None):
    """Log usage, spending top-up credits for whatever the month cannot cover."""
    if not seconds or seconds <= 0:
        return
    a = allowance(user_id, user)
    from_bonus = max(0, int(round(seconds)) - a["month_left_s"])
    from_bonus = min(from_bonus, a["bonus_s"])
    db.log_usage(user_id, session_id, seconds, bonus_s=from_bonus)
    if from_bonus:
        db.add_bonus_seconds(user_id, -from_bonus)


def _label(seconds):
    """Human duration in the visitor's language: '4시간 59분' / '4h 59m'."""
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    try:
        import i18n
        ko = i18n.current_lang() == "ko"
    except Exception:  # outside a request (scripts, tests)
        ko = True
    if ko:
        if h and m: return f"{h}시간 {m}분"
        if h: return f"{h}시간"
        if m: return f"{m}분"
        return f"{seconds}초" if seconds else "0분"
    if h and m: return f"{h}h {m}m"
    if h: return f"{h}h"
    if m: return f"{m} min"
    return f"{seconds}s" if seconds else "0 min"


def price_label(krw, lang="ko"):
    return f"{krw:,}원" if lang == "ko" else f"₩{krw:,}"


# ------------------------------------------------------------ promo codes

def discounted(price, promo):
    """Final price after a percent/fixed code, never below the floor."""
    if not promo or price <= 0:
        return price, 0
    if promo["kind"] == "percent":
        cut = price * promo["value"] // 100
    elif promo["kind"] == "fixed":
        cut = min(price, promo["value"])
    else:
        return price, 0
    floor = int(price * PROMO_FLOOR)
    final = max(floor, price - cut)
    return final, price - final


# ------------------------------------------------- service-wide budget cap

# Total minutes the whole service may transcribe per month, across all users.
# This is the money lock: however many people sign up, spend cannot exceed
# roughly BUDGET × transcription price. 0 disables the cap.
BUDGET_MINUTES = int(os.getenv("MONTHLY_BUDGET_MINUTES", "0"))


def global_check(needed_s):
    """(ok, message). Refuses when the service-wide monthly budget would overrun."""
    if BUDGET_MINUTES <= 0:
        return True, ""
    used = db.usage_seconds_all(since=month_start())
    if used + needed_s <= BUDGET_MINUTES * 60:
        return True, ""
    import i18n
    return False, i18n._("이번 달 서비스 전체 처리량이 한도에 도달했습니다. 다음 달 1일에 다시 열립니다.")


def budget_status():
    if BUDGET_MINUTES <= 0:
        return None
    used = db.usage_seconds_all(since=month_start())
    return {"limit_s": BUDGET_MINUTES * 60, "used_s": used,
            "used_pct": min(100, round(used / (BUDGET_MINUTES * 60) * 100))}
