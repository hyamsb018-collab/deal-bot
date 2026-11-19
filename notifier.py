"""Phone alerts via ntfy.sh and/or Telegram. Configure with GitHub Secrets (see README):

    NTFY_TOPIC          your secret ntfy topic name, e.g. billy-deals-k39f8x2q7
    NTFY_SERVER         optional, defaults to https://ntfy.sh
    TELEGRAM_BOT_TOKEN  optional, from @BotFather
    TELEGRAM_CHAT_ID    optional, your chat id
"""
from __future__ import annotations

import html
import os

import requests


def channels() -> list[str]:
    out = []
    if os.getenv("NTFY_TOPIC"):
        out.append("ntfy")
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    return out


def _ntfy(title: str, message: str, url: str | None, priority: int, tags: list[str]) -> bool:
    server = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    payload = {
        "topic": os.environ["NTFY_TOPIC"],
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags,
    }
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Open deal", "url": url, "clear": True}]
    headers = {}
    if os.getenv("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    r = requests.post(server, json=payload, headers=headers, timeout=20)
    if r.status_code >= 300:
        print(f"   ! ntfy error {r.status_code}: {r.text[:200]}")
    return r.status_code < 300


def _telegram(title: str, message: str, url: str | None) -> bool:
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    text = f"<b>{html.escape(title)}</b>\n{html.escape(message)}"
    if url:
        text += f'\n\n<a href="{html.escape(url, quote=True)}">👉 Open deal</a>'
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=20,
    )
    if r.status_code >= 300:
        print(f"   ! Telegram error {r.status_code}: {r.text[:200]}")
    return r.status_code < 300


def send(title: str, message: str, url: str | None = None, priority: int = 5, tags: list[str] | None = None) -> bool:
    """Send to every configured channel. Returns True if at least one succeeded."""
    ok = False
    active = channels()
    if not active:
        print("   ! No notification channel configured (set NTFY_TOPIC and/or TELEGRAM_* secrets)")
        return False
    if "ntfy" in active:
        try:
            ok = _ntfy(title, message, url, priority, tags or ["moneybag"]) or ok
        except Exception as e:
            print(f"   ! ntfy failed: {e}")
    if "telegram" in active:
        try:
            ok = _telegram(title, message, url) or ok
        except Exception as e:
            print(f"   ! Telegram failed: {e}")
    return ok


def send_deal(p, category_label: str) -> bool:
    c = p.currency
    title = f"{p.discount:.0f}% OFF – {p.title}"[:200]
    message = (
        f"Retailer: {p.retailer}\n"
        f"Item: {p.title}\n"
        f"Now: {c}{p.price:,.2f}   (RRP {c}{p.rrp:,.2f})\n"
        f"Discount: {p.discount:.0f}% off\n"
        f"Category: {category_label}\n"
        f"{p.url}"
    )
    return send(title, message, p.url, priority=5, tags=["rotating_light", "moneybag"])
