"""Checkout, promo codes and the ledger.

Stripe Checkout is the payment provider when STRIPE_SECRET_KEY and
STRIPE_WEBHOOK_SECRET are set: plans are monthly subscriptions, credit packs
are one-time payments, and the webhook is the only thing that grants
anything — a returning browser is never trusted. Without Stripe keys a local
dev build can "simulate" a purchase so the whole flow can be exercised; a
production build without keys tells the visitor payments are not open yet.

Promo codes live in the database (see scripts/promo.py):
  percent / fixed  — a discount, floored so no sale drops below cost;
  comp             — grants a plan outright for N months, no payment. This is
                     how the owner's personal code and reviewer codes work.
"""

import os
from datetime import datetime, timedelta, timezone

import db
import plans

STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")


class BillingError(Exception):
    pass


def stripe_ready():
    return bool(STRIPE_SECRET and STRIPE_WEBHOOK_SECRET)


def _stripe():
    import stripe
    stripe.api_key = STRIPE_SECRET
    return stripe


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------- catalogue

def item(key):
    """(kind, spec) for a plan or top-up key."""
    key = (key or "").strip()
    if key in plans.PLANS and plans.PLANS[key]["price"] > 0:
        return "subscription", {"key": key, **plans.PLANS[key]}
    if key in plans.TOPUPS:
        return "topup", {"key": key, **plans.TOPUPS[key]}
    raise BillingError("Unknown item.")


def topup_price(key, user):
    """Pro subscribers get 10% off credit packs."""
    price = plans.TOPUPS[key]["price"]
    if plans.plan_key(user) == "pro":
        price = price * 90 // 100
    return price


def seed_promos():
    """Create codes listed in PROMO_SEED that do not exist yet.

    Lets a hosted deploy without a shell (Render's free tier) still have the
    owner's code and a launch discount. Format, comma-separated:
      CODE:percent:20:200      (20% off, 200 uses)
      CODE:fixed:3000:50       (3,000 KRW off, 50 uses)
      CODE:comp:pro:120:3      (free Pro for 120 months, 3 uses)
    """
    spec = os.getenv("PROMO_SEED", "").strip()
    if not spec:
        return []
    made = []
    for entry in spec.split(","):
        parts = [p.strip() for p in entry.split(":") if p.strip()]
        if len(parts) < 3 or db.get_promo(parts[0]):
            continue
        code, kind = parts[0], parts[1]
        try:
            if kind == "percent":
                db.add_promo(code, "percent", value=int(parts[2]), max_uses=int(parts[3]) if len(parts) > 3 else None, note="seed")
            elif kind == "fixed":
                db.add_promo(code, "fixed", value=int(parts[2]), max_uses=int(parts[3]) if len(parts) > 3 else None, note="seed")
            elif kind == "comp":
                db.add_promo(code, "comp", plan=parts[2], months=int(parts[3]) if len(parts) > 3 else 12,
                             max_uses=int(parts[4]) if len(parts) > 4 else 1, note="seed")
            else:
                continue
            made.append(code.upper())
        except Exception:
            continue
    return made


# ----------------------------------------------------------- promo codes

def validate_promo(code, user, purchase_kind=None):
    """The promo row if `code` can be used by this user, else raise."""
    import i18n
    promo = db.get_promo(code)
    if not promo:
        raise BillingError(i18n._("없는 코드입니다."))
    if promo["expires_at"] and promo["expires_at"] < _iso(_now()):
        raise BillingError(i18n._("기간이 지난 코드입니다."))
    if promo["max_uses"] is not None and promo["uses"] >= promo["max_uses"]:
        raise BillingError(i18n._("사용 가능 횟수를 모두 쓴 코드입니다."))
    if db.promo_used_by(promo["code"], user["id"]):
        raise BillingError(i18n._("이미 사용한 코드입니다."))
    if purchase_kind and promo["kind"] == "comp":
        raise BillingError(i18n._("이 코드는 결제창이 아니라 계정 페이지에서 등록하는 코드입니다."))
    return promo


def quote(key, user, code=None):
    """What a purchase would cost: list price, discount, final, promo used."""
    kind, spec = item(key)
    list_price = spec["price"] if kind == "subscription" else topup_price(key, user)
    promo = validate_promo(code, user, purchase_kind=kind) if code else None
    final, discount = plans.discounted(list_price, promo)
    return {
        "kind": kind, "item": spec, "list_price": list_price,
        "discount": discount, "amount": final,
        "promo": promo["code"] if promo else None,
        "promo_note": _promo_label(promo, kind) if promo else None,
    }


def _promo_label(promo, kind):
    import i18n
    if promo["kind"] == "percent":
        base = i18n._("{n}% 할인").format(n=promo["value"])
    else:
        base = i18n._("{n}원 할인").format(n=f"{promo['value']:,}")
    if kind == "subscription":
        return base + " · " + i18n._("첫 달에 적용")
    return base


def redeem_comp(code, user):
    """Apply a comp code: grant its plan for its months, log a zero order."""
    promo = validate_promo(code, user)
    if promo["kind"] != "comp":
        import i18n
        raise BillingError(i18n._("이 코드는 결제할 때 입력하는 할인 코드입니다."))
    plan = promo["plan"] if promo["plan"] in plans.PLANS else "pro"
    # Extend an existing comp rather than overwrite it.
    current_until = user.get("plan_until")
    start = _now()
    if plans.plan_key(user) == plan and current_until and current_until > _iso(start):
        start = datetime.fromisoformat(current_until)
    until = start + timedelta(days=30 * max(1, promo["months"]))
    db.set_plan(user["id"], plan, until=_iso(until))
    db.redeem_promo(promo["code"], user["id"])
    db.add_order(user["id"], "comp", plan, amount=0, list_price=plans.PLANS[plan]["price"] * promo["months"],
                 discount=plans.PLANS[plan]["price"] * promo["months"], promo_code=promo["code"],
                 provider="comp", provider_ref=f"comp:{promo['code']}:{user['id']}:{_iso(start)}")
    return plan, _iso(until)


# --------------------------------------------------------------- fulfil

def fulfil(user, q, provider, provider_ref):
    """Grant what was paid for. Idempotent on provider_ref."""
    if provider_ref and db.order_exists(provider_ref):
        return
    spec = q["item"]
    if q["kind"] == "subscription":
        # Stripe keeps the subscription alive; plan_until is only a safety net
        # so a missed cancellation webhook still lapses the plan eventually.
        db.set_plan(user["id"], spec["key"], until=_iso(_now() + timedelta(days=35)))
        seconds = 0
    else:
        seconds = spec["minutes"] * 60
        db.add_bonus_seconds(user["id"], seconds)
    if q.get("promo"):
        db.redeem_promo(q["promo"], user["id"])
    db.add_order(user["id"], q["kind"], spec["key"], amount=q["amount"], list_price=q["list_price"],
                 discount=q["discount"], seconds=seconds, promo_code=q.get("promo"),
                 provider=provider, provider_ref=provider_ref)


def renew(user, invoice_id, amount):
    """A subscription invoice was paid: extend the safety-net expiry."""
    if db.order_exists(invoice_id):
        return
    key = plans.plan_key(user)
    if key == "free":
        return
    db.set_plan(user["id"], key, until=_iso(_now() + timedelta(days=35)))
    db.add_order(user["id"], "renewal", key, amount=amount, list_price=plans.PLANS[key]["price"],
                 provider="stripe", provider_ref=invoice_id)


def cancel(user):
    db.set_plan(user["id"], "free", until=None)
    db.set_stripe_ids(user["id"], subscription_id=None)


# ---------------------------------------------------------------- Stripe

def checkout_url(user, q, success_url, cancel_url):
    """Create a Stripe Checkout session and return its URL."""
    stripe = _stripe()
    spec = q["item"]
    name = spec["name"]
    if q["kind"] == "subscription":
        line = {
            "price_data": {
                "currency": "krw",
                "unit_amount": q["list_price"],
                "recurring": {"interval": "month"},
                "product_data": {"name": f"TranscriptoAI {name}"},
            },
            "quantity": 1,
        }
        mode = "subscription"
    else:
        line = {
            "price_data": {
                "currency": "krw",
                "unit_amount": q["list_price"],
                "product_data": {"name": f"TranscriptoAI {name}"},
            },
            "quantity": 1,
        }
        mode = "payment"

    params = {
        "mode": mode,
        "line_items": [line],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(user["id"]),
        "metadata": {"user_id": str(user["id"]), "item": spec["key"], "promo": q.get("promo") or "",
                     "list_price": str(q["list_price"]), "discount": str(q["discount"]),
                     "amount": str(q["amount"])},
    }
    if user.get("stripe_customer_id"):
        params["customer"] = user["stripe_customer_id"]
    else:
        params["customer_email"] = user["email"]
    if q["discount"]:
        # A one-off coupon: first month for subscriptions, the whole payment
        # for credit packs. The floor was already applied in the quote.
        coupon = stripe.Coupon.create(
            amount_off=q["discount"], currency="krw", duration="once",
            name=f"{q['promo']} ({q['discount']:,} KRW off)",
        )
        params["discounts"] = [{"coupon": coupon.id}]
    if mode == "subscription":
        params["subscription_data"] = {"metadata": {"user_id": str(user["id"]), "item": spec["key"]}}

    session = stripe.checkout.Session.create(**params)
    return session.url


def portal_url(user, return_url):
    stripe = _stripe()
    if not user.get("stripe_customer_id"):
        raise BillingError("No billing account yet.")
    return stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"], return_url=return_url
    ).url


def handle_webhook(payload, signature):
    """Verify and apply one Stripe event. Returns a short description."""
    stripe = _stripe()
    event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    kind = event["type"]
    obj = event["data"]["object"]

    if kind == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        user = db.get_user(int(meta.get("user_id") or obj.get("client_reference_id") or 0))
        if not user:
            return "no such user"
        if obj.get("customer"):
            db.set_stripe_ids(user["id"], customer_id=obj["customer"],
                              subscription_id=obj.get("subscription") or user.get("stripe_subscription_id"))
        q = quote_from_metadata(meta, user)
        fulfil(user, q, "stripe", obj["id"])
        return f"fulfilled {q['item']['key']}"

    if kind == "invoice.paid":
        # First invoice is covered by checkout.session.completed; renewals
        # arrive here alone.
        if obj.get("billing_reason") == "subscription_create":
            return "initial invoice"
        user = db.user_by_stripe_customer(obj.get("customer"))
        if user:
            renew(user, obj["id"], int(obj.get("amount_paid") or 0))
            return "renewed"
        return "no such customer"

    if kind == "customer.subscription.deleted":
        user = db.user_by_stripe_customer(obj.get("customer"))
        if user:
            cancel(user)
            return "cancelled"
        return "no such customer"

    return "ignored"


def quote_from_metadata(meta, user):
    """Rebuild the quote the checkout was created with, from its metadata,
    so fulfilment does not re-validate a code that has since expired."""
    kind, spec = item(meta.get("item"))
    return {
        "kind": kind, "item": spec,
        "list_price": int(meta.get("list_price") or spec["price"]),
        "discount": int(meta.get("discount") or 0),
        "amount": int(meta.get("amount") or spec["price"]),
        "promo": meta.get("promo") or None,
    }
