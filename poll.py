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

# How grouping works:
# - Each run looks only at messages that are NEW since the last tweet (last_id.txt).
# - New updates from the same day (Eastern time) become ONE tweet ("updated 5 times...").
# - Updates from different days are never mixed: each day gets its own tweet.
# - Once tweeted, an update is never counted again by a later run.
#
# Optional: wait until RoAPI has been quiet this many minutes before tweeting today's updates.
# 0 = tweet at the next check, no waiting.
QUIET_MINUTES = int(os.getenv("QUIET_MINUTES", "0"))
MAX_WAIT_MINUTES = int(os.getenv("MAX_WAIT_MINUTES", "120"))  # never hold updates longer than this

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


def to_local(sent_utc):
    if FIXED_EST:
        return sent_utc.astimezone(timezone(timedelta(hours=-5))), "EST"
    local = sent_utc.astimezone(ZoneInfo("America/New_York"))
    return local, local.tzname()  # EST or EDT, whichever is correct on that date


def when_text(local, label):
    """Example: October 27, 2026, 3:45 PM EDT"""
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%B')} {local.day}, {local.year}, {hour}:{local.minute:02d} {ampm} {label}"


def clock_text(local, label):
    """Example: 1 PM EDT (or 1:05 PM EDT when the minutes are not 00)"""
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    clock = f"{hour} {ampm}" if local.minute == 0 else f"{hour}:{local.minute:02d} {ampm}"
    return f"{clock} {label}"


def build_tweet(sent_utc):
    local, label = to_local(sent_utc)
    return (
        f"🚨UPDATE: The Bite by Night Testing Game has been updated at "
        f"{when_text(local, label)}.\n\n{HASHTAGS}"
    )


def build_group_tweet(times_utc):
    """Several updates from the SAME day."""
    times_utc = sorted(times_utc)
    n = len(times_utc)
    first, first_label = to_local(times_utc[0])
    last, last_label = to_local(times_utc[-1])

    if first.strftime("%Y%m%d%H%M") == last.strftime("%Y%m%d%H%M"):
        span = f"at {when_text(first, first_label)}"
    else:
        span = f"throughout {clock_text(first, first_label)} to {clock_text(last, last_label)}"

    return (
        f"🚨UPDATE: The Bite by Night Testing Game has been updated {n} times "
        f"{span}.\n\n{HASHTAGS}"
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


def split_by_day(messages):
    """Cuts the (oldest-first) message list into one group per Eastern-time day."""
    groups = []
    for m in messages:
        day = to_local(datetime.fromisoformat(m["timestamp"]))[0].date()
        if groups and groups[-1][0] == day:
            groups[-1][1].append(m)
        else:
            groups.append((day, [m]))
    return groups


def should_wait(times):
    if QUIET_MINUTES <= 0 or ALLOW_HUMANS:
        return False
    now = datetime.now(timezone.utc)
    quiet_for = (now - times[-1]).total_seconds() / 60
    held_for = (now - times[0]).total_seconds() / 60
    if quiet_for < QUIET_MINUTES and held_for < MAX_WAIT_MINUTES:
        print(
            f"{len(times)} update(s) found. Waiting until RoAPI has been quiet "
            f"for {QUIET_MINUTES} minutes (quiet for {quiet_for:.0f} so far)."
        )
        return True
    return False


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

            # X can show TWO post boxes (the pop-up and the home page one behind it).
            # Wait for any box, then work ONLY inside the pop-up if there is one.
            page.locator('[data-testid="tweetTextarea_0"]').first.wait_for(timeout=30_000)
            page.wait_for_timeout(2000)
            dialog = page.locator('[role="dialog"]').filter(
                has=page.locator('[data-testid="tweetTextarea_0"]')
            )
            scope = dialog.first if dialog.count() > 0 else page

            box = scope.locator('[data-testid="tweetTextarea_0"]').first
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
                scope.locator('input[data-testid="fileInput"]').first.set_input_files(image_path)
                scope.locator('[data-testid="attachments"]').first.wait_for(timeout=60_000)
                page.wait_for_timeout(3000)  # let the image finish uploading

            button = scope.locator(
                '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
            ).first
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

    # Only messages AFTER the bookmark: anything already tweeted is never seen again.
    messages = discord_get(f"/channels/{THREAD_ID}/messages?after={last_id}&limit=100")
    messages.sort(key=lambda m: int(m["id"]))
    if not messages:
        print("No new messages.")
        return

    groups = split_by_day(messages)
    image = find_image()
    if image is None:
        print("WARNING: no update.png found in the repository, posting without an image.")

    poster = Poster()
    try:
        for index, (day, group) in enumerate(groups):
            group_last_id = group[-1]["id"]
            targets = [m for m in group if is_target(m)]
            if not targets:
                save_last_id(group_last_id)
                print(f"{day}: new messages, but none from RoAPI. Skipped.")
                continue

            times = sorted(datetime.fromisoformat(m["timestamp"]) for m in targets)

            # only the newest day can still be "in progress"; older days are finished
            if index == len(groups) - 1 and should_wait(times):
                break

            text = build_tweet(times[0]) if len(times) == 1 else build_group_tweet(times)

            if DRY_RUN:
                print(f"DRY RUN - would tweet ({day}):\n{text}\nDRY RUN - image: {image}")
            else:
                poster.post(text, image)
                print(f"Posted ({day}):\n{text}")

            # bookmark moves forward right after each tweet, so these updates
            # can never be counted again by the next run
            save_last_id(group_last_id)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)  # whatever was not tweeted yet is retried on the next run
    finally:
        poster.close()


main()
