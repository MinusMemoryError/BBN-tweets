"""
Bite by Night: Discord thread -> X, run by GitHub Actions.
Free, no server, no credit card, no X API.

Every few minutes GitHub runs this script. It asks Discord for new messages in
the RoAPI thread. For each new bot/webhook message it posts:

  🚨UPDATE: The Bite by Night Testing Game has been updated at {date}, {time} EST.

The post is made through a real browser that uses the saved X login.
The ID of the last message handled is kept in last_id.txt so nothing is
tweeted twice.
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
THREAD_ID = os.environ["THREAD_ID"].strip()
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"  # 1 = only print, 0 = really tweet
ALLOW_HUMANS = os.getenv("ALLOW_HUMANS", "0") == "1"  # 1 = also react to people (for testing)
FIXED_EST = os.getenv("FIXED_EST", "0") == "1"  # 1 = always UTC-5 labeled EST
LAST_ID_FILE = "last_id.txt"
STATE_FILE = "x_state.json"  # written at runtime from the X_STATE_JSON secret


def discord_get(path):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "User-Agent": "DiscordBot (https://github.com, 1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def build_tweet(sent_utc):
    if FIXED_EST:
        sent = sent_utc.astimezone(timezone(timedelta(hours=-5)))
        label = "EST"
    else:
        sent = sent_utc.astimezone(ZoneInfo("America/New_York"))
        label = sent.tzname()  # EST or EDT, whichever is correct on that date

    date_str = f"{sent.strftime('%B')} {sent.day}, {sent.year}"
    hour = sent.hour % 12 or 12
    time_str = f"{hour}:{sent.minute:02d} {'AM' if sent.hour < 12 else 'PM'}"
    return (
        f"🚨UPDATE: The Bite by Night Testing Game has been updated at "
        f"{date_str}, {time_str} {label}."
    )


def is_target(message):
    if message.get("webhook_id") or message["author"].get("bot"):
        return True
    return ALLOW_HUMANS


def save_last_id(message_id):
    with open(LAST_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(message_id))


class Poster:
    """Starts the browser only when there is really something to tweet."""

    def __init__(self):
        self.pw = None
        self.browser = None
        self.ctx = None

    def _start(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
            check=True,
        )
        from playwright.sync_api import sync_playwright

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(os.environ["X_STATE_JSON"])
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True)
        self.ctx = self.browser.new_context(
            storage_state=STATE_FILE, viewport={"width": 1280, "height": 900}
        )

    def post(self, text):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if self.ctx is None:
            self._start()
        page = self.ctx.new_page()
        try:
            page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
            box = page.locator('[data-testid="tweetTextarea_0"]')
            box.wait_for(timeout=30_000)  # fails here if the login expired
            box.click()
            page.keyboard.insert_text(text)  # insert_text handles emoji reliably
            page.wait_for_timeout(1000)
            button = page.locator('[data-testid="tweetButton"]')
            button.wait_for(timeout=10_000)
            button.click()
            page.wait_for_timeout(4000)  # let the post go through
        except PlaywrightTimeout:
            page.screenshot(path="post_failed.png")
            raise
        finally:
            page.close()

    def close(self):
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()


def main():
    last_id = ""
    if os.path.exists(LAST_ID_FILE):
        with open(LAST_ID_FILE, encoding="utf-8") as f:
            last_id = f.read().strip()

    if not last_id:
        latest = discord_get(f"/channels/{THREAD_ID}/messages?limit=1")
        save_last_id(latest[0]["id"] if latest else "0")
        print("First run: remembered the newest message. Nothing tweeted.")
        return

    messages = discord_get(f"/channels/{THREAD_ID}/messages?after={last_id}&limit=50")
    messages.sort(key=lambda m: int(m["id"]))
    if not messages:
        print("No new messages.")
        return

    poster = Poster()
    try:
        for m in messages:
            if is_target(m):
                text = build_tweet(datetime.fromisoformat(m["timestamp"]))
                if DRY_RUN:
                    print("DRY RUN - would tweet:\n" + text)
                else:
                    poster.post(text)
                    print("Posted:\n" + text)
            save_last_id(m["id"])  # only moves forward after success
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)  # this message is retried on the next run
    finally:
        poster.close()


main()
