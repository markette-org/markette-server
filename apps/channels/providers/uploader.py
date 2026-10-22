"""The optional upload bot for sites without an upload API (the adult platforms).

It runs only in the separate "uploader" worker (Playwright + Chromium, compose profile
``uploader``, ``MARKETTE_UPLOADER=1``), with the account's own browser session (cookies the
user exported). Each site has a *recipe*: a list of steps (go to the upload page, check the
session, pick the file, fill the title/description/tags, submit, wait for the post's
address). A channel can override its recipe in ``config["bot_recipe"]`` when a site changes
its pages.

The bot does not solve CAPTCHAs, hide that it is automated, or retry around a site's
checks: whatever it cannot do falls back to the hand-off, where a person finishes the
upload. The default recipes use generic selectors and have not been tested against the
sites' real (signed-in) pages: run **Test the upload bot** on a channel before relying on it.
"""

import json
import os
import re
import time

from apps.platform import conf

from .base import ProviderError, PublishResult


class HandoffRequired(ProviderError):
    """Publishing needs a person: the post waits as "Ready to upload".

    ``maybe_uploaded``: the bot got past the submit step, so the post may already be on the site.
    """

    def __init__(self, message, *, maybe_uploaded=False):
        super().__init__(message)
        self.maybe_uploaded = maybe_uploaded


def in_uploader():
    return os.environ.get("MARKETTE_UPLOADER") == "1"


def bot_installed():
    return bool(conf.get("UPLOAD_BOT_ENABLED"))


# --- cookies -----------------------------------------------------------------------------
SAME_SITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}


def parse_cookies(text, site):
    """Playwright cookies from a Cookie-Editor/EditThisCookie JSON export, or a "a=1; b=2" header."""
    text = text.strip()
    domain = "." + site.removeprefix("www.")
    try:
        data = json.loads(text)
    except ValueError:
        pairs = [p.split("=", 1) for p in text.split(";") if "=" in p]
        if not pairs:
            raise ProviderError(
                "Paste the cookies as JSON (exported from the browser) or as name=value pairs."
            ) from None
        return [{"name": k.strip(), "value": v.strip(), "domain": domain, "path": "/"} for k, v in pairs]
    if isinstance(data, dict):
        data = data.get("cookies") or []
    cookies = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        cookie = {
            "name": str(c["name"]),
            "value": str(c.get("value", "")),
            "domain": c.get("domain") or domain,
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": SAME_SITE.get(str(c.get("sameSite", "lax")).lower(), "Lax"),
        }
        if c.get("expirationDate") or c.get("expires"):
            cookie["expires"] = float(c.get("expirationDate") or c.get("expires"))
        cookies.append(cookie)
    if not cookies:
        raise ProviderError("No cookies found in what was pasted.")
    return cookies


# --- recipes -------------------------------------------------------------------------------
# Steps: {"goto": url} | {"logged_in": selector} | {"file": selector} | {"fill": selector, "value": field}
# | {"click": selector, "submit": true} | {"wait": ms} | {"result_url": regex} (waits for an address).
# field: title | description | tags | price. Generic selectors: check them with "Test the upload bot".
TITLE = "input[name*='title' i], input[placeholder*='title' i]"
DESCRIPTION = "textarea[name*='desc' i], textarea[placeholder*='desc' i], textarea"
TAGS = "input[name*='tag' i], input[placeholder*='tag' i]"
FILE = "input[type=file]"

GENERIC_VIDEO = [
    {"file": FILE},
    {"fill": TITLE, "value": "title"},
    {"fill": DESCRIPTION, "value": "description"},
    {"fill": TAGS, "value": "tags"},
    {"click": "button[type=submit], input[type=submit]", "submit": True},
]

RECIPES = {
    "pornhub": [
        {"goto": "https://www.pornhub.com/upload/videodata"},
        {"logged_in": "a[href*='/users/'], a[href*='/model/'], [class*='userAvatar' i]"},
        {"file": FILE},
        {"fill": TITLE, "value": "title"},
        {"fill": TAGS, "value": "tags"},
        {"click": "button[type=submit], input[type=submit]", "submit": True},
        {"result_url": r"view_video\.php\?viewkey=\w+"},
    ],
    "youporn": [
        {"goto": "https://www.youporn.com/upload/"},
        {"logged_in": "a[href*='/uservids/'], a[href*='/logout']"},
        {"file": FILE},
        {"fill": TITLE, "value": "title"},
        {"fill": DESCRIPTION, "value": "description"},
        {"click": "button[type=submit], input[type=submit]", "submit": True},
        {"result_url": r"/watch/\d+"},
    ],
    "redtube": [
        {"goto": "https://www.redtube.com/upload"},
        {"logged_in": "a[href*='/users/'], a[href*='logout']"},
        {"file": FILE},
        {"fill": TITLE, "value": "title"},
        {"fill": TAGS, "value": "tags"},
        {"click": "button[type=submit], input[type=submit]", "submit": True},
        {"result_url": r"redtube\.com/\d+"},
    ],
    "xhamster": [
        {"goto": "https://xhamster.com/upload"},
        {"logged_in": "a[href*='/users/'], [data-role*='user' i]"},
        {"file": FILE},
        {"fill": TITLE, "value": "title"},
        {"fill": DESCRIPTION, "value": "description"},
        {"click": "button[type=submit]", "submit": True},
        {"result_url": r"/videos/[\w-]+"},
    ],
    "onlyfans": [
        {"goto": "https://onlyfans.com/posts/create"},
        {"logged_in": "a[href='/my/settings'], a[href*='/my/']"},
        {"file": FILE},
        {"fill": DESCRIPTION, "value": "description"},
        {"click": "button[type=submit], button:has-text('Post')", "submit": True},
        {"result_url": r"onlyfans\.com/\d+/[\w.-]+"},
    ],
    "fansly": [
        {"goto": "https://fansly.com/home"},
        {"logged_in": "app-account-avatar, a[href*='/settings']"},
        {"file": FILE},
        {"fill": DESCRIPTION, "value": "description"},
        {"click": "button:has-text('Post')", "submit": True},
        {"result_url": r"/post/\d+"},
    ],
}


RECIPES.update(
    {
        "xvideos": [
            {"goto": "https://www.xvideos.com/account/uploads/new"},
            {"logged_in": "a[href*='/account'], a[href*='logout']"},
            *GENERIC_VIDEO,
            {"result_url": r"xvideos\.com/video\.?\w+"},
        ],
        "xnxx": [
            {"goto": "https://www.xnxx.com/"},
            {"logged_in": "a[href*='/account'], a[href*='logout']"},
            *GENERIC_VIDEO,
            {"result_url": r"xnxx\.com/video-\w+"},
        ],
        "spankbang": [
            {"goto": "https://spankbang.com/users/upload"},
            {"logged_in": "a[href*='/users/'], a[href*='logout']"},
            *GENERIC_VIDEO,
            {"result_url": r"spankbang\.com/\w+/video/"},
        ],
        "eporner": [
            {"goto": "https://www.eporner.com/upload/"},
            {"logged_in": "a[href*='/profile/'], a[href*='logout']"},
            *GENERIC_VIDEO,
            {"result_url": r"eporner\.com/(video-|hd-porn/)\w+"},
        ],
        "manyvids": [
            {"goto": "https://www.manyvids.com/Upload-vids/"},
            {"logged_in": "a[href*='/Profile/'], a[href*='logout' i]"},
            *GENERIC_VIDEO,
            {"fill": "input[name*='price' i]", "value": "price"},
            {"result_url": r"manyvids\.com/Video/\d+"},
        ],
        "clips4sale": [
            {"goto": "https://admin.clips4sale.com/"},
            {"logged_in": "a[href*='logout' i], [class*='studio' i]"},
            *GENERIC_VIDEO,
            {"fill": "input[name*='price' i]", "value": "price"},
            {"result_url": r"clips4sale\.com/studio/\d+/\d+"},
        ],
    }
)


def recipe_for(provider):
    custom = (provider.channel.config or {}).get("bot_recipe")
    return custom if isinstance(custom, list) and custom else RECIPES.get(provider.key, [])


def _value(field, content):
    if field == "title":
        return content.title
    if field == "description":
        return content.text
    if field == "tags":
        return ", ".join((content.options or {}).get("tags") or content.tags or [])
    if field == "price":
        return str((content.options or {}).get("price") or "")
    return ""


# --- running ---------------------------------------------------------------------------------
def _browser(provider):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # main image: the bot lives in the uploader image only
        raise HandoffRequired("The upload bot is not installed on this server; upload it by hand.") from exc
    return sync_playwright


def run(provider, content=None, *, dry_run=False, screenshot=None):
    """Runs the channel's recipe. dry_run stops before choosing the file (checks the session and the form)."""
    steps = recipe_for(provider)
    if not steps:
        raise HandoffRequired(f"There is no upload bot recipe for {provider.label}.")
    sync_playwright = _browser(provider)
    submitted = False
    report = []
    page = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            ctx = browser.new_context(locale="en-US")
            ctx.add_cookies(provider.creds.get("bot_cookies") or [])
            page = ctx.new_page()
            page.set_default_timeout(30_000)
            for step in steps:
                if dry_run and any(k in step for k in ("file", "fill", "click", "result_url", "wait")):
                    # A test run only looks: does the page have what the next steps need?
                    selector = step.get("file") or step.get("fill") or step.get("click")
                    if selector:
                        report.append({"step": step, "found": page.locator(selector).count() > 0})
                    continue
                if "goto" in step:
                    page.goto(step["goto"], wait_until="domcontentloaded")
                    report.append({"step": step, "url": page.url})
                elif "logged_in" in step:
                    ok = page.locator(step["logged_in"]).count() > 0
                    report.append({"step": step, "found": ok})
                    if not ok:
                        raise HandoffRequired(
                            f"The upload bot is not signed in to {provider.label} (the session expired or the site "
                            "asked for a check). Export fresh cookies, or upload it by hand."
                        )
                elif "file" in step:
                    page.locator(step["file"]).first.set_input_files([m.path for m in content.media])
                elif "fill" in step:
                    value = _value(step.get("value"), content)
                    if value and page.locator(step["fill"]).count():
                        page.locator(step["fill"]).first.fill(value)
                elif "click" in step:
                    page.locator(step["click"]).first.click()
                    submitted = submitted or bool(step.get("submit"))
                elif "wait" in step:
                    time.sleep(min(int(step["wait"]), 600_000) / 1000)
                elif "result_url" in step:
                    pattern = re.compile(step["result_url"])
                    deadline = time.monotonic() + 15 * 60  # videos take a while to upload
                    while time.monotonic() < deadline:
                        found = pattern.search(page.url) or next(
                            (
                                h
                                for h in page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
                                if pattern.search(h)
                            ),
                            None,
                        )
                        if found:
                            return {"url": found if isinstance(found, str) else page.url, "report": report}
                        time.sleep(5)
                    raise HandoffRequired(
                        f"The upload bot sent it to {provider.label} but did not see the new post's address.",
                        maybe_uploaded=True,
                    )
            return {"url": page.url, "report": report}
        except HandoffRequired:
            raise
        except Exception as exc:  # noqa: BLE001 - any page change ends in a hand-off, never a retry loop
            raise HandoffRequired(
                f"The upload bot could not finish on {provider.label} ({exc.__class__.__name__}: {str(exc)[:200]}).",
                maybe_uploaded=submitted,
            ) from exc
        finally:
            if screenshot and page is not None:
                try:
                    page.screenshot(path=screenshot, full_page=False)
                except Exception:  # noqa: BLE001
                    pass
            browser.close()


def upload(provider, content):
    if not provider.uses_bot:
        raise HandoffRequired(f"Upload it on {provider.label}, then paste the address of the new post.")
    if not bot_installed() or not in_uploader():
        raise HandoffRequired("The upload bot is not running on this server; upload it by hand.")
    result = run(provider, content)
    url = result["url"]
    return PublishResult(external_id=provider.post_id(url) or url[:500], url=url)
