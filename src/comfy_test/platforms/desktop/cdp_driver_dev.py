import json, os, re as _re, subprocess, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

# ComfyUI HTTP port. Discovered lazily inside _walk_first_run_wizard's
# _discover_comfy_port() from CDP's page list (whichever 127.0.0.1:<port>
# URL is loaded is the right one). Comfy Desktop 1.0.34 uses 8188; older
# builds used 8000. All downstream server checks read this instead of
# hardcoding.
_COMFY_PORT = None

t0 = time.time()
import builtins as _b
def log(*a, **k):
    _b.print(f'[{int(time.time()-t0):4d}s]', *a, **k, flush=True)

# Three roots, all default to COMFY_TEST_LOGS_DIR for back-compat with
# the flat layout. The platform YMLs override them to mirror the cpu
# nested structure: artifacts under <RUN_ID>/<platform>/, debug-only
# captures (electron_inspect, frames mid-state) under <RUN_ID>/debug/.
_CDP_PORT = int(os.environ.get('COMFY_DESKTOP_CDP_PORT', '9222'))
_LOGS_DIR = Path(os.environ['COMFY_TEST_LOGS_DIR'].replace('\\', '/'))
_RUN_DIR = Path(os.environ.get('COMFY_TEST_RUN_DIR', str(_LOGS_DIR)).replace('\\', '/'))
_DEBUG_DIR = Path(os.environ.get('COMFY_TEST_DEBUG_DIR', str(_LOGS_DIR)).replace('\\', '/'))

# OUT = where DOM snapshots, intermediate frames, and the final mp4
# master live. Treated as debug -- no part of the standard report needs
# it. Standard outputs (results.json, videos/<workflow>/*) go under
# _RUN_DIR.
OUT = _DEBUG_DIR / 'electron_inspect'
FRAMES = OUT / 'frames'
OUT.mkdir(parents=True, exist_ok=True)
FRAMES.mkdir(parents=True, exist_ok=True)
_RUN_DIR.mkdir(parents=True, exist_ok=True)
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

# Frame capture is polling-based: page.screenshot() called from the
# main thread on a sleep loop. We tried CDP Page.startScreencast (push)
# but the ack flow has to run on Playwright's dispatcher thread, which
# is the same thread that needs to read the ack response -- chromium's
# send buffer fills (~318 frames) and screencast quietly stalls.
#
# Polling on the main thread doesn't have that problem (screenshot is
# a sync RPC issued by the same thread that's running the rest of the
# driver flow). The one failure mode polling had -- page.screenshot()
# silently failing post-relaunch because the page reference was bound
# to a stale CDP target -- is fixed by re-resolving the page from the
# current browser if a screenshot raises.
_browser_ref = [None]  # set whenever connect_over_cdp gives us a browser
_capture_warned = [False]

def frame(page):
    fi[0] += 1
    path = str(FRAMES / f'frame_{fi[0]:06d}.png')
    try:
        page.screenshot(path=path)
        return
    except Exception as e:
        # Page is probably detached after browser.close()+reconnect.
        # Re-resolve from the live browser and try once more.
        br = _browser_ref[0]
        if br is None:
            if not _capture_warned[0]:
                log(f'  frame: capture skipped (no browser ref): {e}')
                _capture_warned[0] = True
            return
        try:
            fresh = main_page(br)
            if fresh is None:
                if not _capture_warned[0]:
                    log(f'  frame: capture skipped (no page): {e}')
                    _capture_warned[0] = True
                return
            fresh.screenshot(path=path)
        except Exception as e2:
            if not _capture_warned[0]:
                log(f'  frame: capture failed: {e2}')
                _capture_warned[0] = True

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
    tg = json.loads(urllib.request.urlopen(f'http://localhost:{_CDP_PORT}/json').read())
    (OUT / 'targets.json').write_text(json.dumps(tg, indent=2))
    log(f'CDP targets: {len(tg)}')
    for t in tg:
        log(f"  {t.get('type')}: {t.get('url')} | {t.get('title')}")
except Exception as e:
    log(f'targets list: {e}')


def _prune_blank_targets(cdp_port):
    """Playwright's connect_over_cdp attaches to every page target and hangs
    forever on any that never fires Target.attachedToTarget (blank URLs,
    Electron helper limbo, etc). Close those via the browser CDP endpoint
    before every connect_over_cdp. Idempotent -- safe to call multiple times.

    Only pages with an empty URL are pruned; ComfyUI + normal Electron
    chrome helpers (Title Bar / Title Popup / System Modal) stay alive.
    """
    import websocket as _ws
    try:
        pages = json.loads(urllib.request.urlopen(
            f'http://localhost:{cdp_port}/json/list', timeout=2).read())
        blanks = [p for p in pages
                  if p.get('type') == 'page' and not p.get('url')]
        if not blanks:
            return
        ver = json.loads(urllib.request.urlopen(
            f'http://localhost:{cdp_port}/json/version', timeout=2).read())
        bws = _ws.create_connection(ver['webSocketDebuggerUrl'],
                                    timeout=5,
                                    origin=f'http://localhost:{cdp_port}')
        try:
            for i, p in enumerate(blanks, 1):
                bws.send(json.dumps({
                    "id": i, "method": "Target.closeTarget",
                    "params": {"targetId": p['id']},
                }))
                while True:
                    r = json.loads(bws.recv())
                    if r.get("id") == i:
                        log(f'  prune: closed blank target {p["id"][:12]}')
                        break
        finally:
            bws.close()
    except Exception as e:
        log(f'  prune: skipped (non-fatal): {e}')


# ---------------------------------------------------------------------------
# First-run wizard walker (raw CDP over websocket-client).
#
# Why not playwright: as of Comfy Desktop 1.0.34, `p.chromium.connect_over_cdp`
# hangs at Target attachment forever (180s timeout). Best guess: the setup
# wizard spawns 6 child pages (panel/titleBar/titlePopup/systemModal + two
# empty helpers) and playwright can't finish enumerating them via
# `Target.attachedToTarget`. We DO know that after the wizard has committed
# the app to normal ComfyUI mode there's a single main page, at which point
# connect_over_cdp works fine.
#
# So: drive the wizard past chooser -> configure -> workflow -> install via
# raw CDP, then hand off to playwright for the rest of the flow (Manager
# install, node install, workflow runs).
#
# The walker is idempotent: each iteration inspects the current DOM and
# does whichever action matches. Safe to re-enter mid-way (e.g. if the
# chooser was already completed by a previous test run).
# ---------------------------------------------------------------------------
def _walk_first_run_wizard(cdp_port, timeout=1200):
    import websocket  # from websocket-client, added to venv by _desktop_runner
    def _ws_id():
        _ws_id.n += 1
        return _ws_id.n
    _ws_id.n = 0

    def _attach_panel():
        """Find the panel.html page + open a WS to it. Returns (ws, url)."""
        pages = json.loads(urllib.request.urlopen(
            f'http://localhost:{cdp_port}/json/list', timeout=3).read())
        cand = None
        for p in pages:
            if p.get('type') == 'page' and 'panel.html' in p.get('url', ''):
                cand = p
                break
        if not cand:
            return None, None
        return (websocket.create_connection(cand['webSocketDebuggerUrl'],
                                            timeout=10,
                                            origin=f'http://localhost:{cdp_port}'),
                cand['url'])

    def _eval(ws, expr):
        i = _ws_id()
        ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == i:
                return r.get("result", {}).get("result", {}).get("value")

    def _discover_comfy_port():
        """Comfy Desktop 1.0.34 serves ComfyUI on :8188; earlier versions
        used :8000. Pull the port out of the CDP page list -- whichever
        127.0.0.1:<port> URL is currently loaded is the right one. Falls
        back to trying both known ports if no page URL matches yet."""
        try:
            pages = json.loads(urllib.request.urlopen(
                f'http://localhost:{cdp_port}/json/list', timeout=2).read())
            for p in pages:
                u = p.get('url', '')
                m = _re.search(r'127\.0\.0\.1:(\d+)', u)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        for candidate in (8188, 8000):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{candidate}/system_stats',
                                       timeout=1)
                return candidate
            except Exception:
                pass
        return None

    def _server_up():
        global _COMFY_PORT
        if _COMFY_PORT is None:
            _COMFY_PORT = _discover_comfy_port()
        if _COMFY_PORT is None:
            return False
        try:
            urllib.request.urlopen(
                f'http://127.0.0.1:{_COMFY_PORT}/system_stats', timeout=2)
            return True
        except Exception:
            return False

    # JS payloads run inside the panel.html page context.
    _JS_CLICK_LOCAL = """
        (() => { const b=[...document.querySelectorAll('button,[role=button]')]
            .find(x=>(x.innerText||'').startsWith('Full control'));
          if(!b) return 'no Local button';
          b.click(); return 'clicked Local'; })()
    """
    _JS_TOGGLE_CBS = r"""
        (() => { const out=[];
          for(const cb of document.querySelectorAll('input[type=checkbox]')){
            let label=''; if(cb.id){const l=document.querySelector('label[for="'+cb.id+'"]'); if(l) label=l.innerText;}
            if(!label) label=(cb.closest('label')?.innerText)||(cb.parentElement?.innerText)||'';
            label=label.trim();
            const wantEULA=/EULA|Terms of Service|agree/i.test(label);
            const wantTelemOff=/improve Comfy|anonymous usage/i.test(label);
            if(wantEULA && !cb.checked){cb.click(); out.push('EULA->on');}
            else if(wantTelemOff && cb.checked){cb.click(); out.push('telem->off');}
          }
          return JSON.stringify(out);
        })()
    """
    def _JS_CLICK_BTN(label):
        # Exact text match, skip disabled buttons.
        return f"""
        (() => {{ const b=[...document.querySelectorAll('button,[role=button]')]
            .find(x=>(x.innerText||'').trim()==={label!r});
          if(!b) return 'no {label} button';
          if(b.disabled||b.getAttribute('aria-disabled')==='true') return '{label} disabled';
          b.click(); return 'clicked {label}'; }})()
        """

    # Prune logic lives at module scope (_prune_blank_targets) so the same
    # helper serves every connect_over_cdp callsite; here we just call it.
    def _prune_stuck_targets():
        _prune_blank_targets(cdp_port)

    def _dismiss_templates_modal():
        """After first ComfyUI load, a Templates modal auto-opens. ESC
        dismisses it. Playwright's own logic handles this later too, but
        doing it here means the driver starts on a clean canvas."""
        try:
            pages = json.loads(urllib.request.urlopen(
                f'http://localhost:{cdp_port}/json/list', timeout=2).read())
            comfy = next((p for p in pages
                          if p.get('type') == 'page'
                          and '127.0.0.1' in p.get('url', '')), None)
            if not comfy: return
            pws = websocket.create_connection(comfy['webSocketDebuggerUrl'],
                                              timeout=5,
                                              origin=f'http://localhost:{cdp_port}')
            try:
                for evt_type in ('keyDown', 'keyUp'):
                    i = _ws_id()
                    pws.send(json.dumps({"id": i, "method": "Input.dispatchKeyEvent",
                                         "params": {"type": evt_type, "key": "Escape",
                                                    "code": "Escape",
                                                    "windowsVirtualKeyCode": 27}}))
                    while True:
                        r = json.loads(pws.recv())
                        if r.get("id") == i: break
                log('[wizard-raw] sent ESC to close Templates modal')
            finally:
                pws.close()
        except Exception as e:
            log(f'[wizard-raw] templates-dismiss failed (non-fatal): {e}')

    start = time.time()
    last_screen = None
    ws = None
    log(f'[wizard-raw] start (cdp port {cdp_port})')
    while time.time() - start < timeout:
        if _server_up():
            log(f'[wizard-raw] /system_stats up after {int(time.time()-start)}s')
            if ws:
                try: ws.close()
                except Exception: pass
            # Give ComfyUI a beat to finish loading + open its Templates
            # modal, then dismiss it and prune blank-URL targets so
            # playwright's connect_over_cdp doesn't hang on them.
            time.sleep(3)
            _dismiss_templates_modal()
            _prune_stuck_targets()
            return True
        try:
            if ws is None:
                ws, url = _attach_panel()
                if ws is None:
                    log('[wizard-raw]   no panel.html target yet, retry in 1s')
                    time.sleep(1)
                    continue
                log(f'[wizard-raw] attached to {url}')
            txt = _eval(ws, 'document.body.innerText') or ''
            # Screen detection heuristics from actual DOM samples.
            if 'How do you want to run Comfy' in txt:
                screen = 'chooser'
            elif 'Configure Comfy Desktop' in txt:
                screen = 'configure'
            elif 'Choose a starter workflow' in txt:
                screen = 'workflow_chooser'
            elif ('Downloading ComfyUI' in txt or 'Unpacking the install' in txt
                  or 'Set up environment' in txt):
                screen = 'installing'
            else:
                screen = 'unknown'
            if screen != last_screen:
                log(f'[wizard-raw] screen: {screen}')
                last_screen = screen

            if screen == 'chooser':
                log(f'[wizard-raw]   {_eval(ws, _JS_CLICK_LOCAL)}')
                log(f'[wizard-raw]   cbs: {_eval(ws, _JS_TOGGLE_CBS)}')
                time.sleep(0.4)
                log(f'[wizard-raw]   {_eval(ws, _JS_CLICK_BTN("Continue"))}')
            elif screen == 'configure':
                log(f'[wizard-raw]   {_eval(ws, _JS_CLICK_BTN("Continue"))}')
            elif screen == 'workflow_chooser':
                log(f'[wizard-raw]   {_eval(ws, _JS_CLICK_BTN("Skip & Install"))}')
            elif screen == 'installing':
                # nothing to click, just wait for /system_stats to come up
                pass
            time.sleep(2)
        except Exception as e:
            log(f'[wizard-raw] error: {e.__class__.__name__}: {e}; reattaching')
            try:
                if ws: ws.close()
            except Exception: pass
            ws = None
            time.sleep(1)
    log(f'[wizard-raw] TIMEOUT after {timeout}s without /system_stats')
    if ws:
        try: ws.close()
        except Exception: pass
    return False


# Drive the wizard now. If it never gets ComfyUI's server up, don't even
# try to attach playwright -- it'll just hang on the still-alive wizard
# targets.
if not _walk_first_run_wizard(_CDP_PORT):
    log('[wizard-raw] giving up; ComfyUI server never came up')
    sys.exit(1)


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


# Test-harness banner: yellow fixed-position strip across the top of the
# ComfyUI window. Used to announce shell steps we run BEHIND Manager's
# install (e.g. `git checkout dev` after Manager clones main) so a viewer
# of the video is never misled about what happened.
_DISCLAIMER_JS = r'''
(lines, ttlMs) => {
  const id = '__cm_test_disclaimer';
  const prev = document.getElementById(id);
  if (prev) prev.remove();
  const b = document.createElement('div');
  b.id = id;
  b.style.cssText = [
    'position:fixed','top:0','left:0','right:0','z-index:2147483647',
    'background:#ffcc00','color:#000',
    'font-family:"SF Mono","Monaco",ui-monospace,monospace','font-size:12px',
    'padding:8px 12px','box-shadow:0 2px 8px rgba(0,0,0,0.4)',
    'white-space:pre-wrap','line-height:1.4','pointer-events:none',
    'border-bottom:2px solid #000'
  ].join(';');
  b.textContent = lines.join('\n');
  document.documentElement.appendChild(b);
  if (ttlMs && ttlMs > 0) {
    setTimeout(() => { const el = document.getElementById(id); if (el) el.remove(); }, ttlMs);
  }
}
'''

def _show_test_disclaimer(page, lines, duration=None):
    """Inject a yellow banner into the ComfyUI window announcing test-harness
    shell steps. `duration` in seconds; None = manual dismiss via
    _hide_test_disclaimer."""
    ttl_ms = int(duration * 1000) if duration else 0
    try:
        page.evaluate(_DISCLAIMER_JS, [lines, ttl_ms])
    except Exception as e:
        log(f'  disclaimer inject failed: {e}')


def _hide_test_disclaimer(page):
    try:
        page.evaluate("() => { const el = document.getElementById('__cm_test_disclaimer'); if (el) el.remove(); }")
    except Exception:
        pass


def _find_active_comfy_install():
    """Read Comfy Desktop's installations.json and return
    (install_path, comfy_root, custom_nodes, venv_python) for the active
    standalone install. Raises RuntimeError if not found."""
    installations_json = (Path.home() / 'Library' / 'Application Support' /
                          'Comfy Desktop' / 'installations.json')
    try:
        for inst in json.loads(installations_json.read_text()):
            if inst.get('sourceId') == 'standalone' and inst.get('installPath'):
                install_path = Path(inst['installPath'])
                break
        else:
            raise RuntimeError('no standalone install in installations.json')
    except FileNotFoundError:
        raise RuntimeError(f'{installations_json} not found (Comfy Desktop not launched?)')
    comfy_root = install_path / 'ComfyUI'
    custom_nodes = comfy_root / 'custom_nodes'
    venv_python = comfy_root / '.venv' / 'bin' / 'python'
    if not venv_python.exists():
        venv_python = install_path / 'standalone-env' / 'bin' / 'python'
    return install_path, comfy_root, custom_nodes, venv_python

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

# ============================================================================
# Per-workflow loop helpers. Used by the multi-workflow loop after the
# post-Apply-Changes Electron relaunch + renderer reload settles. Each
# workflow runs from a freshly-restarted ComfyUI to mirror CI's per-container
# isolation (no state-bleed between workflows).
# ============================================================================

def _kill_comfy_proc():
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/IM', 'ComfyUI.exe'],
                           capture_output=True, timeout=10)
        else:
            subprocess.run(['pkill', '-f', 'ComfyUI'],
                           capture_output=True, timeout=10)
    except Exception as e:
        log(f'  loop: kill error (ignored): {e}')


def _devtools_active_port_path():
    """Electron writes the chosen --remote-debugging-port to this file in
    its userData dir. Format:
        <port>
        /devtools/browser/<guid>
    For ComfyUI Desktop, userData is %APPDATA%\\ComfyUI on Windows and
    ~/Library/Application Support/ComfyUI on macOS. Recent builds (1.0.34+)
    renamed the app to 'Comfy Desktop' so also check that path. Resolve
    robustly so SYSTEM-context APPDATA inherited from agent harnesses
    doesn't trip us."""
    # Try new "Comfy Desktop" name first, fall back to legacy "ComfyUI"
    # for older builds.
    _APP_DIRS = ('Comfy Desktop', 'ComfyUI')
    def _pick(base):
        for name in _APP_DIRS:
            p = base / name / 'DevToolsActivePort'
            if p.exists():
                return p
        # None exists yet -- return the new-name path so watchers can
        # wait for it to appear.
        return base / _APP_DIRS[0] / 'DevToolsActivePort'
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA', '')
        if appdata and 'systemprofile' not in appdata.lower():
            return _pick(Path(appdata))
        up = os.environ.get('USERPROFILE', '')
        if up and 'systemprofile' not in up.lower():
            return _pick(Path(up) / 'AppData' / 'Roaming')
        username = os.environ.get('USERNAME', '')
        if username and username.upper() != 'SYSTEM':
            return _pick(Path('C:/Users') / username / 'AppData' / 'Roaming')
        from glob import glob as _glob
        for p in _glob(r'C:\Users\*\AppData\Roaming'):
            if 'systemprofile' in p.lower():
                continue
            return _pick(Path(p))
        return _pick(Path.home() / 'AppData' / 'Roaming')
    if sys.platform == 'darwin':
        return _pick(Path.home() / 'Library' / 'Application Support')
    return _pick(Path.home() / '.config')


def _launch_comfy_random_port():
    """Launch ComfyUI with --remote-debugging-port=0 (let chromium pick a
    free ephemeral port) and read the chosen port from DevToolsActivePort.
    This is what every major browser-test harness does (Puppeteer,
    Playwright, Selenium, chromedp) -- sidesteps the Windows orphan-LISTEN
    socket problem entirely because each launch picks a fresh port the
    kernel guarantees is unbound. Returns the chosen port (int) or None."""
    devtools_file = _devtools_active_port_path()
    # Clear stale file from prior instance so we don't read its old port.
    try:
        devtools_file.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f'  loop: DevToolsActivePort cleanup err (ignored): {e}')

    if sys.platform == 'win32':
        app_exe = os.environ.get('COMFY_DESKTOP_APP_EXE') or os.path.join(
            os.environ.get('LOCALAPPDATA', ''), 'Programs', 'ComfyUI', 'ComfyUI.exe')
        subprocess.Popen([app_exe, '--remote-debugging-port=0'],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
    else:
        app_path = os.environ.get('COMFY_DESKTOP_APP_PATH') or os.path.join(
            os.environ.get('GITHUB_WORKSPACE', ''), 'ComfyUI.app')
        subprocess.Popen(['open', app_path, '--args', '--remote-debugging-port=0'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    log(f'  loop: waiting for DevToolsActivePort at {devtools_file}')
    for i in range(240):
        if devtools_file.exists():
            try:
                content = devtools_file.read_text(encoding='utf-8').strip()
                if content:
                    port = int(content.splitlines()[0])
                    log(f'  loop: DevToolsActivePort -> {port} after {i+1}s')
                    return port
            except Exception as e:
                log(f'  loop: DevToolsActivePort parse err: {e}')
        time.sleep(1)
    log(f'  loop: DevToolsActivePort never appeared after 240s')
    return None


def _wait_cdp_up(timeout_s=240):
    for i in range(timeout_s):
        try:
            urllib.request.urlopen(f'http://localhost:{_CDP_PORT}/json/version', timeout=1)
            log(f'  loop: CDP up after {i+1}s')
            return True
        except Exception:
            time.sleep(1)
    log(f'  loop: CDP did not come up within {timeout_s}s')
    return False


def _wait_cdp_port_free(timeout_s=30):
    """Poll until nothing is bound to the CDP port -- covers the Windows
    case where taskkill returns immediately but the kernel hasn't released
    the previous instance's listener yet. Best-effort: returns True if the
    port becomes bindable, False on timeout."""
    import socket as _socket
    for i in range(timeout_s):
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.bind(('127.0.0.1', _CDP_PORT))
            s.close()
            if i > 0:
                log(f'  loop: port {_CDP_PORT} free after {i}s')
            return True
        except OSError:
            try: s.close()
            except Exception: pass
            time.sleep(1)
    log(f'  loop: port {_CDP_PORT} still held after {timeout_s}s; relaunching anyway')
    return False


def _wait_canvas_ready(page_arg, timeout_s=120):
    for i in range(timeout_s):
        try:
            ready = page_arg.evaluate(
                "typeof window.app !== 'undefined' "
                "&& window.app.graph !== undefined")
            if ready:
                log(f'  loop: canvas ready after {i+1}s')
                return True
        except Exception:
            pass
        try: frame(page_arg)
        except Exception: pass
        time.sleep(1)
    log(f'  loop: canvas not ready in {timeout_s}s (still at {page_arg.url})')
    return False


def _restart_comfy(p_arg, current_browser):
    """Full restart: close current browser, kill ComfyUI, relaunch, reconnect,
    reload renderer (refreshes templates manifest), wait for canvas. Returns
    (new_page, new_browser). new_page may be None on failure."""
    log('  loop: closing current browser')
    try: current_browser.close()
    except Exception: pass
    log('  loop: killing ComfyUI process')
    _kill_comfy_proc()
    time.sleep(5)
    # Launch the new instance with --remote-debugging-port=0; chromium picks
    # a fresh ephemeral port the kernel guarantees is unbound, so the
    # Windows orphan-LISTEN socket from the killed instance can't trip us.
    # Read the chosen port from <userData>/DevToolsActivePort.
    log('  loop: launching ComfyUI with --remote-debugging-port=0')
    new_port = _launch_comfy_random_port()
    if new_port is None:
        log('  loop: failed to obtain CDP port from DevToolsActivePort; bailing')
        return None, current_browser
    log(f'  loop: reconnecting Playwright on port {new_port}')
    _prune_blank_targets(new_port)
    new_browser = p_arg.chromium.connect_over_cdp(f'http://localhost:{new_port}')
    _browser_ref[0] = new_browser
    _capture_warned[0] = False
    new_page = main_page(new_browser)
    if new_page is None:
        log('  loop: no page after restart, bailing')
        return None, new_browser
    install_cursor(new_page)
    log(f'  loop: attached to {new_page.url}')
    for i in range(180):
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{_COMFY_PORT or 8188}/system_stats', timeout=2)
            log(f'  loop: server ready after {i+1}s')
            break
        except Exception:
            try: frame(new_page)
            except Exception: pass
            time.sleep(1)
    _wait_canvas_ready(new_page, 120)
    log('  loop: forcing renderer reload')
    try:
        new_page.reload(wait_until='load', timeout=30000)
        install_cursor(new_page)
        _wait_canvas_ready(new_page, 60)
    except Exception as e:
        log(f'  loop: post-restart reload failed: {e}')
    sleep_capturing(new_page, 3, fps=5)
    return new_page, new_browser


def _dismiss_post_restart_modals(page_arg):
    log('  ext: closing Nodes Manager dialog')
    try:
        cd = page_arg.locator('button[aria-label="Close dialog"]:visible').first
        if cd.count():
            click_with_cursor(page_arg, cd)
            log('  ext: clicked Close dialog')
            sleep_capturing(page_arg, 2, fps=5)
    except Exception as e:
        log(f'  ext: Close dialog failed: {e}')

    log("  ext: dismissing What's New popup")
    try:
        wn = page_arg.locator(
            '.whats-new-popup button[aria-label="Close"]:visible, '
            '.whats-new-popup button.close-button:visible').first
        if wn.count():
            click_with_cursor(page_arg, wn)
            log("  ext: closed What's New popup")
            sleep_capturing(page_arg, 2, fps=5)
        else:
            log("  ext: What's New popup not present")
    except Exception as e:
        log(f"  ext: What's New close failed: {e}")

    log('  ext: dismissing Node Pack Issues modal (if present)')
    try:
        npi = page_arg.locator(
            'div[role="dialog"]:has-text("Node Pack Issues") button:has-text("Close"):visible, '
            'div[role="dialog"]:has-text("Issues") button[aria-label="Close"]:visible').first
        if npi.count():
            click_with_cursor(page_arg, npi)
            log('  ext: closed Node Pack Issues modal')
            sleep_capturing(page_arg, 2, fps=5)
        else:
            log('  ext: Node Pack Issues modal not present')
    except Exception as e:
        log(f"  ext: Node Pack Issues close failed: {e}")


def _open_templates_and_section(page_arg, node_package_name):
    log('  ext: opening Templates sidebar')
    try:
        tpl = page_arg.locator('button[aria-label="Templates"]:visible').first
        if tpl.count():
            click_with_cursor(page_arg, tpl, timeout=10000)
            log('  ext: clicked Templates')
            sleep_capturing(page_arg, 4, fps=5)
        else:
            log('  ext: Templates sidebar button not found')
            return False
    except Exception as e:
        log(f'  ext: Templates click failed: {e}')
        return False

    log(f'  ext: locating "{node_package_name}" section in Templates panel')
    candidates = [
        f'nav [role="button"]:has-text("{node_package_name}")',
        f'nav span:has-text("{node_package_name}")',
        f'nav button:has-text("{node_package_name}")',
    ]
    def find_node_section_local():
        for sel in candidates:
            loc = page_arg.locator(sel).first
            if loc.count():
                return loc, sel
        return None, None
    node_section, hit_sel = find_node_section_local()
    if node_section is None:
        find_panel_js = """
        () => {
          const dialogs = Array.from(document.querySelectorAll(
            'div[role="dialog"], aside, nav'));
          const scrollables = [];
          dialogs.forEach(d => {
            d.querySelectorAll('*').forEach(el => {
              const cs = getComputedStyle(el);
              if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                  && el.scrollHeight > el.clientHeight + 4) {
                scrollables.push(el);
              }
            });
          });
          const nav_first = scrollables.find(el => el.closest('nav'));
          const chosen = nav_first || scrollables.sort(
            (a,b) => (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight)
          )[0];
          if (!chosen) return null;
          chosen.setAttribute('data-driver-scroll', '1');
          return {scrollHeight: chosen.scrollHeight, clientHeight: chosen.clientHeight, scrollTop: chosen.scrollTop};
        }
        """
        info = page_arg.evaluate(find_panel_js)
        if info:
            log(f'  ext: scroll target found (scrollHeight={info["scrollHeight"]} clientHeight={info["clientHeight"]})')
        else:
            log('  ext: no scrollable panel found, falling back to PageDown')
        step_js = """
        () => {
          const el = document.querySelector('[data-driver-scroll="1"]');
          if (!el) return null;
          const before = el.scrollTop;
          el.scrollBy(0, Math.max(40, el.clientHeight * 0.7));
          return {before, after: el.scrollTop, max: el.scrollHeight - el.clientHeight};
        }
        """
        stuck = 0
        last_top = -1
        for i in range(60):
            res = page_arg.evaluate(step_js) if info else None
            if res is None:
                try: page_arg.keyboard.press('PageDown')
                except Exception: pass
            else:
                if res['after'] == res['before']:
                    stuck += 1
                else:
                    stuck = 0
                last_top = res['after']
                at_floor = (res['after'] >= res['max'] - 2 and stuck >= 2)
                if at_floor:
                    log(f'  ext: reached scroll floor at iter {i+1} '
                        f'(scrollTop={res["after"]} max={res["max"]})')
                    sleep_capturing(page_arg, 1, fps=5)
                    node_section, hit_sel = find_node_section_local()
                    break
            sleep_capturing(page_arg, 0.7, fps=5)
            node_section, hit_sel = find_node_section_local()
            if node_section is not None:
                log(f'  ext: scrolled {i+1}x to {node_package_name} '
                    f'({hit_sel}, scrollTop={last_top})')
                break
        else:
            log(f'  ext: ran 60 scroll iters, last scrollTop={last_top}')
    if node_section is None:
        log(f'  ext: {node_package_name} section not found after scrolling')
        return False
    try:
        node_section.scroll_into_view_if_needed()
        sleep_capturing(page_arg, 1, fps=5)
        click_with_cursor(page_arg, node_section)
        log(f'  ext: clicked {node_package_name} section')
        sleep_capturing(page_arg, 2, fps=5)
        return True
    except Exception as e:
        log(f'  ext: {node_package_name} section click failed: {e}')
        return False


def _enumerate_matching_cards(page_arg, cpu_mode, cpu_items):
    try:
        cards = page_arg.locator('[data-testid^="template-workflow-"]:visible')
        n = cards.count()
        log(f'  ext: {n} visible cards in section')
        names = []
        for i in range(n):
            c = cards.nth(i)
            tid = c.get_attribute('data-testid') or ''
            nm = tid[len('template-workflow-'):] if tid.startswith('template-workflow-') else tid
            if cpu_mode == 'all' or \
               (cpu_mode == 'include' and nm in cpu_items) or \
               (cpu_mode == 'exclude' and nm not in cpu_items):
                names.append(nm)
            else:
                log(f'  ext: skipping {nm} (not in spec)')
        return names
    except Exception as e:
        log(f'  ext: enumerate failed: {e}')
        return []


_WS_LISTENER_JS = r"""
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
"""


def _run_named_card(page_arg, target_name):
    """Click target card by data-testid, install WS listener, click Run,
    wait up to 600s. Returns {name, status, duration_seconds, error}."""
    log(f'  ext: clicking template {target_name}')
    try:
        # No `:visible` filter -- cards lower in the section's grid may be
        # off-screen until scrolled into view. data-testid is unique per
        # workflow, so the unfiltered locator is safe.
        card = page_arg.locator(f'[data-testid="template-workflow-{target_name}"]').first
        if not card.count():
            log(f'  ext: card {target_name} not found in section DOM')
            return {'name': target_name, 'status': 'fail',
                    'duration_seconds': 0,
                    'error': f'card {target_name} not found in section'}
        card.scroll_into_view_if_needed()
        sleep_capturing(page_arg, 1, fps=5)
        click_with_cursor(page_arg, card)
        log(f'  ext: clicked template {target_name}')
        sleep_capturing(page_arg, 5, fps=5)
    except Exception as e:
        log(f'  ext: template click failed: {e}')
        return {'name': target_name, 'status': 'fail',
                'duration_seconds': 0, 'error': str(e)}

    log('  ext: installing WS execution listener')
    try:
        page_arg.evaluate(_WS_LISTENER_JS)
    except Exception as e:
        log(f'  ext: WS listener install failed: {e}')

    log('  ext: clicking Run')
    try:
        run_btn = page_arg.locator(
            'button[aria-label="Run"]:visible, button:has-text("Run"):visible').first
        if run_btn.count():
            click_with_cursor(page_arg, run_btn)
            log('  ext: clicked Run')
        else:
            log('  ext: Run button not found')
    except Exception as e:
        log(f'  ext: Run click failed: {e}')

    log('  ext: waiting for execution_success / execution_error')
    run_deadline = time.time() + 600
    run_start = time.time()
    while time.time() < run_deadline:
        frame(page_arg)
        try:
            complete = page_arg.evaluate('window._executionComplete')
        except Exception:
            complete = False
        if complete:
            break
        time.sleep(0.5)
    elapsed = int(time.time() - run_start)
    try:
        events = page_arg.evaluate('window._executionEvents') or []
        err = page_arg.evaluate('window._executionError')
    except Exception:
        events, err = [], None
    log(f'  ext: WS events={len(events)} elapsed={elapsed}s')
    for ev in events[-15:]:
        log(f'    ws: {ev}')
    if err:
        try:
            log('  ext: execution_error data:')
            log(json.dumps(err, indent=2, default=str))
        except Exception:
            log(f'  ext: execution_error (non-serializable): {err!r}')
    elif elapsed >= 600:
        log('  ext: WORKFLOW TIMEOUT (no execution_success/error in 10min)')
    else:
        log(f'  ext: execution_success after {elapsed}s')

    if err:
        status = 'fail'
        err_str = json.dumps(err, default=str)
    elif elapsed >= 600:
        status = 'timeout'
        err_str = 'no execution_success/error in 10min'
    else:
        status = 'pass'
        err_str = None
    sleep_capturing(page_arg, 5, fps=5)
    return {'name': target_name, 'status': status,
            'duration_seconds': elapsed, 'error': err_str}


def _fetch_workflow_list_from_repo():
    """Authoritative list of template workflow names -- preferred source is
    the COMFY_TEST_WORKFLOWS env var (pre-enumerated from a local clone by
    _desktop_runner.run_desktop), falling back to the GitHub contents API
    when invoked outside that wrapper. Each `.json` stem matches the
    data-testid suffix the Templates panel renders
    (`template-workflow-<stem>`). Used as the source of truth for the
    per-workflow loop so we don't depend on which cards the GUI happens to
    have rendered/scrolled-into-view at enumeration time. Returns empty
    list on any failure."""
    env_list = os.environ.get('COMFY_TEST_WORKFLOWS', '').strip()
    if env_list:
        names = [n for n in env_list.split(',') if n]
        log(f'  loop: workflows/ from $COMFY_TEST_WORKFLOWS -> {names}')
        return names
    try:
        node_repo = os.environ.get('NODE_REPO', '')
        node_branch = os.environ.get('NODE_BRANCH', 'main')
        if not node_repo:
            return []
        url = f'https://api.github.com/repos/{node_repo}/contents/workflows?ref={node_branch}'
        req = urllib.request.Request(url, headers={'User-Agent': 'comfy-test-cdp-driver'})
        body = urllib.request.urlopen(req, timeout=10).read()
        items = json.loads(body)
        names = []
        for item in items:
            n = item.get('name', '')
            if isinstance(n, str) and n.endswith('.json'):
                names.append(n[:-5])
        names.sort()
        log(f'  loop: workflows/ from {node_repo}@{node_branch} -> {names}')
        return names
    except Exception as e:
        log(f'  loop: workflows/ fetch failed ({e})')
        return []


def _parse_cpu_spec():
    """Returns (mode, items) parsed from comfy-test.toml's
    [test.workflows].cpu (or .cuda when COMFY_TEST_CUDA=1)."""
    cpu_mode = 'all'
    cpu_items = []
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
            spec_key = 'cuda' if os.environ.get('COMFY_TEST_CUDA', '0') == '1' else 'cpu'
            spec = data.get('test', {}).get('workflows', {}).get(spec_key)
            if spec == 'all' or spec is None:
                cpu_mode = 'all'
            elif isinstance(spec, list):
                excludes = [f.lstrip('!') for f in spec
                            if isinstance(f, str) and f.startswith('!')]
                if excludes:
                    cpu_mode = 'exclude'
                    cpu_items = [e[:-5] if e.endswith('.json') else e for e in excludes]
                else:
                    cpu_mode = 'include'
                    cpu_items = [f[:-5] if f.endswith('.json') else f for f in spec]
            log(f'  ext: {spec_key} spec = {cpu_mode} {cpu_items}')
    except Exception as e:
        log(f'  ext: comfy-test.toml fetch/parse failed ({e}); defaulting to all')
    return cpu_mode, cpu_items


with sync_playwright() as p:
    _prune_blank_targets(_CDP_PORT)
    browser = p.chromium.connect_over_cdp(f'http://localhost:{_CDP_PORT}')
    _browser_ref[0] = browser
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
    # app boots into the welcome -> GPU -> path -> install -> telemetry
    # consent flow. We poll /system_stats; meanwhile we click whatever
    # primary action is on screen, in priority order: confirm popover
    # accept > raised button with a known label > any button with that
    # label. We track signatures so the same button on the same URL
    # only gets clicked once; URL changes reset the set.
    PRIMARY_LABELS = ['Get Started', 'Next', 'Continue', 'Install', 'OK',
                      'Recreate', 'Confirm', 'Accept', 'Allow', 'Yes', 'Finish']

    # Hardware-tile preference is driven by COMFY_TEST_CUDA (set by
    # _desktop_runner.py from --desktop_windows vs --desktop_windows_cuda),
    # not by what the wizard's auto-detect picks. On an NVIDIA box the
    # wizard pre-selects CUDA and enables Next/Install on entry, so without
    # forcing our own tile click we'd silently always install CUDA.
    _CUDA_MODE = os.environ.get('COMFY_TEST_CUDA', '0') == '1'
    if sys.platform == 'darwin':
        PREFERRED = ['Apple Silicon', 'MPS', 'M4', 'M3', 'M2', 'M1']
    elif _CUDA_MODE:
        PREFERRED = ['NVIDIA', 'CUDA', 'AMD', 'ROCm', 'DirectML', 'GPU']
    else:
        PREFERRED = ['CPU']
    log(f'  wizard: COMFY_TEST_CUDA={os.environ.get("COMFY_TEST_CUDA","0")} '
        f'platform={sys.platform} preferred={PREFERRED}')

    def server_up():
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{_COMFY_PORT or 8188}/system_stats', timeout=2)
            return True
        except Exception:
            return False

    def find_action(page):
        # Confirm popover (e.g., "Delete .venv" -> Recreate accept)
        try:
            loc = page.locator('button.p-confirmpopup-accept-button').first
            if loc.count() and loc.is_visible():
                return ('confirm', loc, f'CONFIRM|{(loc.text_content() or "").strip()}')
        except Exception:
            pass
        # Hardware tile FIRST when present and our preferred label is
        # available -- but only if we haven't already picked a tile on this
        # URL. The wizard pre-selects a default (CUDA on NVIDIA) so
        # Next/Install is enabled on entry; without picking our own tile
        # here, the button branch below would click Next and we'd silently
        # inherit the host's hardware default. After clicking once, the
        # tile stays visible (selection just toggles), so we must check
        # `clicked` here to fall through to buttons rather than re-returning
        # the same TILE| signature forever. URL change clears `clicked`.
        tile_already_picked = any(k.startswith('TILE|') for k in clicked)
        if not tile_already_picked:
            for pref in PREFERRED:
                try:
                    tile = page.locator(f'button.hardware-option:has-text("{pref}")').first
                    if tile.count() and tile.is_visible():
                        return ('tile', tile, f'TILE|{pref}')
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
        # Last-resort fallback: any non-disabled hardware tile (covers
        # boxes whose tile labels don't match any of our PREFERRED entries).
        # Same gate: don't return a tile if we've already picked one on
        # this URL -- otherwise after clicking CPU we'd flip back to
        # whatever .first happens to be (the wizard's NVIDIA tile).
        if not tile_already_picked:
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
            # Buttons may need re-clicking if the page didn't advance
            # (CLICK_TTL gate). Tiles don't advance the page on click --
            # once selected, find_action keeps returning the same tile
            # forever; suppress re-clicks until the URL changes (which
            # clears `clicked` at the top of the loop).
            if last_t and (kind == 'tile' or time.time() - last_t < CLICK_TTL):
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

    # ComfyUI Desktop's first-boot path triggers MULTIPLE Python-backend
    # restarts within the first 1-2 minutes (validate install, migrate,
    # reinstall packages, manager pulls, etc.). Each restart can kill
    # the chromium renderer's CDP target, breaking our `page` reference.
    # Wait for the backend to be CONTINUOUSLY UP for `stable_s` seconds
    # before proceeding with any UI actions. If the page dies during the
    # wait, reconnect via CDP and get a fresh page.
    def _reattach_after_close(old_browser):
        # Wait for CDP to be reachable again (in case Electron is mid-restart).
        for _i in range(120):
            try:
                urllib.request.urlopen(f'http://localhost:{_CDP_PORT}/json/version', timeout=1)
                break
            except Exception:
                time.sleep(1)
        else:
            log(f'  recovery: CDP never came back within 120s')
            return None, None
        try: old_browser.close()
        except Exception: pass
        try:
            _prune_blank_targets(_CDP_PORT)
            nb = p.chromium.connect_over_cdp(f'http://localhost:{_CDP_PORT}')
            _browser_ref[0] = nb
            _capture_warned[0] = False
            np = main_page(nb)
            if np is None:
                log('  recovery: no page after reconnect')
                return nb, None
            install_cursor(np)
            log(f'  recovery: reattached at {np.url}')
            return nb, np
        except Exception as e:
            log(f'  recovery: reconnect failed: {e}')
            return None, None

    log('  app: waiting for backend stability (continuous /system_stats up for 30s)')
    _stable_s = 30
    _max_s = 300
    _last_up = None
    _stab_start = time.time()
    while time.time() - _stab_start < _max_s:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{_COMFY_PORT or 8188}/system_stats', timeout=2)
            if _last_up is None:
                _last_up = time.time()
                log(f'  app: /system_stats up; awaiting {_stable_s}s of stability')
            elif time.time() - _last_up >= _stable_s:
                log(f'  app: backend stable for {_stable_s}s; proceeding')
                break
        except Exception:
            if _last_up is not None:
                log(f'  app: /system_stats went DOWN '
                    f'(was up {int(time.time()-_last_up)}s); waiting for re-up')
            _last_up = None
        # If the page itself died (Electron renderer reload / target close),
        # reconnect now so we have a live page going forward.
        try:
            _ = page.url
        except Exception as _pe:
            log(f'  app: page died during stability wait ({_pe!r}); reconnecting')
            _nb, _np = _reattach_after_close(browser)
            if _np is None:
                log('  app: reattach failed; bailing on stability wait')
                break
            browser = _nb
            page = _np
            _last_up = None  # restart stability counter post-reattach
        try: frame(page)
        except Exception: pass
        time.sleep(1)
    else:
        log(f'  app: backend never stabilized within {_max_s}s; proceeding anyway')

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
    # Cloud upsell was removed from Comfy Desktop 1.0.34+ -- don't waste
    # 30s polling for a button that isn't rendered anymore. If it comes
    # back in a future build, re-add here.
    POST_ACTIONS = [
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

    # Install via Manager's /customnode/install/git_url. This is the
    # dev variant of cdp_driver -- the production cdp_driver.py uses the
    # registry-tile UI flow for main-branch CNR installs. Here we POST
    # `https://github.com/<repo>@<branch>` to Manager's git-clone
    # endpoint; gitclone_install (manager_core.py:2169) parses
    # `<url>@<gitref>` via extract_url_and_commit_id and runs
    # `git checkout <gitref>` after cloning. Requires security_level=weak,
    # pre-written by _desktop_runner_dev._write_manager_security_config
    # before app launch.
    node_repo = os.environ.get('NODE_REPO', '')
    node_branch = os.environ.get('NODE_BRANCH', 'main')
    base = f'http://127.0.0.1:{_COMFY_PORT or 8188}'

    # ------------------------------------------------------------------
    # Manager-UI install flow (visible in CDP video):
    #
    #   1. Fetch node's DisplayName / PublisherId / version from its
    #      pyproject.toml so we can target the RIGHT tile in Manager's
    #      search results (multiple tiles can mention the same string).
    #   2. Type the display name into Manager's Search input.
    #   3. Click the tile matching name AND publisher.
    #   4. Open the Version dropdown and pick Nightly (guaranteed to
    #      clone from git; CNR versions may be missing downloadUrl).
    #   5. Click the Install button.
    #   6. Wait for the "Apply Changes" toast (Manager finished -- this
    #      is when git clone + pip install are done).
    #   7. BEFORE clicking Apply Changes: do the branch swap (git
    #      fetch/checkout/pull) so the pending restart picks up the
    #      target branch. Announce with a yellow banner.
    #   8. Click Apply Changes -> in-app restart picks up dev branch.
    #
    # Fallback if UI flow fails (tile not found etc): direct filesystem
    # clone + Manager reboot API. Invisible but guaranteed to work.
    # ------------------------------------------------------------------

    def _do_branch_swap_visibly(node_dir):
        """Run git fetch/checkout/pull with a yellow banner in the
        ComfyUI window announcing each command."""
        commands = [
            ['git', '-C', str(node_dir), 'fetch', 'origin', node_branch],
            ['git', '-C', str(node_dir), 'checkout', node_branch],
            ['git', '-C', str(node_dir), 'pull', 'origin', node_branch],
        ]
        banner_lines = [
            f'TEST HARNESS - swapping to `{node_branch}` branch:',
            *[f'  $ {" ".join(c)}' for c in commands],
        ]
        log(f'  ext: showing disclaimer for branch swap ({node_branch})')
        _show_test_disclaimer(page, banner_lines, duration=None)
        ok = True
        try:
            for cmd in commands:
                log(f'  ext: {" ".join(cmd[0:2])} {" ".join(cmd[3:])}')
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    log(f'  ext: FAIL (exit {r.returncode}): {r.stderr[:300]}')
                    ok = False
                    break
            time.sleep(2)   # let the viewer see the completed commands
        finally:
            _hide_test_disclaimer(page)
        return ok

    def _post_empty(path):
        req = urllib.request.Request(f'{base}{path}', data=b'', method='POST')
        return urllib.request.urlopen(req, timeout=30)

    def _reboot_via_manager_and_wait():
        """Fallback path (no Apply Changes toast). POST Manager's reboot
        endpoint, then poll /system_stats."""
        log('  ext: rebooting ComfyUI via Manager API (fallback path)')
        for reboot_path in ('/api/v2/manager/reboot', '/v2/manager/reboot'):
            try:
                with _post_empty(reboot_path) as resp:
                    log(f'  ext: {reboot_path} -> HTTP {resp.status}')
                    break
            except urllib.error.HTTPError as e:
                log(f'  ext: {reboot_path} HTTP {e.code} (server may be restarting)')
                if e.code in (404, 405):
                    continue
                break
            except Exception as e:
                log(f'  ext: {reboot_path} failed: {e}; trying next')
        log('  ext: waiting for /system_stats after reboot')
        time.sleep(3)
        for i in range(180):
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{_COMFY_PORT or 8188}/system_stats', timeout=2)
                log(f'  ext: /system_stats back up after {(i+1)*2}s')
                return True
            except Exception:
                time.sleep(2)
        return False

    # ------------------------------------------------------------------
    # Fetch the node's DisplayName / PublisherId / version from its
    # pyproject.toml -- same helper as production cdp_driver.py.
    # ------------------------------------------------------------------
    def _fetch_node_meta():
        if not node_repo:
            return None, None, None
        url = f'https://raw.githubusercontent.com/{node_repo}/{node_branch}/pyproject.toml'
        try:
            body = urllib.request.urlopen(url, timeout=10).read().decode('utf-8')
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib   # type: ignore
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

    ui_install_done = False
    if NODE_DISPLAY_NAME and PUBLISHER:
        try:
            sleep_capturing(page, 3, fps=5)
            log(f'  ext: searching "{NODE_DISPLAY_NAME}"')
            fill_with_cursor(page, 'input[placeholder="Search"]:visible', NODE_DISPLAY_NAME)
            sleep_capturing(page, 2, fps=5)

            log(f'  ext: clicking {NODE_DISPLAY_NAME} by {PUBLISHER} tile')
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
                log('  ext: tile not found in Manager search results')
                raise RuntimeError('tile not found')
            sleep_capturing(page, 3, fps=5)

            # Version selector -> pick Nightly (guaranteed to git-clone
            # from CNR repository field; other versions may 404 on
            # missing downloadUrl).
            log('  ext: opening version selector')
            try:
                vt = page.locator('div[role="button"][aria-haspopup="true"].bg-dialog-surface:visible').first
                if vt.count():
                    vt.scroll_into_view_if_needed()
                    sleep_capturing(page, 1, fps=5)
                    click_with_cursor(page, vt)
                    sleep_capturing(page, 1, fps=5)
                    picked = False
                    for label in ('Nightly', 'Latest'):
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
                        log('  ext: no Nightly/Latest option matched, dismissing')
                        try: page.keyboard.press('Escape')
                        except Exception: pass
            except Exception as e:
                log(f'  ext: version selector failed: {e}')

            # Right-panel Install button (LAST "Install" in DOM order --
            # each middle-column tile also has an inline Install).
            log('  ext: clicking right-panel Install')
            btns = page.locator('button:has-text("Install"):visible')
            n = btns.count()
            if not n:
                log('  ext: no visible Install button')
                raise RuntimeError('no Install button')
            btn = btns.nth(n - 1)
            btn.scroll_into_view_if_needed()
            sleep_capturing(page, 1, fps=5)
            click_with_cursor(page, btn)
            log(f'  ext: clicked Install (last of {n} visible)')
            sleep_capturing(page, 8, fps=5)

            # Wait for "Apply Changes" toast -- this is when Manager
            # finished git-clone + pip install. Timeout generous because
            # CADabra pulls pixi + several isolation envs.
            log('  ext: waiting for "Apply Changes" toast')
            applied_deadline = time.time() + 900
            apply_btn = None
            while time.time() < applied_deadline:
                try:
                    ac = page.locator('button:has-text("Apply Changes"):visible').first
                    if ac.count() and ac.is_visible() and not ac.is_disabled():
                        apply_btn = ac
                        break
                except Exception:
                    pass
                sleep_capturing(page, 1, fps=5)

            if apply_btn is None:
                log('  ext: Apply Changes never appeared; skipping branch swap')
                raise RuntimeError('no Apply Changes toast')

            # Manager finished. BEFORE clicking Apply Changes, swap the
            # branch on disk so the imminent restart picks up dev.
            log('  ext: Manager install done -- running branch swap before Apply Changes')
            try:
                _, _, custom_nodes, _ = _find_active_comfy_install()
                node_dir = None
                for candidate in (custom_nodes / node_repo.split('/')[-1].lower(),
                                  custom_nodes / node_repo.split('/')[-1]):
                    if (candidate / '.git').exists():
                        node_dir = candidate
                        break
                if node_dir is None:
                    log(f'  ext: WARNING: no .git in {custom_nodes}/* -- Manager may have installed via CNR zip, skipping branch swap')
                else:
                    log(f'  ext: node installed at {node_dir}')
                    _do_branch_swap_visibly(node_dir)
            except Exception as e:
                log(f'  ext: branch swap failed (non-fatal): {e}')

            # NOW click Apply Changes -- Manager restarts ComfyUI backend
            # in-place; our `page` handle stays valid.
            log('  ext: clicking Apply Changes')
            click_with_cursor(page, apply_btn)
            time.sleep(3)
            log('  ext: waiting for /system_stats after Apply Changes')
            for i in range(180):
                try:
                    urllib.request.urlopen(
                        f'http://127.0.0.1:{_COMFY_PORT or 8188}/system_stats', timeout=2)
                    log(f'  ext: /system_stats back up after {(i+1)*2}s')
                    ui_install_done = True
                    break
                except Exception:
                    time.sleep(2)
            if not ui_install_done:
                log('  ext: /system_stats never came back after Apply Changes')

            # Verify Manager actually installed the node. Common failure
            # mode: Manager's cnr_install silently fails when CADabra's
            # latest_version.downloadUrl is empty, but Apply Changes
            # still shows. Detect the empty custom_nodes/<name> dir and
            # fall through to filesystem clone.
            if ui_install_done:
                _, _, custom_nodes, _ = _find_active_comfy_install()
                installed = any(
                    (custom_nodes / n).is_dir()
                    for n in (node_repo.split('/')[-1].lower(),
                              node_repo.split('/')[-1])
                )
                if not installed:
                    log(f'  ext: WARNING: no {node_repo.split("/")[-1]} dir in '
                        f'{custom_nodes} after Apply Changes -- Manager UI '
                        f'reported success but nothing installed. Falling '
                        f'back to filesystem clone.')
                    ui_install_done = False
        except Exception as e:
            log(f'  ext: UI install flow failed: {e.__class__.__name__}: {e}')
            ui_install_done = False

    if not ui_install_done:
        # --------------------------------------------------------------
        # Fallback: direct filesystem clone (branch-pinned from start).
        # --------------------------------------------------------------
        log('  ext: falling back to filesystem clone + Manager reboot')
        node_name = node_repo.split('/')[-1]
        install_path, comfy_root, custom_nodes, venv_python = _find_active_comfy_install()
        if not custom_nodes.exists():
            raise RuntimeError(f'custom_nodes dir missing: {custom_nodes}')
        log(f'  ext:   install_path = {install_path}')
        log(f'  ext:   custom_nodes = {custom_nodes}')
        log(f'  ext:   venv python  = {venv_python}')

        node_dir = custom_nodes / node_name
        if node_dir.exists():
            log(f'  ext:   {node_dir} exists -- removing before fresh clone')
            subprocess.run(['rm', '-rf', str(node_dir)], check=True)
        clone_cmd = ['git', 'clone', '--depth', '1', '-b', node_branch,
                     f'https://github.com/{node_repo}.git', str(node_dir)]
        log(f'  ext:   $ {" ".join(clone_cmd)}')
        r = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f'git clone failed: {r.stderr[:400]}')

        reqs = node_dir / 'requirements.txt'
        if reqs.exists() and venv_python.exists():
            pip_cmd = [str(venv_python), '-m', 'pip', 'install', '--no-input',
                       '-r', str(reqs)]
            log(f'  ext:   $ {" ".join(pip_cmd)}')
            r = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=900)
            for line in (r.stdout + r.stderr).splitlines()[-15:]:
                log(f'  ext:     {line}')
            if r.returncode != 0:
                log(f'  ext:   WARNING: pip install exit={r.returncode} (continuing)')
        else:
            log('  ext:   no requirements.txt or no venv python -- skipping pip')

        _reboot_via_manager_and_wait()

    # Manager already rebooted ComfyUI via /api/v2/manager/reboot in
    # _reboot_and_wait() above. Skip the driver's kill+relaunch of the
    # whole Electron app entirely -- that path was fragile (hung on
    # connect_over_cdp) and unnecessary now that Manager restarts just
    # the Python server. Our existing `page`/`browser` handles stay
    # valid; the renderer just needs a page.reload() (done below) to
    # reconnect to the fresh backend.
    log('  app: skipping kill+relaunch -- Manager reboot did it')
    clicked_tile = True  # noqa: F841 -- kept for downstream if/else scaffold
    if not clicked_tile:
        log('  ext: unreachable')
    else:
        sleep_capturing(page, 3, fps=5)
        # Server was confirmed up in _reboot_and_wait, but assert here
        # so downstream stages get a fresh check + a log line.
        for i in range(60):
            if server_up():
                log(f'  app: server ready after {i+1}s')
                break
            frame(page)
            time.sleep(1)
            # Server is up but the renderer might still be on a splash
            # (#/desktop-start, #/server-start) or -- on Windows when
            # there's no GPU -- #/not-supported. Worse, custom-node
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

            # Force a renderer reload after the post-Apply-Changes Electron
            # relaunch. The Templates dialog's left-nav caches its node-pack
            # list at first JS-bundle init; even after the Electron process
            # relaunches and reconnects to the new Python backend, the
            # renderer's cached manifest does NOT include freshly-installed
            # node packs. Without this reload the EXTENSIONS section (with
            # comfyui-cadabra etc.) is missing from the nav. Verified
            # interactively via CDP: BEFORE reload -- 12 categories ending at
            # "Partner Nodes"; AFTER reload -- 14 categories adding
            # "EXTENSIONS / ComfyUI-GeometryPack / comfyui-cadabra".
            log('  app: forcing renderer reload to refresh node-pack manifest')
            try:
                page.reload(wait_until='load', timeout=30000)
                install_cursor(page)
                # Re-wait for canvas after the reload -- same shape as the
                # initial wait, smaller budget since backend is already up.
                for i in range(60):
                    try:
                        ready = page.evaluate(
                            "typeof window.app !== 'undefined' "
                            "&& window.app.graph !== undefined")
                        if ready:
                            log(f'  app: canvas re-ready after reload in {i+1}s')
                            break
                    except Exception:
                        pass
                    frame(page)
                    time.sleep(1)
                sleep_capturing(page, 3, fps=5)
            except Exception as e:
                log(f'  app: post-relaunch reload failed: {e}')

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
        # "Node Pack Issues Detected!" -- a Vue modal warning about
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
                # Earlier this loop scrolled `nav .scrollbar-hide.overflow-y-auto:visible`
                # via .first + el.scrollBy(...). Frames from a recent run
                # (CADabra-1248 macos-desktop) showed the panel state was
                # IDENTICAL across 33 seconds of scroll iterations -- the
                # scroll was a no-op. Two reasons it failed:
                #   1. .first arbitrarily picked one match among several
                #      overflow-y-auto divs in the dialog (the right-panel
                #      template grid scrolls too); could be wrong element.
                #   2. We never verified scrollTop actually changed, so a
                #      no-op scrollBy looked the same as a successful one.
                # Fix: pick the actual scrollable left-sidebar by max
                # (scrollHeight - clientHeight), keep scrolling while
                # scrollTop is still moving (= we haven't hit the floor),
                # and continue regardless of whether the section is found.
                # That way a virtualized list that lazy-renders below the
                # current viewport still gets fully traversed.
                find_panel_js = """
                () => {
                  const dialogs = Array.from(document.querySelectorAll(
                    'div[role="dialog"], aside, nav'
                  ));
                  const scrollables = [];
                  dialogs.forEach(d => {
                    d.querySelectorAll('*').forEach(el => {
                      const cs = getComputedStyle(el);
                      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                          && el.scrollHeight > el.clientHeight + 4) {
                        scrollables.push(el);
                      }
                    });
                  });
                  // Prefer the candidate inside a <nav> (left sidebar) over
                  // ones inside the right-panel grid. Fall back to the one
                  // with the largest scroll range.
                  const nav_first = scrollables.find(el => el.closest('nav'));
                  const chosen = nav_first || scrollables.sort(
                    (a,b) => (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight)
                  )[0];
                  if (!chosen) return null;
                  chosen.setAttribute('data-driver-scroll', '1');
                  return {
                    scrollHeight: chosen.scrollHeight,
                    clientHeight: chosen.clientHeight,
                    scrollTop: chosen.scrollTop,
                  };
                }
                """
                info = page.evaluate(find_panel_js)
                if info:
                    log(f'  ext: scroll target found (scrollHeight={info["scrollHeight"]} '
                        f'clientHeight={info["clientHeight"]})')
                else:
                    log('  ext: no scrollable panel found, falling back to PageDown')

                step_js = """
                () => {
                  const el = document.querySelector('[data-driver-scroll="1"]');
                  if (!el) return null;
                  const before = el.scrollTop;
                  el.scrollBy(0, Math.max(40, el.clientHeight * 0.7));
                  return {
                    before,
                    after: el.scrollTop,
                    max: el.scrollHeight - el.clientHeight,
                  };
                }
                """
                stuck = 0
                last_top = -1
                MAX_ITERS = 60
                for i in range(MAX_ITERS):
                    res = page.evaluate(step_js) if info else None
                    if res is None:
                        try: page.keyboard.press('PageDown')
                        except Exception: pass
                    else:
                        if res['after'] == res['before']:
                            stuck += 1
                        else:
                            stuck = 0
                        last_top = res['after']
                        # Hit the floor: scrollTop didn't move 2 iterations
                        # in a row AND we're at scrollHeight - clientHeight.
                        at_floor = (res['after'] >= res['max'] - 2 and stuck >= 2)
                        if at_floor:
                            log(f'  ext: reached scroll floor at iter {i+1} '
                                f'(scrollTop={res["after"]} max={res["max"]})')
                            sleep_capturing(page, 1, fps=5)
                            node_section, hit_sel = find_node_section()
                            break
                    sleep_capturing(page, 0.7, fps=5)
                    node_section, hit_sel = find_node_section()
                    if node_section is not None:
                        log(f'  ext: scrolled {i+1}x to {NODE_PACKAGE_NAME} ({hit_sel}, scrollTop={last_top})')
                        break
                else:
                    log(f'  ext: ran {MAX_ITERS} scroll iters, last scrollTop={last_top}')
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
        #   - cpu = "all"            -> any card
        #   - cpu = ["a","b"]        -> only "a" or "b"
        #   - cpu = ["!a"] (any !)   -> any card except those listed
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
                    # Read .cuda when COMFY_TEST_CUDA=1, else .cpu. Earlier
                    # this was hardcoded to 'cpu' which silently picked the
                    # wrong workflow on --desktop_windows_cuda (the spec's
                    # cpu-mode exclude list happened to allow alpha_wrap).
                    spec_key_inline = 'cuda' if os.environ.get('COMFY_TEST_CUDA', '0') == '1' else 'cpu'
                    spec_inline = data.get('test', {}).get('workflows', {}).get(spec_key_inline)
                    if spec_inline == 'all' or spec_inline is None:
                        cpu_mode = 'all'
                    elif isinstance(spec_inline, list):
                        excludes = [f.lstrip('!') for f in spec_inline if isinstance(f, str) and f.startswith('!')]
                        if excludes:
                            cpu_mode = 'exclude'
                            cpu_items = [e[:-5] if e.endswith('.json') else e for e in excludes]
                        else:
                            cpu_mode = 'include'
                            cpu_items = [f[:-5] if f.endswith('.json') else f for f in spec_inline]
                    log(f'  ext: {spec_key_inline} spec = {cpu_mode} {cpu_items}')
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

            # Snapshot fi[0] before the first workflow's Run so we can slice
            # its frame range out of the global frame counter for per-workflow
            # video encoding at the end of the run.
            _first_workflow_frame_start = fi[0]
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
                # err is the raw msg.data from execution_error -- typically
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

        # Multi-workflow loop. The block above ran the FIRST matching
        # workflow inline (current behavior). For each remaining matching
        # workflow we kill ComfyUI, relaunch, reconnect Playwright, reload
        # the renderer (refreshes the templates manifest with installed
        # packs), navigate to Templates -> comfyui-cadabra section, click
        # the named card, and run it. Frame-index ranges are tracked so
        # the post-loop ffmpeg pass can emit one mp4 per workflow.
        _frame_ranges = []
        if _workflow_results:
            _frame_ranges.append((
                _workflow_results[0]['name'],
                _first_workflow_frame_start if 'picked_name' in dir() and picked_name else 0,
                fi[0],
            ))
        # Workflow list is comfy-test-driven, NOT GUI-driven: we read the
        # cpu/gpu spec from the node repo's comfy-test.toml and (for
        # 'all' or '!exclude' modes) the full list from the repo's
        # workflows/ directory contents. This avoids the failure mode
        # where the Templates panel happens to be closed (or scrolled
        # past the section) at enumeration time.
        NODE_PACKAGE_NAME_outer = os.environ.get('NODE_NAME', 'comfyui-sam3').lower()
        try:
            cpu_mode_outer, cpu_items_outer = _parse_cpu_spec()
            if cpu_mode_outer == 'include':
                _all_matching = list(cpu_items_outer)
            else:
                _full_list = _fetch_workflow_list_from_repo()
                if cpu_mode_outer == 'exclude':
                    _all_matching = [n for n in _full_list if n not in cpu_items_outer]
                else:  # 'all'
                    _all_matching = _full_list
            log(f'  loop: spec={cpu_mode_outer} items={cpu_items_outer} '
                f'-> {len(_all_matching)} workflow(s) {_all_matching}')
        except Exception as e:
            log(f'  loop: workflow list resolution failed: {e}')
            _all_matching = []
        first_name = _workflow_results[0]['name'] if _workflow_results else None
        _remaining = [n for n in _all_matching if n != first_name]
        log(f'  loop: matching={len(_all_matching)} first_ran={first_name!r} '
            f'remaining={len(_remaining)} -> {_remaining}')

        NODE_PACKAGE_NAME_outer = os.environ.get('NODE_NAME', 'comfyui-sam3').lower()
        for _idx, _wf_name in enumerate(_remaining):
            log(f'  loop: full restart for workflow {_idx+2}/{len(_all_matching)} ({_wf_name})')
            page, browser = _restart_comfy(p, browser)
            if page is None:
                log('  loop: restart failed (no page); bailing out of remaining workflows')
                break
            _dismiss_post_restart_modals(page)
            if not _open_templates_and_section(page, NODE_PACKAGE_NAME_outer):
                log(f'  loop: Templates+section open failed for {_wf_name}; recording fail')
                _workflow_results.append({
                    'name': _wf_name, 'status': 'fail',
                    'duration_seconds': 0,
                    'error': 'Templates+section not openable after restart',
                })
                continue
            _start = fi[0]
            _result = _run_named_card(page, _wf_name)
            _workflow_results.append(_result)
            _frame_ranges.append((_wf_name, _start, fi[0]))

    snap(page, 'final')
    log(f'Captured {fi[0]} frames')
    browser.close()

# Write results.json at the run root. Schema matches cpu's
# orchestration/levels/execution.py: timestamp, platform, hardware,
# commit_hash, success, summary, workflows. The dashboard's
# comfy_ci.py:_check_ghpages_result reads success+commit_hash to
# decide pass/fail/stale -- writing only `workflows` makes it render
# as a stale-empty cell even on a green run.
import platform as _platform
from datetime import datetime as _dt, timezone as _tz

def _hardware_info():
    info = {"os": _platform.platform(), "cpu": _platform.processor() or "Unknown"}
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if gpu.returncode == 0 and gpu.stdout.strip():
            info["cuda"] = gpu.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return info

def _comfyui_version():
    """ComfyUI core version from the app's own server (/system_stats). None on failure."""
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/system_stats', timeout=5) as r:
            return (json.loads(r.read()).get('system') or {}).get('comfyui_version')
    except Exception:
        return None

_passed = sum(1 for w in _workflow_results if w.get("status") == "pass")
_failed = sum(1 for w in _workflow_results if w.get("status") == "fail")
_results_data = {
    "timestamp":   _dt.now(_tz.utc).isoformat(),
    "platform":    os.environ.get("COMFY_TEST_DESKTOP_PLATFORM", "unknown_desktop"),
    "hardware":    _hardware_info(),
    "comfyui_version": _comfyui_version(),
    "commit_hash": os.environ.get("COMFY_TEST_NODE_SHA") or None,
    # GHA run URL for Goto-mode in the dashboard. Set by dispatch-test.yml's
    # job-level env (github.* expansion).
    "run_url":     os.environ.get("COMFY_TEST_RUN_URL") or None,
    "success":     _failed == 0 and _passed > 0,
    "summary":     {"total": len(_workflow_results), "passed": _passed, "failed": _failed},
    "workflows":   _workflow_results,
}
_results_path = _RUN_DIR / 'results.json'
try:
    _results_path.write_text(json.dumps(_results_data, indent=2), encoding='utf-8')
    log(f'Wrote {_results_path} ({len(_workflow_results)} workflow(s), '
        f'sha={_results_data["commit_hash"][:12] if _results_data["commit_hash"] else "none"})')
except Exception as e:
    log(f'results.json write failed: {e}')

# imageio-ffmpeg ships a static ffmpeg binary so we don't need a system install.
try:
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as e:
    log(f'imageio-ffmpeg unavailable ({e}); falling back to PATH ffmpeg')
    ffmpeg_exe = 'ffmpeg'

# Master mp4 covers the entire run (wizard + installs + every workflow).
# Useful for end-to-end debugging; per-workflow mp4s are sliced from the
# global frame sequence below using `_frame_ranges` populated by the loop.
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

# Per-workflow mp4 encoding. Each entry in _frame_ranges is
# (workflow_name, start_idx, end_idx). frame_NNNNNN.png is 1-indexed
# (fi[0] is incremented BEFORE the screenshot), so ffmpeg's
# -start_number is start_idx+1 and -frames:v is the count.
_frame_ranges_local = locals().get('_frame_ranges', [])
videos_root = _RUN_DIR / 'videos'
try:
    for wf_name, start_idx, end_idx in _frame_ranges_local:
        count = max(1, end_idx - start_idx)
        wf_dir = videos_root / wf_name
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf_mp4 = wf_dir / 'driver.mp4'
        try:
            subprocess.run([
                ffmpeg_exe, '-y',
                '-start_number', str(start_idx + 1),
                '-framerate', '5',
                '-i', str(FRAMES / 'frame_%06d.png'),
                '-frames:v', str(count),
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                str(wf_mp4),
            ], check=True)
            log(f'  videos/{wf_name}/driver.mp4 placed (frames {start_idx+1}..{end_idx})')
        except Exception as e:
            log(f'  videos/{wf_name}/driver.mp4 encode failed: {e}')
        # Per-workflow thumbnail for the html report's card grid:
        # screenshots/<wf>_executed.png is what html_report.py:182 looks up.
        # Use the LAST captured frame in the workflow's range so the
        # thumbnail shows the workflow's final UI state, matching cpu/gpu's
        # capture_execution_frames end-of-execution screenshot.
        try:
            import shutil as _shot_shutil
            last_frame = FRAMES / f'frame_{end_idx:06d}.png'
            shot_dir = _LOGS_DIR / 'screenshots'
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_path = shot_dir / f'{wf_name}_executed.png'
            if last_frame.exists():
                _shot_shutil.copyfile(str(last_frame), str(shot_path))
                log(f'  screenshots/{wf_name}_executed.png placed')
            else:
                log(f'  screenshots/{wf_name}_executed.png skipped -- frame {end_idx} not on disk')
        except Exception as e:
            log(f'  screenshots/{wf_name}_executed.png copy failed: {e}')
        wf_meta = next((r for r in _workflow_results if r.get('name') == wf_name), {})
        (wf_dir / 'metadata.json').write_text(json.dumps({
            'mp4': 'driver.mp4',
            'duration_seconds': wf_meta.get('duration_seconds') or 0,
            'status': wf_meta.get('status') or 'unknown',
        }, indent=2), encoding='utf-8')
    # If no workflows ran (or _frame_ranges is empty), fall back to the
    # legacy 'system' copy so the html report still has something to show.
    if not _frame_ranges_local and mp4.exists() and mp4.stat().st_size > 0:
        import shutil
        sys_dir = videos_root / 'system'
        sys_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(mp4), str(sys_dir / 'driver.mp4'))
        (sys_dir / 'metadata.json').write_text(json.dumps({
            'mp4': 'driver.mp4', 'duration_seconds': 0, 'status': 'pass',
        }, indent=2), encoding='utf-8')
        log('  videos/system/driver.mp4 placed (no workflows ran)')
except Exception as e:
    log(f'  videos/ placement failed: {e}')

# Drop the per-frame PNGs once the mp4 is encoded -- they're only the
# raw input to ffmpeg and bloat both the artifact and gh-pages.
try:
    if mp4.exists() and mp4.stat().st_size > 0:
        import shutil
        shutil.rmtree(FRAMES, ignore_errors=True)
        log(f'  removed {FRAMES} after successful encode')
except Exception as e:
    log(f'  frames cleanup failed: {e}')
