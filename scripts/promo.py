#!/usr/bin/env python3
"""Manage promo codes from the command line.

  scripts/promo.py add WELCOME20 --percent 20 --max-uses 200 --expires 2026-12-31 --note "launch"
  scripts/promo.py add FRIEND5000 --fixed 5000 --max-uses 50
  scripts/promo.py add CHLOE-DEV --comp pro --months 120 --note "owner"
  scripts/promo.py list
  scripts/promo.py rm WELCOME20

Discounts are capped by plans.PROMO_FLOOR at checkout, so a 90% code still
never sells below cost. Comp codes grant a plan for free and are meant for
the owner and reviewers: keep max-uses small.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import db  # noqa: E402
import plans  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("code")
    g = add.add_mutually_exclusive_group(required=True)
    g.add_argument("--percent", type=int)
    g.add_argument("--fixed", type=int, help="KRW off")
    g.add_argument("--comp", choices=[k for k in plans.PLANS if k != "free"], help="grant this plan free")
    add.add_argument("--months", type=int, default=1, help="comp only: how many months")
    add.add_argument("--max-uses", type=int)
    add.add_argument("--expires", help="YYYY-MM-DD")
    add.add_argument("--note")
    sub.add_parser("list")
    rm = sub.add_parser("rm")
    rm.add_argument("code")
    args = ap.parse_args()

    db.init()
    if args.cmd == "add":
        if args.percent is not None:
            if not 1 <= args.percent <= 100:
                sys.exit("percent must be 1-100")
            kind, value = "percent", args.percent
            if args.percent > (1 - plans.PROMO_FLOOR) * 100:
                print(f"note: discounts are floored at {int((1 - plans.PROMO_FLOOR) * 100)}% off at checkout")
        elif args.fixed is not None:
            kind, value = "fixed", args.fixed
        else:
            kind, value = "comp", 0
        expires = f"{args.expires}T23:59:59+00:00" if args.expires else None
        try:
            db.add_promo(args.code, kind, value=value, plan=args.comp, months=args.months,
                         max_uses=args.max_uses, expires_at=expires, note=args.note)
        except Exception as exc:
            sys.exit(f"could not add: {exc}")
        print(f"added {args.code.upper()} ({kind})")
    elif args.cmd == "rm":
        db.delete_promo(args.code)
        print(f"removed {args.code.upper()}")
    else:
        rows = db.list_promos()
        if not rows:
            print("no codes")
        for r in rows:
            what = {"percent": f"{r['value']}% off", "fixed": f"{r['value']:,} KRW off",
                    "comp": f"free {r['plan']} x{r['months']} mo"}[r["kind"]]
            uses = f"{r['uses']}/{r['max_uses'] if r['max_uses'] is not None else '∞'}"
            print(f"{r['code']:<16} {what:<22} uses {uses:<8} expires {r['expires_at'] or '-':<26} {r['note'] or ''}")


if __name__ == "__main__":
    main()
