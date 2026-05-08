import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

t0 = time.time()
import builtins as _b
def log(*a, **k):
    _b.print(f'[{int(time.time()-t0):4d}s]', *a, **k, flush=True)

OUT = Path(os.environ['COMFY_TEST_LOGS_DIR'].replace('\\', '/')) / 'electron_inspect'
FRAMES = OUT / 'frames'
OUT.mkdir(parents=True, exist_ok=True)
FRAMES.mkdir(parents=True, exist_ok=True)
fi = [0]

# Per-workflow results that get rolled up into results.json at the
# artifact root. comfy-test's generate_html_report() reads this file
# to build the per-platform index.html on gh-pages.
_workflow_results = []

def snap(page, name):
    try:
        page.screenshot(path=str(OUT / f'{name}.png'), full_page=True)
        (OUT / f'{name}.html').write_text(page.content(), encoding='utf-8')
    except Exception as e:
        log(f'  snap {name}: {e}')

def frame(page):
    try:
        fi[0] += 1
        page.screenshot(path=str(FRAMES / f'frame_{fi[0]:06d}.png'))
    except Exception:
        pass

def sleep_capturing(page, seconds, fps=5):
    interval = 1.0 / fps
    end = time.time() + seconds
    while time.time() < end:
        frame(page)
        time.sleep(interval)

def buttons(page):
    try:
        return page.eval_on_selector_all(
            'button, a[role=button], [role=button], input[type=submit], input[type=button]',
            "els => els.map(e => ({text:(e.innerText||e.value||'').trim(), tag:e.tagName, id:e.id, cls:e.className, disabled:!!(e.disabled||e.getAttribute('aria-disabled')==='true'||e.getAttribute('disabled')!==null)}))"
        )
    except Exception:
        return []

def main_page(browser):
    cands = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                u = pg.url or ''
                if u.startswith('devtools://'):
                    continue
                t = pg.title()
            except Exception:
                continue
            cands.append((pg, u, t))
    for pg, u, t in cands:
        if any(k in (t or '') for k in ('ComfyUI', 'Maintenance')) or 'maintenance' in (u or '').lower():
            return pg
    return cands[0][0] if cands else None

try:
    tg = json.loads(urllib.request.urlopen('http://localhost:9222/json').read())
    (OUT / 'targets.json').write_text(json.dumps(tg, indent=2))
    log(f'CDP targets: {len(tg)}')
    for t in tg:
        log(f"  {t.get('type')}: {t.get('url')} | {t.get('title')}")
except Exception as e:
    log(f'targets list: {e}')

# Visible cursor injected into the page so the captured video shows
# where the driver clicks. The CSS transform transitions over 300ms,
# so move-then-wait-then-click looks like a smooth pointer move.
CURSOR_JS = r'''
(() => {
  if (document.getElementById('__fake_cursor')) return;
  const c = document.createElement('div');
  c.id = '__fake_cursor';
  c.style.cssText = [
    'position:fixed','top:0','left:0','width:28px','height:28px',
    'pointer-events:none','z-index:2147483647',
    'transition:transform 300ms cubic-bezier(.4,0,.2,1),filter 120ms',
    'transform:translate(40px,40px)',
    "background:url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M3 2 L3 19 L8 14 L11 21 L14 20 L11 13 L18 13 Z' fill='black' stroke='white' stroke-width='1.5'/></svg>\") no-repeat center / contain"
  ].join(';');
  document.documentElement.appendChild(c);
  window.__moveCursor = (x, y) => { c.style.transform = `translate(${x}px,${y}px)`; };
  window.__clickFlash  = () => { c.style.filter = 'drop-shadow(0 0 8px #4af) brightness(1.4)'; setTimeout(()=>c.style.filter='',180); };
})();
'''

def install_cursor(page):
    try:
        page.evaluate(CURSOR_JS)
    except Exception as e:
        log(f'  cursor inject failed: {e}')

def click_with_cursor(page, loc, timeout=3000):
    try:
        box = loc.bounding_box()
        if box:
            cx = box['x'] + box['width']/2
            cy = box['y'] + box['height']/2
            page.evaluate(f'window.__moveCursor && window.__moveCursor({cx}, {cy})')
            time.sleep(0.4)
            page.evaluate('window.__clickFlash && window.__clickFlash()')
    except Exception:
        pass
    loc.click(timeout=timeout)

def fill_with_cursor(page, sel, text):
    loc = page.locator(sel).first
    if not loc.count() or not loc.is_visible():
        return False
    try:
        box = loc.bounding_box()
        if box:
            cx = box['x'] + box['width']/2
            cy = box['y'] + box['height']/2
            page.evaluate(f'window.__moveCursor && window.__moveCursor({cx}, {cy})')
            time.sleep(0.4)
    except Exception:
        pass
    try:
        loc.click(timeout=3000)
        loc.fill('')
        loc.type(text, delay=80)
        return True
    except Exception:
        return False

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp('http://localhost:9222')
    page = main_page(browser)
    if not page:
        log('No usable page')
        sys.exit(0)
    log(f'Main page: {page.url} | {page.title()}')
    install_cursor(page)
    snap(page, 'initial')
    btns = buttons(page)
    (OUT / 'initial_buttons.json').write_text(json.dumps(btns, indent=2))
    log(f"Buttons: {[b['text'] for b in btns]}")
    frame(page)

    # Click-through wizard loop. We don't pre-seed any config, so the
    # app boots into the welcome → GPU → path → install → telemetry
    # consent flow. We poll /system_stats; meanwhile we click whatever
    # primary action is on screen, in priority order: confirm popover
    # accept > raised button with a known label > any button with that
    # label. We track signatures so the same button on the same URL
    # only gets clicked once; URL changes reset the set.
    PRIMARY_LABELS = ['Get Started', 'Next', 'Continue', 'Install', 'OK',
                      'Recreate', 'Confirm', 'Accept', 'Allow', 'Yes', 'Finish']

    def server_up():
        try:
            urllib.request.urlopen('http://127.0.0.1:8000/system_stats', timeout=2)
            return True
        except Exception:
            return False

    def find_action(page):
        # Confirm popover (e.g., "Delete .venv" → Recreate accept)
        try:
            loc = page.locator('button.p-confirmpopup-accept-button').first
            if loc.count() and loc.is_visible():
                return ('confirm', loc, f'CONFIRM|{(loc.text_content() or "").strip()}')
        except Exception:
            pass
        # Exact-text primary buttons (exclude hardware tiles, must be visible+enabled).
        # :text-is is exact match; :has-text is substring (catches tiles by accident).
        for label in PRIMARY_LABELS:
            for sel in (f'button.p-button-raised:text-is("{label}")',
                        f'button:not(.hardware-option):text-is("{label}")',
                        f'button[aria-label="{label}"]:not(.hardware-option)'):
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible() and not loc.is_disabled():
                        return ('btn', loc, f'BTN|{label}')
                except Exception:
                    pass
        # Hardware-tile fallback: GPU picker disables the bottom Install
        # button until a tile is clicked. Order matters: macOS-friendly
        # tiles first so Apple Silicon wins on macOS; Windows runners
        # have no GPU and fall through to CPU (NVIDIA-default would
        # install cu130 torch which crashes at startup on a CPU box).
        PREFERRED = ['Apple Silicon', 'MPS', 'M1', 'M2', 'M3', 'M4', 'CPU']
        for pref in PREFERRED:
            try:
                tile = page.locator(f'button.hardware-option:has-text("{pref}")').first
                if tile.count() and tile.is_visible():
                    return ('tile', tile, f'TILE|{pref}')
            except Exception:
                pass
        try:
            tile = page.locator('button.hardware-option:not([aria-disabled="true"])').first
            if tile.count() and tile.is_visible():
                name = (tile.get_attribute('aria-label') or tile.text_content() or 'tile').strip()[:40]
                return ('tile', tile, f'TILE|{name}')
        except Exception:
            pass
        return None

    clicked = {}  # sig -> last_click_time; allow re-click after CLICK_TTL
    CLICK_TTL = 5
    page_url = page.url
    start = time.time()
    deadline = start + 1500  # 25min cap
    while time.time() < deadline:
        frame(page)
        if server_up():
            log(f'  /system_stats up after {int(time.time()-start)}s')
            break
        new_url = page.url
        if new_url != page_url:
            log(f'  url: {page_url} -> {new_url}')
            page_url = new_url
            clicked.clear()
            install_cursor(page)  # SPA routes may rebuild DOM
        found = find_action(page)
        if found:
            kind, loc, sig = found
            last_t = clicked.get(sig, 0)
            if time.time() - last_t < CLICK_TTL:
                time.sleep(1); continue
            try:
                click_with_cursor(page, loc)
                log(f'  clicked [{sig}]')
                clicked[sig] = time.time()
                sleep_capturing(page, 1, fps=4)
            except Exception as e:
                log(f'  click [{sig}] failed: {e}')
                clicked[sig] = time.time()
        else:
            time.sleep(2)
    else:
        log(f'  driver timed out after {int(time.time()-start)}s without /system_stats')

    # Keep capturing past server-up so the main canvas/UI lands in
    # the video, then drive a short post-flow: dismiss the cloud
    # upsell, close the workflow templates dialog, open Extensions.
    # Each step tolerates the element not being there (Windows for
    # example skips the cloud upsell).
    log('  capturing canvas load...')
    sleep_capturing(page, 8, fps=5)
    # :visible pseudo-class excludes hidden buttons. There are
    # several `aria-label="Close"` X icons in the page (sidebar
    # accordion items render hidden ones); without :visible the
    # `.first` would pick a hidden one and the click is a no-op.
    # The "Run ComfyUI in the Cloud?" upsell dialog can render with any
    # of several button copies depending on Desktop version. Try them
    # all, longer deadline (modal can render >8s after server-up on a
    # CI runner), and Escape fallback so a stuck modal doesn't block
    # downstream Extensions/Templates steps.
    POST_ACTIONS = [
        ('Cloud upsell',  'cloud',
         ['button:has-text("Continue Locally"):visible',
          'button:has-text("Use Local"):visible',
          'button:has-text("Stay Local"):visible',
          'button:has-text("Run Locally"):visible',
          'button:has-text("Local Install"):visible',
          'button:has-text("No thanks"):visible',
          'button:has-text("Skip"):visible',
          'button:has-text("Maybe later"):visible',
          'button[aria-label="Close"]:visible'],
         30),
        ('Close Templates', 'templates',
         ['button[aria-label="Close"]:visible'], 8),
        ('Extensions', 'extensions',
         ['button[aria-label="Extensions"]:visible'], 8),
    ]
    for name, kind, selectors, secs in POST_ACTIONS:
        log(f'  post: waiting for {name}')
        deadline = time.time() + secs
        hit = False
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible() and not loc.is_disabled():
                        click_with_cursor(page, loc)
                        log(f'  post: clicked {name} via [{sel}]')
                        sleep_capturing(page, 2, fps=5)
                        hit = True
                        break
                except Exception:
                    pass
            if hit:
                break
            sleep_capturing(page, 0.5, fps=5)
        if not hit:
            log(f'  post: {name} not found, skipping')
            if kind in ('templates', 'cloud'):
                try:
                    page.keyboard.press('Escape')
                    log(f'  post: pressed Escape ({name} fallback)')
                    sleep_capturing(page, 2, fps=5)
                except Exception:
                    pass

    # Extensions search → pick tile → choose pinned version → Install.
    # Pull DisplayName / PublisherId / version from the node repo's
    # pyproject.toml at https://raw.githubusercontent.com/<repo>/<branch>/.
    # If that fails we can't reliably target the right tile, so bail
    # out of the search-install flow rather than guess.
    def _fetch_node_meta():
        repo = os.environ.get('NODE_REPO', '')
        branch = os.environ.get('NODE_BRANCH', 'main')
        if not repo:
            return None, None, None
        url = f'https://raw.githubusercontent.com/{repo}/{branch}/pyproject.toml'
        try:
            body = urllib.request.urlopen(url, timeout=10).read().decode('utf-8')
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore
            data = tomllib.loads(body)
            comfy = data.get('tool', {}).get('comfy', {})
            return (comfy.get('DisplayName'),
                    comfy.get('PublisherId'),
                    data.get('project', {}).get('version'))
        except Exception as e:
            log(f'  ext: pyproject.toml fetch/parse failed: {e}')
            return None, None, None

    NODE_DISPLAY_NAME, PUBLISHER, NODE_VERSION = _fetch_node_meta()
    log(f'  ext: node meta = display={NODE_DISPLAY_NAME!r} publisher={PUBLISHER!r} version={NODE_VERSION!r}')
    if not NODE_DISPLAY_NAME or not PUBLISHER:
        # Existing `if not clicked_tile` branch will log + skip the rest.
        NODE_DISPLAY_NAME = NODE_DISPLAY_NAME or '__no_match_should_ever__'
        PUBLISHER = PUBLISHER or '__no_match_should_ever__'
    sleep_capturing(page, 3, fps=5)
    log(f'  ext: searching "{NODE_DISPLAY_NAME}"')
    fill_with_cursor(page, 'input[placeholder="Search"]:visible', NODE_DISPLAY_NAME)
    sleep_capturing(page, 2, fps=5)

    log(f'  ext: clicking {NODE_DISPLAY_NAME} by {PUBLISHER} tile')
    # Match both display name AND publisher in the same tile.
    # Just :has-text("pznodes") is ambiguous — other tiles list
    # pznodes as a related/compatible pack and matched first.
    tile_sel = (f'div.bg-modal-card-background.cursor-pointer'
                f':has-text("{NODE_DISPLAY_NAME}")'
                f':has-text("{PUBLISHER}"):visible')
    deadline = time.time() + 8
    clicked_tile = False
    while time.time() < deadline:
        try:
            tile = page.locator(tile_sel).first
            if tile.count() and tile.is_visible():
                click_with_cursor(page, tile)
                clicked_tile = True
                break
        except Exception:
            pass
        sleep_capturing(page, 0.5, fps=5)
    if not clicked_tile:
        log('  ext: tile not found, ending capture')
    else:
        sleep_capturing(page, 3, fps=5)

        # Version selector lives in the right panel below the
        # ACTIONS/Basic Info accordions — usually below the fold.
        # It's a <div role="button" aria-haspopup="true"> currently
        # displaying e.g. "nightly". scroll_into_view_if_needed
        # auto-scrolls the right panel's overflow-y container.
        log('  ext: scrolling to + opening version selector')
        try:
            vt = page.locator('div[role="button"][aria-haspopup="true"].bg-dialog-surface:visible').first
            if vt.count():
                vt.scroll_into_view_if_needed()
                sleep_capturing(page, 1, fps=5)
                click_with_cursor(page, vt)
                sleep_capturing(page, 1, fps=5)
                # Try the pinned CNR version first; fall back to
                # Latest then Nightly. "Latest" in this UI is an
                # alias Manager dispatches as literal "@latest",
                # which CNR can't always resolve — but it's the
                # next-best signal. Nightly is git-main tracking.
                picked = False
                version_labels = tuple(filter(None, (NODE_VERSION, 'Latest', 'Nightly')))
                for label in version_labels:
                    try:
                        opt = page.locator(
                            f'[role="option"]:has-text("{label}"):visible, '
                            f'[role="menuitem"]:has-text("{label}"):visible, '
                            f'li:has-text("{label}"):visible').first
                        if opt.count():
                            click_with_cursor(page, opt)
                            log(f'  ext: selected {label}')
                            sleep_capturing(page, 1, fps=5)
                            picked = True
                            break
                    except Exception:
                        pass
                if not picked:
                    log('  ext: no version option matched, dismissing')
                    try: page.keyboard.press('Escape')
                    except Exception: pass
        except Exception as e:
            log(f'  ext: version selector failed: {e}')

        # Right-panel Install button: it's the LAST "Install"
        # button in DOM order (each middle-column tile also has
        # an inline Install with the same is-installing attr;
        # picking .first there installs the wrong package).
        log('  ext: clicking right-panel Install')
        try:
            btns = page.locator('button:has-text("Install"):visible')
            n = btns.count()
            if n:
                btn = btns.nth(n - 1)
                btn.scroll_into_view_if_needed()
                sleep_capturing(page, 1, fps=5)
                click_with_cursor(page, btn)
                log(f'  ext: clicked Install (last of {n} visible)')
            else:
                log('  ext: no visible Install button')
        except Exception as e:
            log(f'  ext: install click failed: {e}')
        sleep_capturing(page, 8, fps=5)

        # After install fires, ComfyUI shows a bottom toast:
        # "To apply changes, please restart ComfyUI" with an
        # "Apply Changes" button. Clicking it restarts the
        # backend so the newly-extracted node loads.
        # Wait long enough for nodes whose install.py runs `pixi install
        # --all` over several isolation envs (CADabra, GeometryPack) to
        # finish before the toast appears. Killing during pixi install
        # leaves partial envs that crash the next boot's metadata scan.
        log('  ext: waiting for "Apply Changes" toast')
        applied = False
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                ac = page.locator('button:has-text("Apply Changes"):visible').first
                if ac.count() and ac.is_visible() and not ac.is_disabled():
                    click_with_cursor(page, ac)
                    log('  ext: clicked Apply Changes')
                    applied = True
                    break
            except Exception:
                pass
            sleep_capturing(page, 1, fps=5)
        if not applied:
            log('  ext: Apply Changes toast not seen, skipping')
        # Capture the in-app backend restart for a few seconds, then
        # do a hard close-and-reopen of the whole Electron app — the
        # Templates panel caches its node-pack list at app startup
        # and won't pick up newly-installed packs without a full
        # relaunch. Only force-kill when we never saw Apply Changes:
        # if it was clicked, the in-app restart already happened, and
        # killing now would race the freshly-relaunched python server.
        sleep_capturing(page, 5, fps=5)

        IS_WIN = sys.platform == 'win32'
        if not applied:
            log('  app: killing ComfyUI to force full relaunch')
            try: browser.close()
            except Exception: pass
            try:
                if IS_WIN:
                    subprocess.run(['taskkill', '/F', '/IM', 'ComfyUI.exe'],
                                   capture_output=True, timeout=10)
                else:
                    subprocess.run(['pkill', '-f', 'ComfyUI'],
                                   capture_output=True, timeout=10)
            except Exception as e:
                log(f'  app: kill error: {e}')
            time.sleep(5)
        else:
            log('  app: Apply Changes already triggered in-app restart, skipping pkill')
            try: browser.close()
            except Exception: pass
            time.sleep(5)

        log('  app: relaunching with CDP')
        # Send app stdout/stderr to /dev/null on relaunch; same reason as
        # the bash launch — uv's progress dumps tons of noise.
        if IS_WIN:
            app_exe = os.path.join(os.environ['LOCALAPPDATA'],
                                   'Programs', 'ComfyUI', 'ComfyUI.exe')
            subprocess.Popen([app_exe, '--remote-debugging-port=9222'],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
        else:
            ws = os.environ.get('GITHUB_WORKSPACE', '')
            app_path = os.path.join(ws, 'ComfyUI.app')
            subprocess.Popen(['open', app_path, '--args', '--remote-debugging-port=9222'],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

        log('  app: waiting for CDP')
        for i in range(120):
            try:
                urllib.request.urlopen('http://localhost:9222/json/version', timeout=1)
                log(f'  app: CDP up after {i+1}s')
                break
            except Exception:
                time.sleep(1)

        log('  app: reconnecting Playwright')
        browser = p.chromium.connect_over_cdp('http://localhost:9222')
        page = main_page(browser)
        if page is None:
            log('  app: no page after relaunch, bailing')
        else:
            install_cursor(page)
            log(f'  app: attached to {page.url}')
            for i in range(180):
                if server_up():
                    log(f'  app: server ready after {i+1}s')
                    break
                frame(page)
                time.sleep(1)
            # Server is up but the renderer might still be on a splash
            # (#/desktop-start, #/server-start) or — on Windows when
            # there's no GPU — #/not-supported. Worse, custom-node
            # install scripts (e.g. SAM3) restart the python server,
            # which leaves the renderer stuck on the splash because the
            # IPC channel got reset while the renderer was waiting.
            #
            # Plan: wait for window.app.graph; every ~5s try clicking
            # past splash buttons; at 60s force a page.reload to recover
            # from the post-install python-restart state.
            log('  app: waiting for main canvas (window.app.graph)')
            reloaded_once = False
            for i in range(240):
                try:
                    ready = page.evaluate(
                        "typeof window.app !== 'undefined' "
                        "&& window.app.graph !== undefined")
                    if ready:
                        log(f'  app: canvas ready after {i+1}s ({page.url})')
                        break
                except Exception:
                    pass
                if i % 5 == 0:
                    for label in ('Continue', 'Get Started', 'Next', 'OK'):
                        try:
                            b = page.locator(
                                f'button:not(.hardware-option):text-is("{label}"):visible'
                            ).first
                            if b.count() and b.is_visible() and not b.is_disabled():
                                click_with_cursor(page, b)
                                log(f'  app: clicked [{label}] to dismiss splash ({page.url})')
                                break
                        except Exception:
                            pass
                if i == 60 and not reloaded_once:
                    reloaded_once = True
                    log('  app: canvas not ready in 60s, reloading page')
                    try:
                        page.reload(wait_until='load', timeout=30000)
                        install_cursor(page)
                    except Exception as e:
                        log(f'  app: reload failed: {e}')
                frame(page)
                time.sleep(1)
            else:
                log(f'  app: canvas never became ready (still at {page.url})')
            sleep_capturing(page, 5, fps=5)

        # Post-restart: close Nodes Manager (may not exist), open Templates sidebar.
        log('  ext: closing Nodes Manager dialog')
        try:
            cd = page.locator('button[aria-label="Close dialog"]:visible').first
            if cd.count():
                click_with_cursor(page, cd)
                log('  ext: clicked Close dialog')
                sleep_capturing(page, 2, fps=5)
        except Exception as e:
            log(f'  ext: Close dialog failed: {e}')

        # Restart pops a "What's New" release-notes overlay (Vue
        # component .whats-new-popup) sitting right over the
        # Templates sidebar button. Dismiss it first.
        log("  ext: dismissing What's New popup")
        try:
            wn = page.locator('.whats-new-popup button[aria-label="Close"]:visible, .whats-new-popup button.close-button:visible').first
            if wn.count():
                click_with_cursor(page, wn)
                log("  ext: closed What's New popup")
                sleep_capturing(page, 2, fps=5)
            else:
                log("  ext: What's New popup not present")
        except Exception as e:
            log(f"  ext: What's New close failed: {e}")

        # On post-install relaunch, ComfyUI Desktop sometimes shows
        # "Node Pack Issues Detected!" — a Vue modal warning about
        # extension conflicts with the new ComfyUI version. It sits over
        # the canvas and intercepts clicks on the Templates sidebar
        # button. Dismiss before opening Templates.
        log("  ext: dismissing Node Pack Issues modal (if present)")
        try:
            np_modal = page.locator(
                'div[role="dialog"]:has-text("Node Pack Issues") button[aria-label="Close"]:visible, '
                'div[role="dialog"]:has-text("Node Pack Issues") button[aria-label="Close dialog"]:visible'
            ).first
            if np_modal.count():
                click_with_cursor(page, np_modal)
                log("  ext: closed Node Pack Issues modal")
                sleep_capturing(page, 2, fps=5)
            else:
                log("  ext: Node Pack Issues modal not present")
        except Exception as e:
            log(f"  ext: Node Pack Issues close failed: {e}")

        log('  ext: opening Templates sidebar')
        try:
            tpl = page.locator('button[aria-label="Templates"]:visible').first
            if tpl.count():
                # Bump click timeout above the default 3s; in some
                # post-restart states the button settles into its
                # final hit area only after a brief layout pass.
                click_with_cursor(page, tpl, timeout=10000)
                log('  ext: clicked Templates')
                sleep_capturing(page, 4, fps=5)
            else:
                log('  ext: Templates sidebar button not found')
        except Exception as e:
            log(f'  ext: Templates click failed: {e}')

        # Templates panel sections are keyed off the lowercase
        # package name (e.g. "comfyui-sam3"), matching the
        # custom_nodes/ directory the install creates.
        NODE_PACKAGE_NAME = os.environ.get('NODE_NAME', 'comfyui-sam3').lower()
        log(f'  ext: locating "{NODE_PACKAGE_NAME}" section in Templates panel')
        node_section = None
        # The Templates panel left sidebar is a <nav> with an inner
        # `div.scrollbar-hide.overflow-y-auto` that holds the category
        # list. Each category is a `<div role="button">`. We scroll
        # THAT inner div, not the aside/dialog wrapper.
        candidates = [
            f'nav [role="button"]:has-text("{NODE_PACKAGE_NAME}")',
            f'nav span:has-text("{NODE_PACKAGE_NAME}")',
            f'nav button:has-text("{NODE_PACKAGE_NAME}")',
        ]
        def find_node_section():
            for sel in candidates:
                loc = page.locator(sel).first
                if loc.count():
                    return loc, sel
            return None, None
        try:
            node_section, hit_sel = find_node_section()
            if node_section is None:
                panel = page.locator('nav .scrollbar-hide.overflow-y-auto:visible, nav .overflow-y-auto:visible').first
                for i in range(25):
                    try:
                        if panel.count():
                            panel.evaluate('el => el.scrollBy(0, el.clientHeight * 0.7)')
                        else:
                            page.keyboard.press('PageDown')
                    except Exception:
                        try: page.keyboard.press('PageDown')
                        except Exception: pass
                    sleep_capturing(page, 1, fps=5)
                    node_section, hit_sel = find_node_section()
                    if node_section is not None:
                        log(f'  ext: scrolled {i+1}x to {NODE_PACKAGE_NAME} ({hit_sel})')
                        break
            if node_section is not None:
                node_section.scroll_into_view_if_needed()
                sleep_capturing(page, 1, fps=5)
                click_with_cursor(page, node_section)
                log(f'  ext: clicked {NODE_PACKAGE_NAME} section')
                sleep_capturing(page, 2, fps=5)
            else:
                log(f'  ext: {NODE_PACKAGE_NAME} section not found after scrolling')
        except Exception as e:
            log(f'  ext: {NODE_PACKAGE_NAME} section click failed: {e}')

        # Pick the first CPU-compatible template per the node repo's
        # comfy-test.toml [test.workflows].cpu spec. Mirrors
        # comfy-test/src/comfy_test/common/config_file.py:resolve_workflows
        #   - cpu = "all"            → any card
        #   - cpu = ["a","b"]        → only "a" or "b"
        #   - cpu = ["!a"] (any !)   → any card except those listed
        if node_section is not None:
            cpu_mode = 'all'   # 'all' | 'include' | 'exclude'
            cpu_items = []     # list of workflow names (without .json)
            try:
                node_repo = os.environ.get('NODE_REPO', '')
                node_branch = os.environ.get('NODE_BRANCH', 'main')
                if node_repo:
                    toml_url = f'https://raw.githubusercontent.com/{node_repo}/{node_branch}/comfy-test.toml'
                    log(f'  ext: fetching comfy-test.toml from {toml_url}')
                    toml_text = urllib.request.urlopen(toml_url, timeout=10).read().decode('utf-8')
                    try:
                        import tomllib
                    except ImportError:
                        import tomli as tomllib  # type: ignore
                    data = tomllib.loads(toml_text)
                    cpu = data.get('test', {}).get('workflows', {}).get('cpu')
                    if cpu == 'all' or cpu is None:
                        cpu_mode = 'all'
                    elif isinstance(cpu, list):
                        excludes = [f.lstrip('!') for f in cpu if isinstance(f, str) and f.startswith('!')]
                        if excludes:
                            cpu_mode = 'exclude'
                            cpu_items = [e[:-5] if e.endswith('.json') else e for e in excludes]
                        else:
                            cpu_mode = 'include'
                            cpu_items = [f[:-5] if f.endswith('.json') else f for f in cpu]
                    log(f'  ext: cpu spec = {cpu_mode} {cpu_items}')
            except Exception as e:
                log(f'  ext: comfy-test.toml fetch/parse failed ({e}); defaulting to all')

            log(f'  ext: picking first matching {NODE_PACKAGE_NAME} template')
            picked_card = None
            picked_name = None
            try:
                cards = page.locator('[data-testid^="template-workflow-"]:visible')
                n = cards.count()
                log(f'  ext: {n} visible cards')
                for i in range(n):
                    c = cards.nth(i)
                    tid = c.get_attribute('data-testid') or ''
                    name = tid[len('template-workflow-'):] if tid.startswith('template-workflow-') else tid
                    if cpu_mode == 'all' or \
                       (cpu_mode == 'include' and name in cpu_items) or \
                       (cpu_mode == 'exclude' and name not in cpu_items):
                        picked_card = c
                        picked_name = name
                        break
                    else:
                        log(f'  ext: skipping {name} (not in CPU list)')
                if picked_card is not None:
                    picked_card.scroll_into_view_if_needed()
                    sleep_capturing(page, 1, fps=5)
                    click_with_cursor(page, picked_card)
                    log(f'  ext: clicked template {picked_name}')
                    sleep_capturing(page, 5, fps=5)
                else:
                    log('  ext: no CPU-eligible template card found')
            except Exception as e:
                log(f'  ext: template click failed: {e}')

            # Hook the page's existing WebSocket BEFORE clicking Run.
            # Same approach as comfy-test/src/comfy_test/reporting/screenshot.py:
            # intercept window.app.api.socket.onmessage; flag completion on
            # execution_success / execution_error / execution_interrupted.
            log('  ext: installing WS execution listener')
            try:
                page.evaluate(r"""
                    window._executionComplete = false;
                    window._executionError = null;
                    window._executionEvents = [];
                    if (window.app && window.app.api && window.app.api.socket) {
                        const origOnMessage = window.app.api.socket.onmessage;
                        window.app.api.socket.onmessage = function(event) {
                            if (origOnMessage) {
                                try { origOnMessage.call(this, event); } catch(e) {}
                            }
                            if (event && typeof event.data === 'string') {
                                try {
                                    const msg = JSON.parse(event.data);
                                    window._executionEvents.push({type: msg.type, ts: Date.now()});
                                    if (msg && msg.type === 'execution_success') {
                                        window._executionComplete = true;
                                    } else if (msg && msg.type === 'execution_error') {
                                        window._executionError = msg.data;
                                        window._executionComplete = true;
                                    } else if (msg && msg.type === 'execution_interrupted') {
                                        window._executionError = msg.data || 'Execution interrupted';
                                        window._executionComplete = true;
                                    }
                                } catch (e) {}
                            }
                        };
                    } else {
                        window._executionError = 'window.app.api.socket not available';
                    }
                """)
            except Exception as e:
                log(f'  ext: WS listener install failed: {e}')

            log('  ext: clicking Run')
            try:
                run_btn = page.locator(
                    'button[aria-label="Run"]:visible, '
                    'button:has-text("Run"):visible'
                ).first
                if run_btn.count():
                    click_with_cursor(page, run_btn)
                    log('  ext: clicked Run')
                else:
                    log('  ext: Run button not found')
            except Exception as e:
                log(f'  ext: Run click failed: {e}')

            # Wait for execution_success / execution_error from the WS.
            log('  ext: waiting for execution_success / execution_error')
            run_deadline = time.time() + 600
            run_start = time.time()
            while time.time() < run_deadline:
                frame(page)
                try:
                    complete = page.evaluate('window._executionComplete')
                except Exception:
                    complete = False
                if complete:
                    break
                time.sleep(0.5)
            elapsed = int(time.time() - run_start)
            try:
                events = page.evaluate('window._executionEvents') or []
                err = page.evaluate('window._executionError')
            except Exception:
                events, err = [], None
            log(f'  ext: WS events={len(events)} elapsed={elapsed}s')
            for ev in events[-15:]:
                log(f'    ws: {ev}')
            if err:
                # err is the raw msg.data from execution_error — typically
                # has node_type, exception_type, exception_message, traceback.
                try:
                    log('  ext: execution_error data:')
                    log(json.dumps(err, indent=2, default=str))
                except Exception:
                    log(f'  ext: execution_error (non-serializable): {err!r}')
            elif elapsed >= 600:
                log('  ext: WORKFLOW TIMEOUT (no execution_success/error in 10min)')
            else:
                log(f'  ext: execution_success after {elapsed}s')

            # Record this workflow's outcome for results.json.
            if err:
                _status = 'fail'
                _err_str = json.dumps(err, default=str) if err else None
            elif elapsed >= 600:
                _status = 'timeout'
                _err_str = 'no execution_success/error in 10min'
            else:
                _status = 'pass'
                _err_str = None
            _workflow_results.append({
                'name': picked_name or 'unknown_template',
                'status': _status,
                'duration_seconds': elapsed,
                'error': _err_str,
            })
            sleep_capturing(page, 5, fps=5)

    snap(page, 'final')
    log(f'Captured {fi[0]} frames')
    browser.close()

# Write results.json at the artifact root (one level above electron_inspect/).
# comfy-test's generate_html_report() requires this to render the per-
# platform index.html on gh-pages. Schema matches what the framework
# emits on other platforms: {"workflows": [{name, status, duration_seconds, error}]}.
# A synthetic 'system' row is prepended by the platform YML's "Synthetic
# system workflow" step (with hardware metadata), so we don't add one here.
_results_path = OUT.parent / 'results.json'
try:
    _results_path.write_text(json.dumps({'workflows': _workflow_results}, indent=2),
                             encoding='utf-8')
    log(f'Wrote {_results_path} ({len(_workflow_results)} workflow(s))')
except Exception as e:
    log(f'results.json write failed: {e}')

# imageio-ffmpeg ships a static ffmpeg binary so we don't need a system install.
try:
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as e:
    log(f'imageio-ffmpeg unavailable ({e}); falling back to PATH ffmpeg')
    ffmpeg_exe = 'ffmpeg'

mp4 = OUT / 'driver.mp4'
try:
    subprocess.run([
        ffmpeg_exe, '-y', '-framerate', '5',
        '-i', str(FRAMES / 'frame_%06d.png'),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
        str(mp4),
    ], check=True)
    log(f'Wrote {mp4}')
except Exception as e:
    log(f'ffmpeg failed: {e}')

# Drop the per-frame PNGs once the mp4 is encoded — they're only the
# raw input to ffmpeg and bloat both the artifact and gh-pages
# (300+ frame_*.png files per platform). Keep frames/ on ffmpeg failure
# so the run is still debuggable.
try:
    if mp4.exists() and mp4.stat().st_size > 0:
        import shutil
        shutil.rmtree(FRAMES, ignore_errors=True)
        log(f'  removed {FRAMES} after successful encode')
except Exception as e:
    log(f'  frames cleanup failed: {e}')

# Place a copy of the mp4 under videos/<workflow_name>/driver.mp4 with
# a sidecar metadata.json so the html report's existing video-discovery
# loop (html_report.generate_html_report) renders it as a playable
# video on the per-platform index. Use the picked template name for
# now; when the per-workflow loop ships, this will be one entry per
# workflow plus a 'system' entry for the wizard/install phase.
try:
    if mp4.exists() and mp4.stat().st_size > 0:
        import shutil
        videos_root = OUT.parent / 'videos'
        for _wf in _workflow_results:
            wf_name = _wf.get('name') or 'system'
            wf_dir = videos_root / wf_name
            wf_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(mp4), str(wf_dir / 'driver.mp4'))
            (wf_dir / 'metadata.json').write_text(json.dumps({
                'mp4': 'driver.mp4',
                'duration_seconds': _wf.get('duration_seconds') or 0,
                'status': _wf.get('status') or 'unknown',
            }, indent=2), encoding='utf-8')
            log(f'  videos/{wf_name}/driver.mp4 placed')
        # Always also make a 'system' entry pointing at the same mp4
        # so the wizard/install run is browsable even if no workflow ran.
        if not _workflow_results:
            sys_dir = videos_root / 'system'
            sys_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(mp4), str(sys_dir / 'driver.mp4'))
            (sys_dir / 'metadata.json').write_text(json.dumps({
                'mp4': 'driver.mp4', 'duration_seconds': 0, 'status': 'pass',
            }, indent=2), encoding='utf-8')
            log('  videos/system/driver.mp4 placed (no workflows ran)')
except Exception as e:
    log(f'  videos/ placement failed: {e}')
