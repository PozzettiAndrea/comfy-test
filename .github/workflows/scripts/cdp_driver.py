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

def click_with_cursor(page, loc):
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
    loc.click(timeout=3000)

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
    POST_ACTIONS = [
        ('Continue Locally',  'button:has-text("Continue Locally"):visible'),
        ('Close Templates',   'button[aria-label="Close"]:visible'),
        ('Extensions',        'button[aria-label="Extensions"]:visible'),
    ]
    for name, sel in POST_ACTIONS:
        log(f'  post: waiting for {name}')
        deadline = time.time() + 8
        hit = False
        while time.time() < deadline:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible() and not loc.is_disabled():
                    click_with_cursor(page, loc)
                    log(f'  post: clicked {name}')
                    sleep_capturing(page, 2, fps=5)
                    hit = True
                    break
            except Exception:
                pass
            sleep_capturing(page, 0.5, fps=5)
        if not hit:
            log(f'  post: {name} not found, skipping')
            if name == 'Close Templates':
                try:
                    page.keyboard.press('Escape')
                    log('  post: pressed Escape (Templates fallback)')
                    sleep_capturing(page, 2, fps=5)
                except Exception:
                    pass

    # Extensions search → pick tile → choose Latest → Install.
    # Hard-coded to SAM3/pznodes while we iterate; generalizing to
    # parse pyproject.toml from inputs.node_repo is out of scope.
    NODE_DISPLAY_NAME = 'SAM3'
    PUBLISHER = 'pznodes'
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
                for label in ('0.1.10', 'Latest', 'Nightly'):
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
        log('  ext: waiting for "Apply Changes" toast')
        applied = False
        deadline = time.time() + 20
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
        # relaunch.
        sleep_capturing(page, 5, fps=5)

        log('  app: killing ComfyUI to force full relaunch')
        try: browser.close()
        except Exception: pass
        IS_WIN = sys.platform == 'win32'
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

        log('  app: relaunching with CDP')
        if IS_WIN:
            app_exe = os.path.join(os.environ['LOCALAPPDATA'],
                                   'Programs', 'ComfyUI', 'ComfyUI.exe')
            subprocess.Popen([app_exe, '--remote-debugging-port=9222'],
                             creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
        else:
            ws = os.environ.get('GITHUB_WORKSPACE', '')
            app_path = os.path.join(ws, 'ComfyUI.app')
            subprocess.Popen(['open', app_path, '--args', '--remote-debugging-port=9222'])

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
            # Wait for /system_stats again (may already be up since
            # config is now installed).
            for i in range(180):
                if server_up():
                    log(f'  app: server ready after {i+1}s')
                    break
                frame(page)
                time.sleep(1)
            sleep_capturing(page, 8, fps=5)

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

        log('  ext: opening Templates sidebar')
        try:
            tpl = page.locator('button[aria-label="Templates"]:visible').first
            if tpl.count():
                click_with_cursor(page, tpl)
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
        # The Templates panel virtualizes — sections that aren't on
        # screen aren't in the DOM. Incrementally scroll the panel
        # and probe for the node section after each scroll.
        candidates = [
            f'aside [role="button"]:has-text("{NODE_PACKAGE_NAME}")',
            f'aside button:has-text("{NODE_PACKAGE_NAME}")',
            f'aside h2:has-text("{NODE_PACKAGE_NAME}")',
            f'aside h3:has-text("{NODE_PACKAGE_NAME}")',
            f'aside [class*="accordion"]:has-text("{NODE_PACKAGE_NAME}")',
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
                panel = page.locator('aside [class*="overflow-y-auto"]:visible, aside .scrollbar-custom:visible').first
                for i in range(20):
                    try:
                        if panel.count():
                            panel.evaluate('el => el.scrollBy(0, el.clientHeight * 0.7)')
                        else:
                            page.keyboard.press('PageDown')
                    except Exception:
                        page.keyboard.press('PageDown')
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

        # Open the first template card under the node section.
        if node_section is not None:
            log(f'  ext: opening first {NODE_PACKAGE_NAME} template')
            try:
                card_candidates = [
                    'aside [class*="template-card"]:visible',
                    'aside [class*="workflow-card"]:visible',
                    'aside .cursor-pointer img:visible',
                    'aside .cursor-pointer:visible',
                ]
                tpl_card = None
                for sel in card_candidates:
                    cards = page.locator(sel)
                    if cards.count():
                        tpl_card = cards.first
                        log(f'  ext: found template via {sel}')
                        break
                if tpl_card is not None:
                    tpl_card.scroll_into_view_if_needed()
                    sleep_capturing(page, 1, fps=5)
                    click_with_cursor(page, tpl_card)
                    log('  ext: clicked first template')
                    sleep_capturing(page, 4, fps=5)
            except Exception as e:
                log(f'  ext: template click failed: {e}')

            # Run the workflow.
            log('  ext: clicking Run')
            try:
                run_btn = page.locator(
                    'button[aria-label="Run"]:visible, '
                    'button:has-text("Run"):visible'
                ).first
                if run_btn.count():
                    click_with_cursor(page, run_btn)
                    log('  ext: clicked Run')
                    sleep_capturing(page, 30, fps=5)
                else:
                    log('  ext: Run button not found')
            except Exception as e:
                log(f'  ext: Run click failed: {e}')

    snap(page, 'final')
    log(f'Captured {fi[0]} frames')
    browser.close()

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
