"""Browser control for the agent, built on Playwright.

One file holds everything: the runtime, the sessions and every browser_* tool.

How it is put together
- Playwright lives on one background thread with its own event loop. Tool
  calls arrive from any thread (the main loop's workers, parallel subagents)
  and are handed to that loop, so the browser survives across turns.
- Each agent owns one Session: its own browser, cookies and tabs. The main
  agent is "main"; a subagent's session is keyed by its name (OWNER).
- Every tool returns text and never raises. A result ends with the page
  status and anything that happened meanwhile (dialogs, downloads, new tabs,
  console errors, failed requests), so the agent always knows where it is.
- A crashed or closed browser is restarted on the next call, with cookies and
  storage restored from the last snapshot and the last page reopened.
"""

import asyncio
import atexit
import concurrent.futures
import json
import os
import re
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from . import config, history, workspace

OWNER = ContextVar("browser_owner", default="main")

ARTIFACTS = Path(tempfile.gettempdir()) / f"axos-browser-{os.getpid()}"
STATE_DIR = Path.home() / ".agents" / "browser"  # saved logins outlive the process

CALL_TIMEOUT = 180  # seconds: hard ceiling for one tool call
REF = re.compile(r"e\d+")
CLOSED = re.compile(r"has been closed|Target closed|Connection closed|Browser closed", re.I)

SCHEMAS = []  # tool definitions, in registration order
TOOLS = {}    # name -> blocking callable
SESSIONS = {}  # owner -> Session


class BrowserError(Exception):
    """A failure worded for the model: what went wrong and what to do next."""


# ------------------------------------------------------------------ runtime

class Runtime:
    """The background thread and event loop that own Playwright."""

    def __init__(self):
        self.loop = None
        self.playwright = None
        self._starting = threading.Lock()

    def run(self, coroutine, timeout=CALL_TIMEOUT):
        """Run a coroutine on the browser loop and wait for its result."""
        with self._starting:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(
                    target=self.loop.run_forever, name="axos-browser", daemon=True
                ).start()
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise BrowserError(
                f"the browser did not answer within {timeout}s; the call was cancelled"
            ) from None

    async def driver(self):
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        return self.playwright


RUNTIME = Runtime()


def explain(failure):
    """A Playwright error, cut down to what the model can act on."""
    text = str(failure)
    message = " ".join(text.split("Call log:")[0].split())[:300]
    if "intercepts pointer events" in text:
        message += " Another element covers it (a dialog, banner or overlay): close that first."
    elif isinstance(failure, PlaywrightTimeout):
        message += " The page may still be loading: use browser_wait_for, or take a new snapshot."
    elif "not an <input>" in text or "not editable" in text.lower():
        message += " Use browser_select for dropdowns, browser_click for checkboxes."
    return message


def safe_name(name, suffix):
    stem = re.sub(r"[^\w.-]", "_", Path(str(name)).name) or "file"
    return stem if stem.endswith(suffix) else stem + suffix


async def quiet(page, timeout=2500):
    """Wait for the network to go idle, but never fail because it did not."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightError:
        pass


async def settle(page):
    """Let the page react to an action: navigation first, then lazy requests."""
    await page.wait_for_timeout(120)
    try:
        await page.wait_for_load_state("domcontentloaded")
    except PlaywrightError:
        pass
    await quiet(page)


# The page is described by a script that tags every visible, meaningful
# element with a ref the agent can act on.
SNAPSHOT_JS = """
(start) => {
  const wanted = 'a[href],button,input,select,textarea,summary,[role],[onclick],' +
    '[contenteditable=""],[contenteditable="true"],[tabindex]:not([tabindex="-1"]),' +
    'h1,h2,h3,label,img[alt]';
  const roles = {a:'link', button:'button', select:'combobox', textarea:'textbox',
    summary:'button', img:'img', h1:'heading', h2:'heading', h3:'heading', label:'label'};
  const inputs = {checkbox:'checkbox', radio:'radio', submit:'button', button:'button',
    file:'file', range:'slider'};
  document.querySelectorAll('[data-axos-ref]').forEach(e => e.removeAttribute('data-axos-ref'));
  const out = [];
  let n = start;
  for (const el of document.querySelectorAll(wanted)) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (!box.width || !box.height || style.visibility === 'hidden' ||
        style.display === 'none' || el.type === 'hidden') continue;
    const tag = el.tagName.toLowerCase();
    const ref = 'e' + (n++);
    el.setAttribute('data-axos-ref', ref);
    const label = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    const name = (el.getAttribute('aria-label') || label || el.getAttribute('alt') ||
      el.getAttribute('placeholder') || el.getAttribute('title') ||
      (tag === 'input' || tag === 'select' ? (el.name || el.id) : el.innerText) || '')
      .replace(/\\s+/g, ' ').trim().slice(0, 100);
    const item = {ref, name,
      role: el.getAttribute('role') || roles[tag] ||
        (tag === 'input' ? (inputs[el.type] || 'textbox') : tag)};
    if (tag === 'select' && el.selectedOptions[0]) item.value = el.selectedOptions[0].text.slice(0, 80);
    else if ((tag === 'input' || tag === 'textarea') && el.value &&
        !['password', 'checkbox', 'radio', 'file'].includes(el.type)) item.value = String(el.value).slice(0, 80);
    if (el.checked) item.checked = true;
    if (el.disabled) item.disabled = true;
    if (tag === 'a') item.href = (el.getAttribute('href') || '').slice(0, 120);
    if (box.bottom <= 0 || box.top >= innerHeight) item.offscreen = true;
    out.push(item);
  }
  return out;
}
"""


# ------------------------------------------------------------------ session

class Session:
    """One agent's browser: process, context, tabs and everything observed."""

    def __init__(self, owner, headless, width, height, permissions, state):
        self.owner = owner
        self.headless = headless
        self.viewport = {"width": width, "height": height}
        self.permissions = permissions or []
        self.state = state          # storage state used when a context opens
        self.lock = asyncio.Lock()  # one tool call at a time per session

        self.browser = None
        self.context = None
        self.pages = []
        self.active = None
        self.refs = {}              # ref -> frame it was found in
        self.last_url = ""
        self.closed = False

        self.events = []            # told to the agent with the next result
        self.console = deque(maxlen=300)
        self.network = deque(maxlen=500)
        self.new_errors = 0
        self.new_failures = 0
        self.dialog_action = "dismiss"
        self.dialog_text = None
        self.mocks = {}             # url pattern -> (status, body, content type)
        self.tracing = False
        self._reopen = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self):
        driver = await RUNTIME.driver()
        try:
            self.browser = await driver.chromium.launch(headless=self.headless)
        except PlaywrightError as failure:
            if "Executable doesn't exist" not in str(failure):
                raise
            try:  # fall back to an installed Google Chrome
                self.browser = await driver.chromium.launch(
                    headless=self.headless, channel="chrome"
                )
            except PlaywrightError:
                raise BrowserError(
                    "no browser is installed. Run: uv run playwright install chromium"
                ) from None
        await self.open_context()

    async def open_context(self):
        self.pages, self.active, self.refs = [], None, {}
        self.context = await self.browser.new_context(
            viewport=self.viewport,
            accept_downloads=True,
            storage_state=self.state,
            permissions=self.permissions or None,
        )
        self.context.set_default_timeout(config.BROWSER_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(config.BROWSER_NAV_TIMEOUT_MS)
        self.context.on("page", self._on_page)
        self.context.on("console", self._on_console)
        self.context.on("weberror", self._on_page_error)
        self.context.on("dialog", self._on_dialog)
        self.context.on("response", self._on_response)
        self.context.on("requestfailed", self._on_request_failed)
        for pattern in self.mocks:
            await self.context.route(pattern, self._fulfil)
        self.tracing = False
        await self.context.new_page()

    async def close(self):
        self.closed = True
        for closing in (self.context, self.browser):
            try:
                if closing is not None:
                    await closing.close()
            except PlaywrightError:
                pass

    async def recover(self):
        """Bring a dead browser back with the last known cookies and page."""
        url = self.last_url
        try:
            if self.browser is not None:
                await self.browser.close()
        except PlaywrightError:
            pass
        await self.start()
        if url and url != "about:blank":
            try:
                await self.active.goto(url, wait_until="domcontentloaded")
            except PlaywrightError:
                pass
        self.events.append(
            "the browser had crashed or been closed, so it was restarted: cookies "
            "and storage were restored and the last page reopened. Typed input "
            "and scroll position are lost and every old ref is invalid"
        )

    async def page(self):
        """The active tab, repairing the session first if it is broken."""
        if self.browser is None or not self.browser.is_connected():
            await self.recover()
        self.pages = [page for page in self.pages if not page.is_closed()]
        if not self.pages:
            await self.context.new_page()
            if self._reopen and self.last_url:
                try:
                    await self.active.goto(self.last_url, wait_until="domcontentloaded")
                except PlaywrightError:
                    pass
        self._reopen = False
        if self.active not in self.pages:
            self.active = self.pages[-1]
        return self.active

    # -- observers ---------------------------------------------------------

    def _on_page(self, page):
        self.pages.append(page)
        if self.active is not None:
            self.events.append(f"a new tab opened (tab {len(self.pages)}) and is now active")
        self.active = page
        page.on("download", self._on_download)
        page.on("crash", self._on_crash)

    def _on_crash(self, page):
        if page in self.pages:
            self.pages.remove(page)
        self._reopen = True
        self.events.append("the tab crashed and was reopened at its last URL; old refs are invalid")

    def _on_console(self, message):
        self.console.append((message.type, message.text))
        if message.type == "error":
            self.new_errors += 1

    def _on_page_error(self, web_error):
        self.console.append(("pageerror", str(web_error.error)))
        self.new_errors += 1

    async def _on_dialog(self, dialog):
        accept = dialog.type == "alert" or self.dialog_action == "accept"
        self.events.append(
            f'a {dialog.type} dialog said "{dialog.message[:200]}" and was '
            f"{'accepted' if accept else 'dismissed'}"
            + ("" if accept else " (browser_handle_dialog changes that)")
        )
        try:
            if accept:
                await dialog.accept(self.dialog_text or "")
            else:
                await dialog.dismiss()
        except PlaywrightError:
            pass

    async def _on_download(self, download):
        folder = ARTIFACTS / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_name(download.suggested_filename, "")
        try:
            await download.save_as(path)
            self.events.append(f"a file was downloaded to {path}")
        except PlaywrightError as failure:
            self.events.append(f"a download failed: {explain(failure)}")

    def _on_response(self, response):
        request = response.request
        self.network.append(
            (response.status, request.method, response.url, request.resource_type, "")
        )
        if response.status >= 400:
            self.new_failures += 1

    def _on_request_failed(self, request):
        self.network.append(
            (0, request.method, request.url, request.resource_type, request.failure or "failed")
        )
        self.new_failures += 1

    async def _fulfil(self, route):
        for pattern, (status, body, content_type) in self.mocks.items():
            if re.fullmatch(_glob(pattern), route.request.url):
                if status == 0:
                    return await route.abort()
                return await route.fulfill(status=status, body=body, content_type=content_type)
        await route.continue_()

    # -- elements ----------------------------------------------------------

    def frame(self, page, name):
        for frame in page.frames:
            if name == frame.name or name in frame.url:
                return frame
        known = ", ".join(f.name or f.url for f in page.frames[1:]) or "none"
        raise BrowserError(f"no frame matches {name!r}. Frames on this page: {known}")

    def locator(self, page, target, frame=None):
        """Resolve a ref or selector, without waiting."""
        if REF.fullmatch(target):
            home = self.refs.get(target)
            if home is None or home.is_detached():
                raise BrowserError(
                    f"{target} is not a current ref. Call browser_get_accessibility "
                    "again and use a ref from the new snapshot."
                )
            return home.locator(f'[data-axos-ref="{target}"]')
        return (self.frame(page, frame) if frame else page).locator(target)

    async def element(self, page, target, frame=None):
        """Resolve a ref or selector and wait for it to exist (lazy content)."""
        found = self.locator(page, target, frame).first
        try:
            await found.wait_for(state="attached")
        except PlaywrightTimeout:
            hint = (
                "The page changed since the snapshot: take a new one."
                if REF.fullmatch(target)
                else "Check the selector, or take a snapshot and use a ref."
            )
            raise BrowserError(f"no element matches {target!r}. {hint}") from None
        return found

    # -- reporting ---------------------------------------------------------

    async def status(self):
        """The footer of every result: where we are and what happened."""
        page = await self.page()
        try:
            title = await page.title()
        except PlaywrightError:
            title = ""
        lines = [
            f"[page] {title or '(no title)'} - {page.url} "
            f"(tab {self.pages.index(page) + 1} of {len(self.pages)})"
        ]
        notes, self.events = self.events, []
        if self.new_errors:
            notes.append(f"{self.new_errors} new console error(s), see browser_get_console")
        if self.new_failures:
            notes.append(f"{self.new_failures} failed request(s), see browser_get_network")
        self.new_errors = self.new_failures = 0
        if notes:
            lines.append("[events] " + "; ".join(notes))
        try:  # the snapshot recover() restores from
            self.last_url = page.url
            self.state = await self.context.storage_state()
        except PlaywrightError:
            pass
        return "\n".join(lines)


def _glob(pattern):
    return re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")


# ------------------------------------------------------------- tool wiring

def _string(description):
    return {"type": "string", "description": description}


def _integer(description):
    return {"type": "integer", "description": description}


def _boolean(description):
    return {"type": "boolean", "description": description}


def _choice(description, *values):
    return {"type": "string", "enum": list(values), "description": description}


TARGET = _string(
    "The element: a ref from browser_get_accessibility (e.g. e12), or a Playwright "
    "selector (CSS, text=Sign in, role=button[name=\"Save\"])"
)
FRAME = _string("With a selector only: name or URL fragment of the iframe to look in")


def tool(description, properties=None, required=(), needs_session=True):
    """Register an async implementation as a blocking, never-raising tool."""

    def register(implementation):
        name = implementation.__name__
        SCHEMAS.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties or {},
                    "required": list(required),
                },
            },
        })

        def call(**arguments):
            owner = OWNER.get()  # read here: the browser loop is another thread
            try:
                return history.cap(
                    RUNTIME.run(_invoke(owner, name, implementation, arguments, needs_session))
                )
            except BrowserError as failure:
                return f"Error: {failure}"

        TOOLS[name] = call
        return implementation

    return register


async def _invoke(owner, name, implementation, arguments, needs_session):
    if not needs_session:
        return await implementation(owner, **arguments)

    session = SESSIONS.get(owner)
    if session is None:
        raise BrowserError("no browser session is open. Call browser_create_session first.")

    async with session.lock:
        try:
            result = await implementation(session, **arguments)
        except BrowserError as failure:
            result = f"Error: {failure}"
        except TypeError as mismatch:
            result = f"Error: wrong arguments for {name} ({mismatch})."
        except PlaywrightError as failure:
            if CLOSED.search(str(failure)):
                result = f"Error: the browser closed during {name}; the action may not have happened."
            else:
                result = f"Error: {explain(failure)}"

        if session.closed:
            return result
        try:
            return f"{result}\n\n{await session.status()}"
        except (PlaywrightError, BrowserError) as failure:
            return f"{result}\n\n[page] unavailable: {explain(failure)}"


@contextmanager
def owned_by(owner):
    """Give the enclosed tool calls their own session; close it afterwards."""
    token = OWNER.set(owner)
    try:
        yield
    finally:
        OWNER.reset(token)
        if owner in SESSIONS:
            try:
                RUNTIME.run(_close(owner), 30)
            except BrowserError:
                pass


async def _close(owner):
    session = SESSIONS.pop(owner, None)
    if session is not None:
        await session.close()


def shutdown():
    """Close every session and stop Playwright. Safe to call more than once."""
    if RUNTIME.loop is None:
        return

    async def close_all():
        for owner in list(SESSIONS):
            await _close(owner)
        if RUNTIME.playwright is not None:
            await RUNTIME.playwright.stop()
            RUNTIME.playwright = None

    try:
        RUNTIME.run(close_all(), 30)
    except Exception:  # noqa: BLE001 - shutting down; nothing useful to do
        pass


atexit.register(shutdown)


# ============================================================== the tools
# ---------------------------------------------------------------- lifecycle

@tool(
    "Start a browser session (or restart it, if one is open). Call this before "
    "any other browser tool. The session stays open across turns until "
    "browser_close_session.",
    {
        "headless": _boolean("Run without a window. Default: a window when a display exists"),
        "width": _integer("Viewport width in px (default 1280)"),
        "height": _integer("Viewport height in px (default 800)"),
        "state": _string("Name of a login state saved with browser_save_state to start from"),
        "permissions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Browser permissions to grant, e.g. geolocation, clipboard-read, notifications",
        },
    },
    needs_session=False,
)
async def browser_create_session(owner, headless=None, width=1280, height=800,
                                 state=None, permissions=None):
    restarted = owner in SESSIONS
    await _close(owner)

    storage = None
    if state:
        path = STATE_DIR / safe_name(state, ".json")
        if not path.exists():
            saved = ", ".join(p.stem for p in STATE_DIR.glob("*.json")) or "none"
            raise BrowserError(f"no saved state named {state!r}. Saved states: {saved}")
        storage = json.loads(path.read_text())

    if headless is None:
        headless = config.BROWSER_HEADLESS
    session = Session(owner, headless, width, height, permissions, storage)
    await session.start()
    SESSIONS[owner] = session
    return (
        f"Browser session {'restarted' if restarted else 'started'} "
        f"({'headless' if headless else 'visible window'}, {width}x{height})"
        f"{', logged-in state ' + state + ' loaded' if state else ''}.\n\n"
        f"{await session.status()}"
    )


@tool("Close the browser session and everything in it. Do this when the browsing task is finished.")
async def browser_close_session(session):
    SESSIONS.pop(session.owner, None)
    await session.close()
    return "Browser session closed."


# --------------------------------------------------------------- navigation

@tool(
    "Open a URL in the active tab and wait for it to load.",
    {
        "url": _string("The address. https:// is assumed when no scheme is given"),
        "wait_until": _choice(
            "When the page counts as loaded (default load). Use domcontentloaded "
            "for slow pages, networkidle for pages that keep fetching",
            "load", "domcontentloaded", "networkidle", "commit",
        ),
    },
    ["url"],
)
async def browser_navigate(session, url, wait_until="load"):
    page = await session.page()
    if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        url = "https://" + url
    before, note, response = page.url, "", None

    for attempt in (1, 2):
        try:
            response = await page.goto(url, wait_until=wait_until)
            break
        except PlaywrightTimeout:
            if page.url != before:
                note = (" It was still loading when the wait ran out, so content may be "
                        "missing: use browser_wait_for before reading it.")
                break
            if attempt == 2:
                raise BrowserError(f"{url} did not respond in time (tried twice).") from None
        except PlaywrightError as failure:
            if attempt == 2 or not re.search(r"ERR_(CONNECTION|NETWORK|TIMED_OUT|EMPTY)", str(failure)):
                raise
            await asyncio.sleep(1)

    session.refs = {}
    status = f"HTTP {response.status}" if response else "no response status"
    return f"Opened {page.url} ({status}).{note}"


@tool(
    "Go back, go forward, or reload the active tab.",
    {"action": _choice("What to do", "back", "forward", "reload")},
    ["action"],
)
async def browser_history(session, action):
    page = await session.page()
    moved = await {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action]()
    session.refs = {}
    if moved is None and action != "reload":
        return f"There is no page to go {action} to."
    return f"Done: {action}."


# --------------------------------------------------------------- inspection

@tool("Get the active tab's URL, title, size, scroll position, iframes and load state.")
async def browser_get_page_info(session):
    page = await session.page()
    info = await page.evaluate(
        "({ready: document.readyState, y: Math.round(scrollY), "
        "height: document.documentElement.scrollHeight, "
        "viewport: innerWidth + 'x' + innerHeight})"
    )
    frames = [f.name or f.url for f in page.frames[1:]]
    return (
        f"URL: {page.url}\nTitle: {await page.title()}\n"
        f"Viewport: {info['viewport']}, scrolled to {info['y']} of {info['height']}px\n"
        f"Load state: {info['ready']}\n"
        f"Frames: {', '.join(frames) or 'none'}"
    )


@tool(
    "List the page's visible interactive elements and headings, each with a ref "
    "(e1, e2, ...) to pass to the other tools. Call it after every navigation or "
    "page change: refs from an older snapshot stop working. Covers iframes.",
    {
        "filter": _string("Only elements whose role or name contains this text"),
        "viewport_only": _boolean("Only elements currently on screen (default false)"),
        "max_elements": _integer("Cap on listed elements (default 200)"),
    },
)
async def browser_get_accessibility(session, filter=None, viewport_only=False, max_elements=200):
    page = await session.page()
    session.refs, items = {}, []
    for frame in page.frames:
        try:
            found = await frame.evaluate(SNAPSHOT_JS, len(items) + 1)
        except PlaywrightError:
            continue  # a frame that went away or refuses scripts
        for item in found:
            session.refs[item["ref"]] = frame
        items.extend(found)

    needle = (filter or "").lower()
    lines = []
    for item in items:
        if viewport_only and item.get("offscreen"):
            continue
        if needle and needle not in f"{item['role']} {item['name']}".lower():
            continue
        line = f"[{item['ref']}] {item['role']} \"{item['name']}\""
        for key in ("value", "href"):
            if item.get(key):
                line += f" {key}={item[key]}"
        line += "".join(f" ({flag})" for flag in ("checked", "disabled", "offscreen") if item.get(flag))
        lines.append(line)

    if not lines:
        return "No matching elements. The page may still be loading (browser_wait_for) or be empty."
    shown = lines[:max_elements]
    more = len(lines) - len(shown)
    return "\n".join(shown) + (
        f"\n... {more} more not shown: narrow with filter or viewport_only." if more > 0 else ""
    )


@tool(
    "Read the page's text content, or its HTML. Use this to extract information.",
    {
        "target": _string("Read only this element (ref or selector). Default: the whole page"),
        "html": _boolean("Return HTML instead of visible text (default false)"),
        "frame": FRAME,
    },
)
async def browser_get_dom(session, target=None, html=False, frame=None):
    page = await session.page()
    if target:
        found = await session.element(page, target, frame)
    else:
        found = (session.frame(page, frame) if frame else page).locator("body")
    text = await (found.inner_html() if html else found.inner_text())
    return text.strip() or "(empty)"


@tool(
    "Run JavaScript in the page and return the result. The script is an expression "
    "or a function, e.g. `document.title` or `() => localStorage.getItem('token')`. "
    "With a target, the function receives that element: `el => el.dataset.id`.",
    {
        "script": _string("JavaScript expression or function"),
        "target": _string("Pass this element (ref or selector) to the function"),
        "frame": FRAME,
    },
    ["script"],
)
async def browser_evaluate(session, script, target=None, frame=None):
    page = await session.page()
    if target:
        value = await (await session.element(page, target, frame)).evaluate(script)
    else:
        value = await (session.frame(page, frame) if frame else page).evaluate(script)
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


# -------------------------------------------------------------- interaction

@tool(
    "Click an element. Waits for it to be visible and enabled, scrolls it into "
    "view, then waits for the page to settle.",
    {
        "target": TARGET,
        "button": _choice("Mouse button (default left)", "left", "right", "middle"),
        "double": _boolean("Double-click (default false)"),
    },
    ["target"],
)
async def browser_click(session, target, button="left", double=False):
    page = await session.page()
    found = await session.element(page, target)
    await found.click(button=button, click_count=2 if double else 1)
    await settle(await session.page())
    return f"Clicked {target}."


@tool(
    "Replace the content of a text input, textarea or editable element.",
    {
        "target": TARGET,
        "value": _string("The text to enter"),
        "submit": _boolean("Press Enter afterwards (default false)"),
        "slowly": _boolean("Type key by key, for fields that react to each keystroke such as autocomplete (default false)"),
    },
    ["target", "value"],
)
async def browser_fill(session, target, value, submit=False, slowly=False):
    page = await session.page()
    found = await session.element(page, target)
    if slowly:
        await found.fill("")
        await found.press_sequentially(value, delay=40)
    else:
        await found.fill(value)
    if submit:
        await found.press("Enter")
    await settle(await session.page())
    return f"Filled {target}{' and pressed Enter' if submit else ''}."


@tool(
    "Choose option(s) in a <select> dropdown, by visible label or by value.",
    {
        "target": TARGET,
        "values": {"type": "array", "items": {"type": "string"}, "description": "Labels or values to select"},
    },
    ["target", "values"],
)
async def browser_select(session, target, values):
    page = await session.page()
    found = await session.element(page, target)
    try:
        chosen = await found.select_option(label=values, timeout=3000)
    except PlaywrightError:
        chosen = await found.select_option(value=values)
    await settle(page)
    return f"Selected {chosen} in {target}."


@tool("Move the mouse over an element, to reveal menus and tooltips.", {"target": TARGET}, ["target"])
async def browser_hover(session, target):
    page = await session.page()
    await (await session.element(page, target)).hover()
    await page.wait_for_timeout(250)
    return f"Hovering over {target}."


@tool(
    "Press a key or key combination, e.g. Enter, Escape, Tab, ArrowDown, Control+A.",
    {
        "key": _string("Key name, with + for combinations"),
        "target": _string("Focus this element first (ref or selector). Default: whatever has focus"),
    },
    ["key"],
)
async def browser_press_key(session, key, target=None):
    page = await session.page()
    if target:
        await (await session.element(page, target)).press(key)
    else:
        await page.keyboard.press(key)
    await settle(await session.page())
    return f"Pressed {key}."


@tool(
    "Scroll the page. Use load_all on pages that load more as you scroll "
    "(infinite lists, lazy images): it scrolls until nothing new appears.",
    {
        "to": _choice("Direction or edge (default down)", "down", "up", "left", "right", "top", "bottom"),
        "pixels": _integer("Distance for a direction (default 80% of the viewport)"),
        "target": _string("Scroll this element (ref or selector) into view instead"),
        "load_all": _boolean("Keep scrolling to the bottom until the page stops growing"),
    },
)
async def browser_scroll(session, to="down", pixels=None, target=None, load_all=False):
    page = await session.page()
    size = page.viewport_size or session.viewport
    rounds = 0

    if target:
        await (await session.element(page, target)).scroll_into_view_if_needed()
    elif load_all:
        last, stable = -1, 0
        while rounds < 40 and stable < 2:
            height = await page.evaluate("document.documentElement.scrollHeight")
            stable = stable + 1 if height == last else 0
            last = height
            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(350)
            await quiet(page, 1500)
            rounds += 1
    elif to in ("top", "bottom"):
        edge = "0" if to == "top" else "document.documentElement.scrollHeight"
        await page.evaluate(f"window.scrollTo(0, {edge})")
    else:
        step = pixels or int(size["height" if to in ("down", "up") else "width"] * 0.8)
        dx, dy = {"down": (0, step), "up": (0, -step), "right": (step, 0), "left": (-step, 0)}[to]
        await page.mouse.move(size["width"] / 2, size["height"] / 2)
        await page.mouse.wheel(dx, dy)

    await page.wait_for_timeout(250)
    await quiet(page, 1500)
    at = await page.evaluate(
        "({y: Math.round(scrollY), view: innerHeight, height: document.documentElement.scrollHeight})"
    )
    bottom = at["y"] + at["view"] >= at["height"] - 2
    return (
        f"Showing {at['y']}-{at['y'] + at['view']} of {at['height']}px"
        f"{' (bottom of page)' if bottom else ''}."
        + (f" Scrolled {rounds} time(s) until no more content loaded." if load_all else "")
        + " New content needs a new browser_get_accessibility snapshot."
    )


@tool(
    "Drag one element onto another.",
    {"source": TARGET, "target": _string("Where to drop it (ref or selector)")},
    ["source", "target"],
)
async def browser_drag(session, source, target):
    page = await session.page()
    await (await session.element(page, source)).drag_to(await session.element(page, target))
    await settle(page)
    return f"Dragged {source} onto {target}."


@tool(
    "Low-level mouse control at viewport coordinates, for canvases and maps "
    "where there is no element to target.",
    {
        "x": _integer("X in px from the left of the viewport"),
        "y": _integer("Y in px from the top of the viewport"),
        "action": _choice("What to do (default click)", "move", "click", "down", "up"),
    },
    ["x", "y"],
)
async def browser_mouse(session, x, y, action="click"):
    page = await session.page()
    await page.mouse.move(x, y)
    if action == "click":
        await page.mouse.click(x, y)
    elif action in ("down", "up"):
        await getattr(page.mouse, action)()
    await settle(await session.page())
    return f"Mouse {action} at ({x}, {y})."


@tool(
    "Set the files of a file input (<input type=file>).",
    {
        "target": TARGET,
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Files to upload"},
    },
    ["target", "paths"],
)
async def browser_upload(session, target, paths):
    page = await session.page()
    files = [str(workspace.resolve(path)) for path in paths]
    missing = [path for path in files if not Path(path).is_file()]
    if missing:
        raise BrowserError(f"file(s) not found: {', '.join(missing)}")
    await (await session.element(page, target)).set_input_files(files)
    return f"Attached {len(files)} file(s) to {target}."


@tool(
    "Decide how JavaScript dialogs (confirm, prompt, beforeunload) are answered "
    "from now on. Alerts are always accepted. Dialogs are answered the moment "
    "they appear and reported in the next result, so set this BEFORE the action "
    "that opens the dialog. Default: dismiss.",
    {
        "action": _choice("How to answer", "accept", "dismiss"),
        "text": _string("Text to enter when accepting a prompt dialog"),
    },
    ["action"],
)
async def browser_handle_dialog(session, action, text=None):
    session.dialog_action, session.dialog_text = action, text
    return f"Dialogs will now be {action}ed" + (f' with "{text}".' if text else ".")


# ------------------------------------------------------------------ waiting

@tool(
    "Wait for something before continuing. Give exactly one of: target, text, "
    "url, load_state or seconds. Prefer a condition to a fixed number of seconds.",
    {
        "target": _string("Wait for this element (ref or selector)"),
        "state": _choice("With target: the state to wait for (default visible)",
                         "visible", "hidden", "attached", "detached"),
        "text": _string("Wait until this text is visible on the page"),
        "url": _string("Wait until the URL contains this"),
        "load_state": _choice("Wait for a load state", "load", "domcontentloaded", "networkidle"),
        "seconds": {"type": "number", "description": "Just wait this long (max 30)"},
        "timeout": {"type": "number", "description": "Give up after this many seconds (default: the configured action timeout)"},
        "frame": FRAME,
    },
)
async def browser_wait_for(session, target=None, state="visible", text=None, url=None,
                           load_state=None, seconds=None, timeout=None, frame=None):
    page = await session.page()
    limit = {"timeout": timeout * 1000} if timeout else {}
    try:
        if target:
            await session.locator(page, target, frame).first.wait_for(state=state, **limit)
            return f"{target} is {state}."
        if text:
            await (session.frame(page, frame) if frame else page).get_by_text(text).first.wait_for(**limit)
            return f'"{text}" is visible.'
        if url:
            await page.wait_for_url(lambda current: url in current, **limit)
            return f"URL now contains {url}."
        if load_state:
            await page.wait_for_load_state(load_state, **limit)
            return f"Page reached {load_state}."
    except PlaywrightTimeout:
        waited = target or text or url or load_state
        raise BrowserError(
            f"still waiting for {waited!r} when the time ran out. Inspect the page "
            "(browser_get_accessibility or browser_screenshot) before retrying."
        ) from None
    if seconds:
        await page.wait_for_timeout(min(float(seconds), 30) * 1000)
        return f"Waited {min(float(seconds), 30):g}s."
    raise BrowserError("give one of: target, text, url, load_state, seconds.")


# -------------------------------------------------------- screenshots, size

@tool(
    "Save a screenshot as a PNG file and return its path. To see what it shows, "
    "call read_file on that path.",
    {
        "target": _string("Capture only this element (ref or selector)"),
        "full_page": _boolean("Capture the whole scrollable page, not just the viewport"),
        "name": _string("File name for the image (default screenshot-N)"),
    },
)
async def browser_screenshot(session, target=None, full_page=False, name=None):
    page = await session.page()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / safe_name(name or f"screenshot-{len(list(ARTIFACTS.glob('*.png'))) + 1}", ".png")
    if target:
        await (await session.element(page, target)).screenshot(path=path)
    else:
        await page.screenshot(path=path, full_page=full_page)
    return f"Screenshot saved to {path}. Call read_file on it to get a description."


@tool(
    "Resize the viewport, e.g. 390x844 for a phone or 768x1024 for a tablet, to check responsive layouts.",
    {"width": _integer("Width in px"), "height": _integer("Height in px")},
    ["width", "height"],
)
async def browser_resize(session, width, height):
    page = await session.page()
    session.viewport = {"width": width, "height": height}
    await page.set_viewport_size(session.viewport)
    await settle(page)
    return f"Viewport is now {width}x{height}."


# --------------------------------------------------------------------- tabs

@tool("List the open tabs with their index, title and URL.")
async def browser_list_tabs(session):
    active = await session.page()
    lines = []
    for index, page in enumerate(session.pages, start=1):
        marker = "*" if page is active else " "
        lines.append(f"{marker} {index}. {await page.title() or '(no title)'} - {page.url}")
    return "\n".join(lines) + "\n(* = active tab)"


@tool("Open a new tab and make it active.", {"url": _string("Address to open in it (optional)")})
async def browser_new_tab(session, url=None):
    await session.page()
    await session.context.new_page()
    session.events = [e for e in session.events if "new tab opened" not in e]
    if url:
        return await browser_navigate(session, url)
    return f"Opened tab {len(session.pages)}."


@tool(
    "Make another tab the active one.",
    {"index": _integer("Tab number from browser_list_tabs")},
    ["index"],
)
async def browser_switch_tab(session, index):
    await session.page()
    if not 1 <= index <= len(session.pages):
        raise BrowserError(f"there is no tab {index}; {len(session.pages)} tab(s) are open.")
    session.active = session.pages[index - 1]
    session.refs = {}
    await session.active.bring_to_front()
    return f"Switched to tab {index}."


@tool(
    "Close a tab.",
    {"index": _integer("Tab number from browser_list_tabs (default: the active tab)")},
)
async def browser_close_tab(session, index=None):
    active = await session.page()
    if index is not None and not 1 <= index <= len(session.pages):
        raise BrowserError(f"there is no tab {index}; {len(session.pages)} tab(s) are open.")
    page = session.pages[index - 1] if index else active
    await page.close()
    session.refs = {}
    return "Tab closed."


# ----------------------------------------------------------------- debugging

@tool(
    "Read the browser console: logs, warnings, errors and uncaught page errors.",
    {
        "errors_only": _boolean("Only errors (default false)"),
        "clear": _boolean("Empty the log after reading (default false)"),
    },
)
async def browser_get_console(session, errors_only=False, clear=False):
    entries = [
        f"{kind}: {text}" for kind, text in session.console
        if not errors_only or kind in ("error", "pageerror")
    ][-60:]
    if clear:
        session.console.clear()
    return "\n".join(entries) or "The console is empty."


@tool(
    "List recent network requests with their status. Use it to find failed or "
    "slow calls and to learn which API a page talks to.",
    {
        "filter": _string("Only requests whose URL contains this"),
        "failed_only": _boolean("Only failures and HTTP 4xx/5xx (default false)"),
        "limit": _integer("How many of the most recent to show (default 40)"),
    },
)
async def browser_get_network(session, filter=None, failed_only=False, limit=40):
    lines = []
    for status, method, url, kind, failure in session.network:
        if filter and filter not in url:
            continue
        if failed_only and 0 < status < 400:
            continue
        lines.append(f"{status or 'FAILED'} {method} {url[:200]} [{kind}]" + (f" - {failure}" if failure else ""))
    return "\n".join(lines[-limit:]) or "No matching requests."


@tool(
    "Answer matching requests with a fake response instead of the real server, "
    "to test how a page behaves. Applies to requests made after this call.",
    {
        "url_pattern": _string("Glob for the request URL, e.g. **/api/users*"),
        "status": _integer("HTTP status to return (default 200). 0 makes the request fail"),
        "body": _string("Response body"),
        "content_type": _string("Content type (default application/json)"),
        "remove": _boolean("Remove this mock instead of adding it"),
    },
    ["url_pattern"],
)
async def browser_mock_network(session, url_pattern, status=200, body="",
                               content_type="application/json", remove=False):
    await session.page()
    if remove:
        if session.mocks.pop(url_pattern, None) is None:
            raise BrowserError(f"no mock for {url_pattern}. Active: {', '.join(session.mocks) or 'none'}")
        await session.context.unroute(url_pattern, session._fulfil)
        return f"Mock removed for {url_pattern}."
    if url_pattern not in session.mocks:
        await session.context.route(url_pattern, session._fulfil)
    session.mocks[url_pattern] = (status, body, content_type)
    return f"Requests matching {url_pattern} now get {'a network failure' if status == 0 else f'HTTP {status}'}."


@tool("Start recording a Playwright trace (screenshots, DOM snapshots, network) for debugging.")
async def browser_start_trace(session):
    await session.page()
    if session.tracing:
        raise BrowserError("a trace is already recording; stop it with browser_stop_trace.")
    await session.context.tracing.start(screenshots=True, snapshots=True)
    session.tracing = True
    return "Trace recording started."


@tool(
    "Stop recording and save the trace to a .zip file.",
    {"name": _string("File name for the trace (default trace)")},
)
async def browser_stop_trace(session, name="trace"):
    if not session.tracing:
        raise BrowserError("no trace is recording; start one with browser_start_trace.")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / safe_name(name, ".zip")
    await session.context.tracing.stop(path=path)
    session.tracing = False
    return f"Trace saved to {path}. View it with: uv run playwright show-trace {path}"


# ------------------------------------------------------------ authentication

@tool(
    "Save the session's cookies and local storage under a name, so a login can "
    "be reused later, also in a future run.",
    {"name": _string("Name for the saved state, e.g. github")},
    ["name"],
)
async def browser_save_state(session, name):
    await session.page()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / safe_name(name, ".json")
    await session.context.storage_state(path=path)
    path.chmod(0o600)
    return f"State saved as {path.stem!r}."


@tool(
    "Replace the session's cookies and local storage with a saved state, then reopen the current page.",
    {"name": _string("Name used with browser_save_state")},
    ["name"],
)
async def browser_load_state(session, name):
    path = STATE_DIR / safe_name(name, ".json")
    if not path.exists():
        saved = ", ".join(p.stem for p in STATE_DIR.glob("*.json")) or "none"
        raise BrowserError(f"no saved state named {name!r}. Saved states: {saved}")
    page = await session.page()
    url = page.url
    session.state = json.loads(path.read_text())
    await session.context.close()
    await session.open_context()
    if url and url != "about:blank":
        await session.active.goto(url)
    return f"State {name!r} loaded; open tabs were replaced by one tab."


@tool(
    "List the session's cookies (names and domains, not values) or clear them. "
    "Read localStorage with browser_evaluate.",
    {"action": _choice("What to do (default list)", "list", "clear")},
)
async def browser_cookies(session, action="list"):
    await session.page()
    if action == "clear":
        await session.context.clear_cookies()
        return "Cookies cleared."
    cookies = await session.context.cookies()
    lines = sorted(
        f"{c['domain']}  {c['name']}  ({'session' if c.get('expires', -1) < 0 else 'persistent'}"
        f"{', httpOnly' if c.get('httpOnly') else ''})"
        for c in cookies
    )
    return "\n".join(lines) or "No cookies."


# ------------------------------------------------------------------- testing

CHECKS = ("visible", "hidden", "enabled", "disabled", "checked", "text_contains",
          "value_equals", "count_equals", "url_contains", "title_contains")


@tool(
    "Check a condition and get PASS or FAIL with the actual value. Retries for "
    "a few seconds, so it tolerates content that is still arriving.",
    {
        "check": _choice("What to verify", *CHECKS),
        "target": _string("The element (ref or selector); not needed for url/title checks"),
        "expected": _string("Expected text, value or count, where the check needs one"),
    },
    ["check"],
)
async def browser_assert(session, check, target=None, expected=None):
    page = await session.page()
    if check not in CHECKS:
        raise BrowserError(f"unknown check {check!r}. Use one of: {', '.join(CHECKS)}")
    if check not in ("url_contains", "title_contains") and not target:
        raise BrowserError(f"{check} needs a target.")

    async def evaluate():
        if check == "url_contains":
            return expected in page.url, page.url
        if check == "title_contains":
            title = await page.title()
            return expected in title, title
        found = session.locator(page, target)
        count = await found.count()
        if check == "count_equals":
            return count == int(expected), count
        if check == "hidden":
            return count == 0 or not await found.first.is_visible(), "visible"
        if count == 0:
            return False, "no such element"
        first = found.first
        if check == "visible":
            return await first.is_visible(), "hidden"
        if check == "enabled":
            return await first.is_enabled(), "disabled"
        if check == "disabled":
            return await first.is_disabled(), "enabled"
        if check == "checked":
            return await first.is_checked(), "not checked"
        if check == "value_equals":
            value = await first.input_value()
            return value == expected, value
        text = await first.inner_text()
        return expected in text, text[:200]

    passed, actual = False, None
    for _ in range(20):  # about 5 seconds
        passed, actual = await evaluate()
        if passed:
            break
        await page.wait_for_timeout(250)

    subject = f"{check}" + (f" {target}" if target else "") + (f" = {expected!r}" if expected is not None else "")
    return f"PASS: {subject}" if passed else f"FAIL: {subject}. Actual: {actual!r}"
