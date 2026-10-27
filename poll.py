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
HASHTAGS = "#BiteByNight #ROBLOX"
IMAGE_NAMES = ("update.png", "update.jpg", "update.jpeg", "update.webp")


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
        f"{date_str}, {time_str} {label}.\n\n{HASHTAGS}"
    )


def find_image():
    for name in IMAGE_NAMES:
        if os.path.exists(name):
            return name
    return None


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
        from playwright.sync_api import sync_playwright

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(os.environ["X_STATE_JSON"])
        self.pw = sync_playwright().start()
        try:
            # GitHub's machines already come with Google Chrome installed
            self.browser = self.pw.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001
            print(f"Chrome not found ({e}). Installing it...")
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "--with-deps", "chrome"],
                check=True,
            )
            self.browser = self.pw.chromium.launch(channel="chrome", headless=True)
        self.ctx = self.browser.new_context(
            storage_state=STATE_FILE, viewport={"width": 1280, "height": 900}
        )

    def post(self, text, image_path=None):
        if self.ctx is None:
            self._start()

        page = self.ctx.new_page()
        try:
            page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
            box = page.locator('[data-testid="tweetTextarea_0"]')
            box.wait_for(timeout=30_000)  # fails here if the login expired
            box.click()

            # type the text line by line; Enter makes a new line
            for i, line in enumerate(text.split("\n")):
                if i > 0:
                    page.keyboard.press("Enter")
                if line:
                    page.keyboard.insert_text(line)  # insert_text handles emoji reliably
            page.keyboard.insert_text(" ")  # a final space closes X's hashtag suggestion box
            page.wait_for_timeout(1000)

            if image_path:
                page.locator('input[data-testid="fileInput"]').first.set_input_files(image_path)
                page.locator('[data-testid="attachments"]').wait_for(timeout=60_000)
                page.wait_for_timeout(3000)  # let the image finish uploading

            button = page.locator('[data-testid="tweetButton"]')
            button.wait_for(timeout=10_000)
            try:
                button.click(timeout=15_000)
            except Exception:  # noqa: BLE001
                # something invisible is covering the button, so click it from inside the page
                button.evaluate("el => el.click()")
            page.wait_for_timeout(4000)  # let the post go through
        except Exception:
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

    image = find_image()
    poster = Poster()
    try:
        for m in messages:
            if is_target(m):
                text = build_tweet(datetime.fromisoformat(m["timestamp"]))
                if image is None:
                    print("WARNING: no update.png found in the repository, posting without an image.")
                if DRY_RUN:
                    print("DRY RUN - would tweet:\n" + text)
                    print(f"DRY RUN - image: {image}")
                else:
                    poster.post(text, image)
                    print("Posted:\n" + text)
            save_last_id(m["id"])  # only moves forward after success
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)  # this message is retried on the next run
    finally:
        poster.close()


main()
