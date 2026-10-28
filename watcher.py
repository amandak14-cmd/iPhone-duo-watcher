#!/usr/bin/env python3
"""
iPhone Duo stock watcher — Apple Store Singapore, in-store pickup.

Checks Apple's pickup-availability endpoint for every Apple Store in
Singapore and sends a Telegram message when a watched model appears
(or disappears) for pickup.

No third-party packages needed (standard library only).

Environment variables
---------------------
TELEGRAM_BOT_TOKEN   required  token from @BotFather
TELEGRAM_CHAT_ID     required  your chat id (run with MODE=test to find it)
MODE                 optional  check (default) | test | discover
RUN_MINUTES          optional  keep checking for this many minutes (default 0 = one check)
INTERVAL_SECONDS     optional  gap between checks while looping (default 30)
PART_NUMBERS         optional  comma list like "ABCD3ZP/A,EFGH3ZP/A" to skip auto-detection
NOTIFY_SOLD_OUT      optional  "true" (default) also tells you when stock disappears
STATE_FILE           optional  where to remember what we've already told you (default state.json)
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config ---

SG_TZ = timezone(timedelta(hours=8))

# What to watch: label -> Apple SG product page for that exact configuration.
WATCH = {
    "iPhone Duo 512GB Star White": "https://www.apple.com/sg/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-star-white",
    "iPhone Duo 512GB Night Sky": "https://www.apple.com/sg/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky",
}

FULFILMENT_URL = "https://www.apple.com/sg/shop/fulfillment-messages"
SEARCH_POSTCODE = "018972"  # Marina Bay Sands; "search nearby" from here covers the whole island
MAX_STORE_DISTANCE_KM = 60  # anything farther isn't in Singapore

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-SG,en;q=0.9",
    "Referer": "https://www.apple.com/sg/shop/buy-iphone/iphone-duo",
}

ERROR_ALERT_AFTER = 5  # consecutive failed checks before we bother you about it

# Part numbers read from Apple SG's iPhone Duo page on 28 Oct 2026.
# Used if auto-detection from the product page doesn't work.
KNOWN_PARTS = {
    "iPhone Duo 512GB Star White": "MK2F4X/A",
    "iPhone Duo 512GB Night Sky": "MK2G4X/A",
}


# --------------------------------------------------------------- helpers ---

def env(name, default=None):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def now_sg():
    return datetime.now(SG_TZ).strftime("%a %d %b, %-I:%M %p")


def log(*args):
    print(f"[{datetime.now(SG_TZ).strftime('%H:%M:%S')}]", *args, flush=True)


def http_get(url, params=None, timeout=20):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# -------------------------------------------------------------- telegram ---

def telegram(text, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = json.load(resp).get("ok")
            if not ok:
                log("Telegram said no:", resp.status)
            return ok
    except urllib.error.HTTPError as e:
        log("Telegram error:", e.code, e.read().decode(errors="replace")[:300])
        return False


def print_chat_ids(token):
    """Helps first-time setup: lists chats that have messaged the bot."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    data = json.loads(http_get(url))
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        log("No chats found. Open your bot in Telegram, send it any message, then run this again.")
    for cid, name in chats.items():
        log(f"Chat id {cid}  ({name})  <- put this in TELEGRAM_CHAT_ID")


# ---------------------------------------------------------- part numbers ---

# Singapore part numbers look like MK2F4X/A (one or two region letters before /A).
PART_RE = re.compile(r'"partNumber"\s*:\s*"([A-Z0-9]{5}[A-Z]{1,2}/A)"')


def page_products(url):
    """Reads Apple's product page and returns {(colour, capacity): part_number}."""
    html = http_get(url)
    found = {}
    starts = [m for m in PART_RE.finditer(html)]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        chunk = html[m.end():end]
        cap = re.search(r'"dimensionCapacity"\s*:\s*"(\w+)"', chunk)
        col = re.search(r'"dimensionColor"\s*:\s*"(\w+)"', chunk)
        if cap and col:
            found[(col.group(1).lower(), cap.group(1).lower())] = m.group(1)
    return found


def get_parts(state, verbose=False):
    manual = env("PART_NUMBERS")
    if manual:
        parts = [p.strip() for p in manual.split(",") if p.strip()]
        return {f"Part {p}": p for p in parts}

    if verbose:
        # Double-check the built-in part numbers against Apple's live page.
        try:
            live = page_products(next(iter(WATCH.values())))
            for (col, cap), pn in sorted(live.items()):
                log(f"  Apple page lists {pn}  {col} {cap}")
            for label, pn in KNOWN_PARTS.items():
                ok = pn in live.values()
                log(f"{label} -> {pn}  {'(matches Apple page)' if ok else '(NOT on Apple page — tell Claude)'}")
        except Exception as e:  # noqa: BLE001
            log(f"Couldn't read Apple's product page to double-check part numbers: {e}")
    return dict(KNOWN_PARTS)


# ----------------------------------------------------------- availability ---

def _in_singapore(store):
    addr = json.dumps(store.get("address") or store.get("retailStore", {}).get("address") or {}).lower()
    if "singapore" in addr or '"sg"' in addr:
        return True
    try:
        return float(store.get("storedistance", 9999)) <= MAX_STORE_DISTANCE_KM
    except (TypeError, ValueError):
        return False


def check_availability(parts):
    """Returns {(store_name, label): (available: bool, quote: str)}."""
    params = {
        "fae": "true",
        "pl": "true",
        "mts.0": "regular",
        "mts.1": "compact",
        "searchNearby": "true",
        "location": SEARCH_POSTCODE,
    }
    labels = list(parts)
    for i, label in enumerate(labels):
        params[f"parts.{i}"] = parts[label]

    try:
        raw = http_get(FULFILMENT_URL, params)
    except urllib.error.HTTPError as e:
        if e.code == 541:
            raise RuntimeError("Apple refused the stock check (HTTP 541, Apple's automated-traffic block)") from None
        raise RuntimeError(f"Apple returned HTTP {e.code}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("Apple sent back a web page instead of stock data") from None
    stores = (((data.get("body") or {}).get("content") or {}).get("pickupMessage") or {}).get("stores")
    if stores is None:
        raise RuntimeError(f"Unexpected response shape: {str(data)[:300]}")

    result = {}
    for store in stores:
        if not _in_singapore(store):
            continue
        name = store.get("storeName") or store.get("storeNumber", "Unknown store")
        pa = store.get("partsAvailability") or {}
        for label in labels:
            info = pa.get(parts[label]) or {}
            display = (info.get("pickupDisplay") or "").lower()
            quote = info.get("pickupSearchQuote") or info.get("storePickupQuote") or display or "unknown"
            quote = re.sub(r"<[^>]+>", "", quote)
            result[(name, label)] = (display == "available", quote)
    if not result:
        raise RuntimeError("No Singapore stores in Apple's response.")
    return result


# ------------------------------------------------------------------ main ---

def run_once(parts, state, token, chat_id):
    notify_gone = env("NOTIFY_SOLD_OUT", "true").lower() == "true"
    seen = state.setdefault("seen", {})

    try:
        avail = check_availability(parts)
    except Exception as e:  # noqa: BLE001
        state["errors"] = state.get("errors", 0) + 1
        log(f"Check failed ({state['errors']} in a row): {e}")
        if state["errors"] == ERROR_ALERT_AFTER:
            telegram(
                "⚠️ <b>Stock watcher is having a moment.</b>\n"
                f"Apple hasn't answered properly {ERROR_ALERT_AFTER} times in a row "
                f"(last error: <code>{str(e)[:150]}</code>).\n"
                "I'll keep trying and tell you when I'm back. If this keeps happening, "
                "Apple may be blocking GitHub's servers — see the README for running it at home.",
                token, chat_id,
            )
        return

    if state.get("errors", 0) >= ERROR_ALERT_AFTER:
        telegram("✅ Stock watcher is back on duty. Carry on.", token, chat_id)
    state["errors"] = 0

    newly_in, newly_out = [], []
    for (store, label), (ok, quote) in sorted(avail.items()):
        key = f"{store} | {label}"
        was = seen.get(key, False)
        log(f"{'🟢' if ok else '⚪'} {store:<24} {label:<30} {quote}")
        if ok and not was:
            newly_in.append((store, label, quote))
        elif was and not ok:
            newly_out.append((store, label))
        seen[key] = ok

    if newly_in:
        lines = [f"• <b>{label}</b> — Apple {store} ({quote})" for store, label, quote in newly_in]
        links = "\n".join(
            f'<a href="{WATCH[l]}">Buy {l.replace("iPhone Duo 512GB ", "")}</a>'
            for l in sorted({l for _, l, _ in newly_in}) if l in WATCH
        )
        telegram(
            "🟢 <b>iPhone Duo pickup stock just landed!</b>\n\n"
            + "\n".join(lines)
            + "\n\nRun, don't walk. (Or tap, don't scroll.)\n"
            + (links + "\n" if links else "")
            + f"\n<i>{now_sg()}</i>",
            token, chat_id,
        )
    if newly_out and notify_gone:
        lines = [f"• {label} — Apple {store}" for store, label in newly_out]
        telegram(
            "🔴 <b>Gone again:</b>\n" + "\n".join(lines)
            + "\nThat was quick. I'll keep watching.",
            token, chat_id,
        )


def main():
    mode = env("MODE", "check").lower()
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    state_file = env("STATE_FILE", "state.json")

    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set.")

    if mode == "test":
        if not chat_id:
            print_chat_ids(token)
            sys.exit(0)
        ok = telegram(
            "👋 Hello from your iPhone Duo stock watcher.\n"
            "I'm watching the 512GB in Star White and Night Sky at every Apple Store in Singapore. "
            "I'll only speak up when something changes — I promise not to be that group chat.",
            token, chat_id,
        )
        sys.exit(0 if ok else 1)

    if not chat_id:
        sys.exit("TELEGRAM_CHAT_ID is not set. Run with MODE=test to find it.")

    state = load_state(state_file)

    if mode == "discover":
        parts = get_parts(state, verbose=True)
        try:
            avail = check_availability(parts)
            log("Stock check worked. Current status:")
            for (store, label), (ok, quote) in sorted(avail.items()):
                log(f"  {'🟢' if ok else '⚪'} {store:<24} {label:<30} {quote}")
        except Exception as e:  # noqa: BLE001
            log(f"STOCK CHECK FAILED: {e}")
            sys.exit(1)
        return

    parts = get_parts(state)
    if not parts:
        state["errors"] = state.get("errors", 0) + 1
        if state["errors"] == ERROR_ALERT_AFTER:
            telegram(
                "⚠️ I couldn't work out the iPhone Duo part numbers from Apple's site. "
                "Run the workflow in <b>discover</b> mode, or set PART_NUMBERS (see README).",
                token, chat_id,
            )
        save_state(state_file, state)
        sys.exit(1)

    run_minutes = float(env("RUN_MINUTES", "0"))
    interval = max(30, int(env("INTERVAL_SECONDS", "30")))
    deadline = time.time() + run_minutes * 60

    while True:
        run_once(parts, state, token, chat_id)
        save_state(state_file, state)
        if time.time() + interval > deadline:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
