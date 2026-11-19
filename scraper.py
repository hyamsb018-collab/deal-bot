#!/usr/bin/env python3
"""Discount monitor: checks every configured retailer page and alerts your phone when a
padel racket or pair of football boots hits the discount threshold (default 80% off RRP).

Usage
    python scraper.py                  normal run (what GitHub Actions does)
    python scraper.py --dry-run        log matches but send nothing and save nothing
    python scraper.py --threshold 40   temporarily use a lower threshold (for testing)
    python scraper.py --test-alert     just send a test notification to your phone
    python scraper.py --only "Padel Market" -v   check one site with debug output
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import notifier
from adapters import ADAPTERS
from adapters.context import RunContext

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "seen_deals.json"


# --------------------------------------------------------------------- state file
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            print("! seen_deals.json was unreadable – starting fresh")
            data = {}
    else:
        data = {}
    data.setdefault("deals", {})
    data.setdefault("health", {})
    return data


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def prune_state(state: dict, days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    state["deals"] = {k: v for k, v in state["deals"].items() if v.get("alerted_at", "") >= cutoff}


# ------------------------------------------------------------------- filtering
def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text) is not None


def matches_category(p, cat: dict, require_keywords: bool) -> tuple[bool, str]:
    text = f"{p.title} {p.brand}".lower()
    if any(_has_word(text, w) for w in cat.get("exclude", [])):
        return False, "excluded word"
    brands = cat.get("brands", [])
    if brands and not any(_has_word(text, b) for b in brands):
        return False, "brand not in list"
    if require_keywords and cat.get("require_any") and not any(_has_word(text, w) for w in cat["require_any"]):
        return False, "missing keyword"
    if p.rrp < float(cat.get("min_rrp", 0)):
        return False, "RRP too low"
    return True, ""


# ---------------------------------------------------------------------- health
def update_health(state, cfg, site_name: str, ok: bool, dry_run: bool) -> None:
    """Warn (at most every 3 days) if a site has returned nothing for a while – usually a block or a dead URL."""
    today = date.today()
    h = state["health"].setdefault(site_name, {"first_checked": today.isoformat()})
    if ok:
        h["last_ok"] = today.isoformat()
        h.pop("last_warned", None)
        return
    if not cfg.get("health_alerts", True) or dry_run:
        return
    since = date.fromisoformat(h.get("last_ok") or h.get("first_checked"))
    warned = h.get("last_warned")
    if (today - since).days >= int(cfg.get("health_alert_after_days", 1)) and (
        not warned or (today - date.fromisoformat(warned)).days >= 3
    ):
        if notifier.send(
            f"⚠️ Deal bot: {site_name} isn't returning products",
            f"No products have been parsed from {site_name} since {since.isoformat()}. "
            "The site may be blocking the bot or the URL in config.json may have changed. "
            "Check the latest run in the GitHub Actions tab.",
            priority=2,
            tags=["warning"],
        ):
            h["last_warned"] = today.isoformat()


# ------------------------------------------------------------------------ main
def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--only", help="only check the site with this exact name")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    dry_run = args.dry_run or env_flag("DRY_RUN")
    if args.test_alert or env_flag("TEST_ALERT"):
        ok = notifier.send(
            "✅ Deal bot test",
            "If you can read this on your phone, alerts are working. Tap to open ntfy.",
            url="https://ntfy.sh",
            priority=4,
            tags=["white_check_mark"],
        )
        print("Test alert sent." if ok else "Test alert FAILED – check your secrets.")
        return 0 if ok else 1

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    threshold = args.threshold or float(os.getenv("THRESHOLD_OVERRIDE") or 0) or float(cfg.get("threshold_percent", 80))
    max_plausible = float(cfg.get("max_plausible_discount", 97))
    realert_step = float(cfg.get("realert_if_discount_improves_by", 5))
    deadline = time.time() + 60 * float(cfg.get("max_runtime_minutes", 11))

    state = load_state()
    prune_state(state, int(cfg.get("remember_deals_for_days", 45)))
    ctx = RunContext(cfg, verbose=args.verbose or env_flag("VERBOSE"))

    print(f"Deal bot – threshold {threshold:.0f}% – {'DRY RUN – ' if dry_run else ''}"
          f"alerts via: {', '.join(notifier.channels()) or 'NONE CONFIGURED'}\n")

    summary_rows = []
    alerts_sent = 0

    for site in cfg["sites"]:
        name = site["name"]
        if args.only and args.only.lower() != name.lower():
            continue
        if not site.get("enabled", True):
            continue
        searches = [s for s in site.get("searches", []) if s.get("url", "").startswith("http")]
        if not searches:
            summary_rows.append((name, "–", "–", "skipped (no URL pasted yet)"))
            continue
        if time.time() > deadline:
            summary_rows.append((name, "–", "–", "skipped (out of time this run)"))
            continue

        adapter = ADAPTERS.get(site.get("adapter", "browser"))
        if adapter is None:
            summary_rows.append((name, "–", "–", f"unknown adapter '{site.get('adapter')}'"))
            continue

        print(f"▶ {name}")
        site_total, site_best, errors = 0, None, []
        for search in searches:
            cat_key = search["category"]
            cat = cfg["categories"][cat_key]
            require_kw = search.get("require_keywords", site.get("adapter") == "ebay")
            try:
                products = adapter(search, site, ctx)
            except Exception as e:  # one broken site must never stop the others
                msg = str(e).splitlines()[0][:150]
                print(f"   ✗ {cat['label']}: {msg}")
                errors.append(msg)
                continue

            kept = []
            for p in products:
                p.category = cat_key
                ok, why = matches_category(p, cat, require_kw)
                if ok:
                    kept.append(p)
                else:
                    ctx.debug(f"filtered ({why}): {p.title}")
            kept.sort(key=lambda p: p.discount, reverse=True)
            site_total += len(kept)
            print(f"   {cat['label']}: {len(products)} discounted listings parsed, {len(kept)} match your filters")
            for p in kept[:3]:
                print(f"      {p.discount:5.1f}%  {p.currency}{p.price:.2f} (RRP {p.currency}{p.rrp:.2f})  {p.title[:70]}")
            if kept and (site_best is None or kept[0].discount > site_best.discount):
                site_best = kept[0]

            for p in kept:
                if p.discount < threshold:
                    break
                if p.discount > max_plausible:
                    print(f"      ? ignoring implausible {p.discount}% (probably a parsing error): {p.url}")
                    continue
                prev = state["deals"].get(p.key)
                if prev and p.discount < float(prev.get("discount", 0)) + realert_step:
                    continue                      # already told you about this one
                print(f"   🔥 DEAL {p.discount:.0f}% – {p.title} – {p.url}")
                if dry_run:
                    continue
                if notifier.send_deal(p, cat["label"]):
                    alerts_sent += 1
                    state["deals"][p.key] = {
                        "title": p.title,
                        "retailer": p.retailer,
                        "url": p.url,
                        "price": p.price,
                        "rrp": p.rrp,
                        "discount": p.discount,
                        "alerted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    save_state(state)              # save immediately, in case the run is cut short

        update_health(state, cfg, name, ok=site_total > 0, dry_run=dry_run)
        status = "error: " + errors[0] if errors and not site_total else ("ok" if site_total else "0 products – check URL/selectors")
        best = f"{site_best.discount:.0f}%" if site_best else "–"
        summary_rows.append((name, str(site_total), best, status))
        ctx.polite_pause()

    ctx.close()
    if not dry_run:
        save_state(state)

    print(f"\nDone. {alerts_sent} alert(s) sent.")
    write_github_summary(summary_rows, threshold, alerts_sent, dry_run)
    return 0


def write_github_summary(rows, threshold, alerts, dry_run) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"## Deal bot run – threshold {threshold:.0f}%{' (dry run)' if dry_run else ''}",
        f"**Alerts sent:** {alerts}",
        "",
        "| Site | Matching discounted items | Best discount seen | Status |",
        "|---|---|---|---|",
    ]
    lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
