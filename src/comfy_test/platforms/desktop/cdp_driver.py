import atexit, json, os, re as _re, shutil, subprocess, sys, threading, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

# ComfyUI HTTP port. Discovered lazily inside _walk_first_run_wizard's
# _discover_comfy_port() from CDP's page list (whichever 127.0.0.1:<port>
# URL is loaded is the right one). Comfy Desktop 1.0.34 uses 8188; older
# builds used 8000. All downstream server checks read this instead of
# hardcoding.
_COMFY_PORT = None

# This driver relays ComfyUI's own log file (and third-party node output) to
# stdout. That content is arbitrary UTF-8, while a Windows console defaults to
# cp1252 -- printing e.g. U+2192 there raises UnicodeEncodeError and kills the
# run mid-test. Re-encode our streams as UTF-8 and never fail on an
# unrepresentable character. _desktop_runner reads this pipe with a matching
# encoding=utf-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass   # already-wrapped or non-reconfigurable stream; log() still guards

t0 = time.time()
import builtins as _b
def log(*a, **k):
    try:
        _b.print(f'[{int(time.time()-t0):4d}s]', *a, **k, flush=True)
    except UnicodeEncodeError:
        # Belt and braces: if stdout could not be reconfigured above, degrade
        # the message rather than aborting the test run over a log line.
        enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
        safe = [str(x).encode(enc, errors='replace').decode(enc, errors='replace')
                for x in a]
        _b.print(f'[{int(time.time()-t0):4d}s]', *safe, **k, flush=True)

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

# Playwright's default screenshot timeout is 30s. When the compositor is not
# committing frames (occluded/minimized window, no GPU surface) captureScreenshot
# never returns, so every capture burns the full 30s and a run crawls -- while
# page.evaluate and clicks keep working fine, because those need no frame.
# Fail fast instead: a missing frame is cosmetic, a stalled run is not.
_SHOT_TIMEOUT_MS = int(os.environ.get('COMFY_TEST_SHOT_TIMEOUT_MS', '5000'))

# Per-workflow execution ceiling. 600s x 21 workflows is a 3.5-hour run when
# nothing completes, which makes each debugging cycle unusable. Override with
# COMFY_TEST_WORKFLOW_TIMEOUT_S; raise it for real CUDA workloads that
# legitimately take minutes per graph.
_WORKFLOW_TIMEOUT_S = int(os.environ.get('COMFY_TEST_WORKFLOW_TIMEOUT_S', '180'))


def snap(page, name):
    # The DOM dump is the useful half and costs nothing, so write it even when
    # the image cannot be captured.
    try:
        (OUT / f'{name}.html').write_text(page.content(), encoding='utf-8')
    except Exception:
        pass
    if _capture_disabled[0]:
        return
    try:
        page.screenshot(path=str(OUT / f'{name}.png'), full_page=True,
                        timeout=_SHOT_TIMEOUT_MS)
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

def _log_capture_diagnostics(page):
    """Explain WHY captureScreenshot is hanging, once, on first failure.

    A minimized or hidden Electron window has no compositor surface, so
    Page.captureScreenshot waits for a frame that never arrives -- while
    page.evaluate keeps working, since the DOM needs no frame. That
    asymmetry is the signature, and windowState names the cause.
    """
    # JS facts first: page.evaluate keeps working when capture does not, so
    # this always yields something. document.visibilityState == 'hidden' is
    # the decisive tell -- chromium reports that for a minimized/occluded
    # window, and that is exactly when it stops committing frames.
    try:
        info = page.evaluate("""() => ({
            vis: document.visibilityState,
            hidden: document.hidden,
            w: window.innerWidth, h: window.innerHeight,
            ow: window.outerWidth, oh: window.outerHeight,
            dpr: window.devicePixelRatio,
            canvases: Array.from(document.querySelectorAll('canvas'))
                           .map(c => c.width + 'x' + c.height).slice(0, 4),
        })""")
        log(f'  capture-diag: visibility={info["vis"]} hidden={info["hidden"]} '
            f'inner={info["w"]}x{info["h"]} outer={info["ow"]}x{info["oh"]} '
            f'dpr={info["dpr"]} canvases={info["canvases"]}')
        if info.get('hidden') or info.get('vis') == 'hidden':
            log('  capture-diag: page is HIDDEN -- chromium stops committing '
                'frames, so captureScreenshot/startScreencast cannot return')
        if not info.get('w') or not info.get('h'):
            log('  capture-diag: zero-size viewport -- nothing to composite')
    except Exception as e:
        log(f'  capture-diag: evaluate failed ({e})')

    # A never-settling animation is the prime suspect: capture only fails
    # from ~5s after the Templates modal is ESC-dismissed. If its leave
    # transition never finishes, the page animates forever -- and both
    # captureScreenshot (waits for a presented frame) and playwright's
    # "stable" actionability check (compares bounding boxes across two
    # animation frames) can then never succeed.
    try:
        anim = page.evaluate("""() => {
            const running = (document.getAnimations ? document.getAnimations() : [])
                .filter(a => a.playState === 'running');
            const desc = running.slice(0, 6).map(a => {
                const t = a.effect && a.effect.target;
                const nm = t ? (t.tagName || '') + (t.className
                          ? '.' + String(t.className).split(/\\s+/).slice(0, 2).join('.')
                          : '') : '?';
                return (a.constructor && a.constructor.name || 'Animation') + '<' + nm + '>';
            });
            const overlays = ['.p-dialog-mask', '.p-overlay', '[role="dialog"]',
                              '.p-component-overlay', '.p-drawer-mask']
                .map(s => s + '=' + document.querySelectorAll(s).length)
                .join(' ');
            return {n: running.length, desc, overlays,
                    infinite: running.filter(a => {
                        try { return a.effect.getTiming().iterations === Infinity; }
                        catch (e) { return false; }
                    }).length};
        }""")
        log(f'  capture-diag: running animations={anim["n"]} '
            f'(infinite={anim["infinite"]}) {anim["desc"]}')
        log(f'  capture-diag: overlays {anim["overlays"]}')
        if anim['n']:
            log('  capture-diag: an animation is still running -- this alone '
                'can stop captureScreenshot from ever returning a frame')
    except Exception as e:
        log(f'  capture-diag: evaluate failed ({e})')

    # Browser.getWindowForTarget lives on the BROWSER session, not a page
    # session -- asking a page session returns "wasn't found".
    try:
        br = _browser_ref[0]
        if br is None:
            return
        sess = br.new_browser_cdp_session()
        try:
            tid = getattr(page, '_target_id', None)
            info = (sess.send('Browser.getWindowForTarget', {'targetId': tid})
                    if tid else sess.send('Browser.getWindowForTarget'))
            b = info.get('bounds', {})
            log(f'  capture-diag: windowState={b.get("windowState")} '
                f'size={b.get("width")}x{b.get("height")} '
                f'pos={b.get("left")},{b.get("top")}')
        finally:
            try:
                sess.detach()
            except Exception:
                pass
    except Exception as e:
        log(f'  capture-diag: window query unavailable ({e})')


# Set when neither the surface nor the renderer path can produce an image.
# frame() then becomes a no-op so the run proceeds at normal speed.
_capture_disabled = [False]
# Consecutive frame() failures. Capture is only disabled after a sustained
# streak: a SINGLE transient miss (page mid-navigation, server restart)
# used to disable capture permanently, which is why sandbox runs whose
# Electron phase disabled capture and whose browser-ui phase re-enabled it
# still ended with "Captured 1 frames" and no video (measured
# GeometryPack-2208: the first post-re-enable frame failed once, capture
# died for the remaining 10 minutes of perfectly capturable chrome).
_capture_fail_streak = [0]
_CAPTURE_FAIL_LIMIT = 10


def frame(page):
    if _capture_disabled[0]:
        # Capture is known-broken on this host. Return before touching the
        # counter: fi[0] is what the run reports as "Captured N frames" and
        # what ffmpeg's frame_%06d.png pattern is built from, so counting
        # frames we never wrote produced "Captured 1877 frames" against an
        # empty dir and then an ffmpeg "Could find no file with path" error.
        return
    # Reserve the next index only on success: a failed attempt must not
    # burn an index, or the frame_%06d sequence gets holes and ffmpeg
    # stops encoding at the first gap.
    path = str(FRAMES / f'frame_{fi[0] + 1:06d}.png')
    try:
        page.screenshot(path=path, timeout=_SHOT_TIMEOUT_MS)
        fi[0] += 1
        _capture_fail_streak[0] = 0
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
            fresh.screenshot(path=path, timeout=_SHOT_TIMEOUT_MS)
            fi[0] += 1
            _capture_fail_streak[0] = 0
        except Exception as e2:
            _capture_fail_streak[0] += 1
            if not _capture_warned[0]:
                log(f'  frame: capture failed '
                    f'({_capture_fail_streak[0]}/{_CAPTURE_FAIL_LIMIT}): {e2}')
                _log_capture_diagnostics(page)
                _capture_warned[0] = True
            if _capture_fail_streak[0] < _CAPTURE_FAIL_LIMIT:
                # Transient (navigation, restart in progress): keep trying.
                return
            # Deliberately NO fromSurface=false retry here. That path cannot
            # help and actively hangs: chromium's page_handler.cc bails out
            # before any emulation handling ("We don't support clip/emulation
            # when capturing from window"), so it could never return the
            # emulated viewport anyway, and on Windows it resolves to
            # PrintWindow, which blocks indefinitely on a window that is not
            # presenting. A raw CDPSession.send has no timeout, so that call
            # wedged the whole driver a few seconds after this diagnostic --
            # runs before it was added got 300s further.
            #
            # Frames are cosmetic (video + report thumbnails). results.json
            # and every workflow assertion are unaffected, so give up on
            # capture and let the run proceed.
            _capture_disabled[0] = True
            log(f'  frame: {_CAPTURE_FAIL_LIMIT} consecutive capture failures '
                '-- disabling frame capture for this run so it can proceed. '
                'No further frames will be produced; results.json and '
                'workflow assertions are unaffected.')

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
    tg = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{_CDP_PORT}/json').read())
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
        def _blank_ids():
            pages = json.loads(urllib.request.urlopen(
                f'http://127.0.0.1:{cdp_port}/json/list', timeout=2).read())
            return {p['id']: p for p in pages
                    if p.get('type') == 'page' and not p.get('url')}

        # Sample TWICE and only close what is blank in both.
        #
        # A single sample cannot tell "electron helper stuck in limbo forever"
        # apart from "the real main window, blank for the moment because it is
        # mid-navigation". At the wizard->main handoff Comfy Desktop's main
        # window is briefly blank while it navigates to 127.0.0.1:8188, and
        # closing it there destroys the BrowserWindow outright.
        #
        # Measured in CADabra-1355: the app is on screen at t+131s, this
        # function closes a blank target at t+132s, and from t+133s onward the
        # screen is a static desktop with no Comfy taskbar entry -- while the
        # 8188 page survives, so CDP attaches and the DOM stays queryable. With
        # no window there is no compositor surface: Page.captureScreenshot
        # hangs, startScreencast emits nothing, requestAnimationFrame stops,
        # and playwright's "stable" actionability check can never pass, so
        # every click times out. That is the whole failure, from one close.
        #
        # A genuinely stuck helper stays blank across the gap; a navigating
        # window does not.
        first = _blank_ids()
        if not first:
            return
        time.sleep(3)
        second = _blank_ids()
        still_blank = set(first) & set(second)
        transient = set(first) - still_blank
        for tid in transient:
            log(f'  prune: keeping {tid[:12]} -- was blank, now navigating '
                f'(this is the app window at the wizard handoff)')
        blanks = [second[t] for t in still_blank]
        if not blanks:
            return
        ver = json.loads(urllib.request.urlopen(
            f'http://127.0.0.1:{cdp_port}/json/version', timeout=2).read())
        bws = _ws.create_connection(ver['webSocketDebuggerUrl'],
                                    timeout=5,
                                    origin=f'http://127.0.0.1:{cdp_port}')
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
def _comfy_base_url():
    return f'http://127.0.0.1:{_COMFY_PORT or 8188}'


def _configure_comfy_settings():
    """Suppress the first-run Templates panel, server-side.

    ComfyUI auto-opens the Templates browser on a fresh install when no
    workflow is loaded. That modal sits over the canvas, and while it is up
    the page stops presenting frames -- which is why every screenshot times
    out, why Playwright clicks never reach 'stable', and why the UI visibly
    freezes the moment Templates appears. The driver used to react to it
    (hunt for a Close button, fall back to Escape); this stops it happening.

    Same settings, same endpoint as reporting/screenshot.py's
    _configure_server_settings(), which has carried the comment "Prevent
    Templates panel from showing on first run" all along -- the desktop path
    simply never applied them.

    Best effort: the server may not be up yet on the first call, and these
    are re-applied after the Manager reboot resets user state.
    """
    settings = {
        'Comfy.TutorialCompleted': True,
        # Vue node overlays default off on a fresh install, which suppresses
        # all preview rendering.
        'Comfy.VueNodes.Enabled': True,
    }
    for key, value in settings.items():
        try:
            req = urllib.request.Request(
                f'{_comfy_base_url()}/settings/{key}',
                data=json.dumps(value).encode(), method='POST',
                headers={'Content-Type': 'application/json'},
            )
            urllib.request.urlopen(req, timeout=10)
            log(f'  settings: {key}={value}')
        except Exception as e:
            log(f'  settings: {key} failed ({e})')


def _walk_first_run_wizard(cdp_port, timeout=1200):
    import websocket  # from websocket-client, added to venv by _desktop_runner
    def _ws_id():
        _ws_id.n += 1
        return _ws_id.n
    _ws_id.n = 0

    def _attach_panel():
        """Find the panel.html page + open a WS to it. Returns (ws, url)."""
        pages = json.loads(urllib.request.urlopen(
            f'http://127.0.0.1:{cdp_port}/json/list', timeout=3).read())
        cand = None
        for p in pages:
            if p.get('type') == 'page' and 'panel.html' in p.get('url', ''):
                cand = p
                break
        if not cand:
            return None, None
        return (websocket.create_connection(cand['webSocketDebuggerUrl'],
                                            timeout=10,
                                            origin=f'http://127.0.0.1:{cdp_port}'),
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
                f'http://127.0.0.1:{cdp_port}/json/list', timeout=2).read())
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
                f'http://127.0.0.1:{cdp_port}/json/list', timeout=2).read())
            comfy = next((p for p in pages
                          if p.get('type') == 'page'
                          and '127.0.0.1' in p.get('url', '')), None)
            if not comfy: return
            pws = websocket.create_connection(comfy['webSocketDebuggerUrl'],
                                              timeout=5,
                                              origin=f'http://127.0.0.1:{cdp_port}')
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
            # Suppress the Templates modal at the source, before the frontend
            # can open it. Dismissing it after the fact (the ESC fallback
            # below) leaves a window where it is up and the page has stopped
            # presenting frames.
            _configure_comfy_settings()
            if ws:
                try: ws.close()
                except Exception: pass
            # Give ComfyUI a beat to finish loading, then prune blank-URL
            # targets so playwright's connect_over_cdp doesn't hang on them.
            #
            # Do NOT send Escape here. On Windows it does not merely close the
            # Templates modal -- it makes the whole ComfyUI window vanish
            # (measured in CADabra-1348: frame at t+136s shows the UI fully
            # rendered, ESC lands at t+137s, and from t+138s the screen is
            # byte-identical desktop with no Comfy taskbar entry). The page
            # itself survives -- CDP still attaches and the DOM is fully
            # queryable -- which is the signature of electron's
            # close-intercepted-as-win.hide() pattern rather than a crash.
            #
            # The consequences are the entire bug we spent days on: no window
            # means no compositor surface, so Page.captureScreenshot hangs and
            # startScreencast emits nothing; no frames means no
            # requestAnimationFrame, so playwright's "stable" actionability
            # check can never pass and EVERY click times out -- which is why
            # all 21 workflows reported `timeout` with no execution_success.
            #
            # It is also redundant: _configure_comfy_settings() above sets
            # Comfy.TutorialCompleted before the frontend can open the modal.
            # macOS does not bind Escape this way, hence Windows-only.
            time.sleep(3)
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


def install_dialog_handler(page):
    """Attach a per-page dialog handler so Playwright doesn't crash on
    dialog races during page.reload() / Manager restart teardown.

    Without a user-registered handler, Playwright's Node.js driver
    auto-dismisses dialogs internally. That path awaits
    `Page.handleJavaScriptDialog` which races the dialog closing on
    its own -- landing after the dialog is gone throws
    `ProtocolError: No dialog is showing`, which as an unhandled
    promise rejection hard-exits the Node driver and kills the whole
    Python `page.reload()` call. Attaching ANY handler routes dialogs
    through us instead, and we swallow the race explicitly.

    beforeunload: accept (we WANT to leave during page.reload).
    Everything else: dismiss (no side effect on cancel-style dialogs).
    """
    def _handle(dialog):
        try:
            if getattr(dialog, 'type', '') == 'beforeunload':
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            pass  # dialog already closed (the race we're guarding against)
    try:
        page.on('dialog', _handle)
    except Exception as e:
        log(f'  dialog handler attach failed: {e}')


def install_viewport_size(page, width=1920, height=1080):
    """Pin browser viewport (CSS pixels) so page.screenshot(), per-workflow
    videos, and the live viewer all render at a predictable size.

    Overrides via Emulation.setDeviceMetricsOverride (Playwright's
    set_viewport_size). Independent of the Electron OS window size --
    Comfy Desktop's own BrowserWindow default is ~1280x863, but that's
    just the OS frame; what we capture is the WebContents viewport,
    which this pins to 1080p.
    """
    # Clamp to what the window can actually composite. Page.captureScreenshot
    # reads the composited *window surface*, so a viewport larger than the
    # window is never satisfiable and every capture stalls until timeout --
    # the docstring above is true for layout but not for capture.
    #
    # Read innerWidth/Height BEFORE overriding: afterwards they report the
    # emulated value, which is why a broken run's diagnostics show
    # inner=1920x1080 next to outer=1280x863.
    try:
        real_w, real_h = page.evaluate(
            '() => [window.innerWidth, window.innerHeight]')
        if real_w and real_h and (real_w < width or real_h < height):
            log(f'  viewport: window client is {real_w}x{real_h}, smaller than '
                f'requested {width}x{height}; clamping so surface capture can '
                f'succeed. We deliberately do NOT resize the window to match: '
                f'doing so meant hunting for its HWND, which repeatedly found '
                f'the wrong one. Smaller artifacts beat a black screen.')
            width, height = real_w, real_h
    except Exception as e:
        log(f'  viewport: could not read window size ({e}); using '
            f'{width}x{height} unclamped')
    try:
        page.set_viewport_size({"width": width, "height": height})
        log(f'  viewport pinned to {width}x{height}')
    except Exception as e:
        log(f'  viewport {width}x{height} set failed: {e}')


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


# Install-console overlay: fixed-position dark monospace panel that
# renders inside the ComfyUI WebContents (so it shows up on the
# per-workflow video). Used by the --dev install path to demonstrate
# the exact git/pip/install.py commands a dev-branch user would type,
# streaming subprocess output into it live.
_INSTALL_CONSOLE_JS = r'''
(() => {
  const id = '__cm_install_console';
  if (document.getElementById(id)) return;
  const el = document.createElement('div');
  el.id = id;
  el.style.cssText = [
    'position:fixed', 'top:0', 'right:0', 'bottom:0',
    'width:50vw', 'height:100vh',
    'background:#0a0a0a', 'color:#e0e0e0',
    'border:0', 'border-left:2px solid #3a3a3a',
    'padding:16px 20px', 'box-sizing:border-box',
    'font:13px/1.45 ui-monospace,Consolas,Menlo,monospace',
    'overflow-y:auto', 'z-index:2147483647',
    'box-shadow:-12px 0 40px rgba(0,0,0,.6)',
    'white-space:pre-wrap', 'word-break:break-word',
  ].join(';');
  const hdr = document.createElement('div');
  hdr.textContent = 'installing (dev branch) -- actual commands running in ComfyUI Desktop:';
  hdr.style.cssText = 'color:#7cf;margin-bottom:10px;letter-spacing:.03em;font-size:12px';
  const body = document.createElement('div');
  body.id = id + '_body';
  el.appendChild(hdr);
  el.appendChild(body);
  document.body.appendChild(el);
})();
'''

# Line-based appender: split on newlines, color each line by prefix so
# our commands ($ ...) and meta-notes (# ...) pop against subprocess
# output. subprocess.Popen with bufsize=1 gives complete lines, so
# splitting is safe (no mid-line boundary issues).
_INSTALL_CONSOLE_APPEND_JS = r'''
(text) => {
  const b = document.getElementById('__cm_install_console_body');
  if (!b) return;
  const parts = text.split('\n');
  for (let i = 0; i < parts.length; i++) {
    const line = parts[i];
    // Drop the trailing empty string from a text ending in '\n' --
    // otherwise we'd emit a blank <span> per append call.
    if (line === '' && i === parts.length - 1) break;
    let color = '#e0e0e0';
    if (line.startsWith('$ ')) color = '#8be07c';           // our commands: green
    else if (line.startsWith('#')) color = '#c2a3ff';       // our meta-notes: purple
    else if (line.trim().startsWith('-> exit')) color = '#ffb87a';  // exit line: salmon
    const span = document.createElement('span');
    span.style.color = color;
    span.textContent = line + '\n';
    b.appendChild(span);
  }
  const p = b.parentElement;
  p.scrollTop = p.scrollHeight;
}
'''

_INSTALL_CONSOLE_HIDE_JS = "() => { const el = document.getElementById('__cm_install_console'); if (el) el.remove(); }"


def _show_install_console(page):
    try:
        page.evaluate(_INSTALL_CONSOLE_JS)
    except Exception as e:
        log(f'  console show failed: {e}')


def _console_append(page, text):
    try:
        page.evaluate(_INSTALL_CONSOLE_APPEND_JS, text)
    except Exception:
        pass  # single append failing is OK; the subprocess keeps running


def _hide_install_console(page):
    try:
        page.evaluate(_INSTALL_CONSOLE_HIDE_JS)
    except Exception:
        pass


def _find_active_comfy_install():
    """Read Comfy Desktop's installations.json and return
    (install_path, comfy_root, custom_nodes, venv_python) for the active
    standalone install. Raises RuntimeError if not found.

    installations.json path:
      - macOS:   ~/Library/Application Support/Comfy Desktop/installations.json
      - Windows: %APPDATA%\\Comfy Desktop\\installations.json
    """
    if sys.platform == 'win32':
        appdata = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming'))
        installations_json = appdata / 'Comfy Desktop' / 'installations.json'
    else:
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
    _venv_bin = 'Scripts' if sys.platform == 'win32' else 'bin'
    _venv_exe = 'python.exe' if sys.platform == 'win32' else 'python'
    venv_python = comfy_root / '.venv' / _venv_bin / _venv_exe
    if not venv_python.exists():
        venv_python = install_path / 'standalone-env' / _venv_bin / _venv_exe
    return install_path, comfy_root, custom_nodes, venv_python


# Cross-run guard so multiple call sites don't spawn duplicate tail
# threads (e.g. wizard-completion + ext-install-block both call it).
_comfyui_log_tail_started = [False]


def _start_comfyui_log_tail():
    """Spawn a background thread that tails ComfyUI's stdout log
    (`<install>/logs/comfyui.log`) and forwards every new line via
    log() with a `[comfyui]` prefix. Idempotent -- safe to call from
    multiple sites.

    Why: workflow-execution errors (invalid prompt, missing input files,
    node exceptions) are printed by ComfyUI's Python server to
    stdout, which Comfy Desktop redirects to `comfyui.log`. The driver's
    CDP-level view can only see `execution_success`/`execution_error`
    from the WS, not the actual traceback. Tailing the file forwards
    ComfyUI's output into session.log (which the live-viewer's
    bottom-right panel already displays), giving us live server-side
    errors without a post-run log dig.

    Uses `tail -F` (capital F) so log rotation / per-run file wipes
    are followed transparently."""
    if _comfyui_log_tail_started[0]:
        return
    _comfyui_log_tail_started[0] = True

    def _tail_worker():
        # Wait up to 60s for installations.json + comfyui.log to appear --
        # they're written by Comfy Desktop during setup wizard.
        log_file = None
        for _ in range(60):
            try:
                install_path, _, _, _ = _find_active_comfy_install()
                candidate = install_path / 'logs' / 'comfyui.log'
                if candidate.exists():
                    log_file = candidate
                    break
            except Exception:
                pass
            time.sleep(1)
        if log_file is None:
            log('[comfyui-log-tail] gave up: no logs/comfyui.log within 60s')
            return
        log(f'[comfyui-log-tail] tailing {log_file}')
        # Python-native tail -F equivalent: reads new lines as they're
        # appended, and re-seeks to 0 on truncation/rotation. Works on
        # macOS and Windows (Windows doesn't ship `tail` on PATH).
        try:
            # Retry the open: exists() was checked above, but Comfy Desktop
            # deletes and recreates comfyui.log when the backend restarts, so
            # a single attempt races that window and used to kill this thread
            # with a bare "[Errno 2] No such file or directory".
            fh = None
            for _attempt in range(20):
                try:
                    fh = open(str(log_file), 'r', encoding='utf-8', errors='replace')
                    break
                except FileNotFoundError:
                    time.sleep(0.5)
            if fh is None:
                log(f'[comfyui-log-tail] gave up: {log_file} never became readable')
                return
            fh.seek(0)   # replay whole log-so-far (matches old `-n +1` behavior)
            while True:
                line = fh.readline()
                if not line:
                    # No new data: brief sleep, then check for truncation.
                    time.sleep(0.5)
                    try:
                        if os.stat(str(log_file)).st_size < fh.tell():
                            fh.seek(0)
                    except FileNotFoundError:
                        # File was rotated away; try to reopen a few times.
                        for _ in range(10):
                            time.sleep(0.5)
                            try:
                                fh.close()
                                fh = open(str(log_file), 'r', encoding='utf-8', errors='replace')
                                break
                            except FileNotFoundError:
                                continue
                        else:
                            log(f'[comfyui-log-tail] log gone: {log_file}')
                            return
                    continue
                # Emit through the same [comfyui] prefix as before.
                stripped = line.rstrip('\n')
                if stripped:
                    log(f'[comfyui] {stripped}')
        except Exception as e:
            log(f'[comfyui-log-tail] tail failed: {e}')

    threading.Thread(target=_tail_worker, daemon=True).start()


_QUEUE_PROMPT_JS = r"""
(async () => {
  const app = window.app;
  if (!app) return JSON.stringify({kind: 'noapp'});
  // POST /prompt ourselves rather than calling app.queuePrompt().
  //
  // Both submit the same graph, but queuePrompt swallows a rejection into a UI
  // toast and resolves normally, so a graph ComfyUI REFUSES looks identical to
  // one it accepted. ComfyUI emits no execution_success/error for a prompt it
  // never ran, so the driver then waits the full timeout and reports
  // WORKFLOW TIMEOUT -- hiding the real reason. Measured in CADabra-1415:
  // mesh_face_seg was rejected with "Value not in list: file_path:
  // '3d/cube.stl'" at t+696s and still cost 180s to report as a timeout.
  //
  // POSTing directly exposes the 400 and its node_errors, turning a blind
  // 180s timeout into an instant, accurate failure. client_id is carried so
  // execution events still route to this page's websocket.
  if (typeof app.graphToPrompt === 'function') {
    try {
      const p = await app.graphToPrompt();
      const cid = (app.api && app.api.clientId) || window.clientId;
      const r = await fetch('/prompt', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt: p.output, client_id: cid,
                              extra_data: {extra_pnginfo: {workflow: p.workflow}}}),
      });
      const body = await r.text();
      if (r.ok) {
        let pid = null;
        try { pid = JSON.parse(body).prompt_id; } catch (e) {}
        return JSON.stringify({kind: 'queued', prompt_id: pid});
      }
      let detail = body.slice(0, 800);
      try {
        const j = JSON.parse(body);
        const parts = [];
        if (j.error) parts.push(j.error.message || j.error.type || String(j.error));
        for (const [nid, ne] of Object.entries(j.node_errors || {})) {
          for (const err of (ne.errors || [])) {
            parts.push(`node ${nid} ${ne.class_type || ''}: ` +
                       `${err.message}${err.details ? ' (' + err.details + ')' : ''}`);
          }
        }
        if (parts.length) detail = parts.join('; ');
      } catch (e) {}
      return JSON.stringify({kind: 'rejected', status: r.status, detail: detail});
    } catch (e) {
      return JSON.stringify({kind: 'threw', detail: String(e && e.message || e)});
    }
  }
  if (typeof app.queuePrompt === 'function') {
    try { await app.queuePrompt(0, 1); return JSON.stringify({kind: 'queued', via: 'queuePrompt'}); }
    catch (e) { return JSON.stringify({kind: 'threw', detail: String(e && e.message || e)}); }
  }
  return JSON.stringify({kind: 'nomethod'});
})()
"""


def _queued_prompt_ids():
    """Every prompt_id ComfyUI currently knows about: queued, running or done.

    Used to prove that clicking Run actually submitted something. The button
    click reporting success is NOT evidence: measured in CADabra-1406, a
    force-click on Run logged `clicked Run` while /queue and /history both
    stayed empty, and all 21 workflows then sat until WORKFLOW TIMEOUT.
    """
    ids = set()
    base = _comfy_base_url()
    try:
        with urllib.request.urlopen(base + '/queue', timeout=5) as r:
            d = json.loads(r.read() or b'{}')
        for key in ('queue_running', 'queue_pending'):
            for item in (d.get(key) or []):
                if isinstance(item, (list, tuple)) and len(item) > 1:
                    ids.add(str(item[1]))
    except Exception:
        pass
    try:
        with urllib.request.urlopen(base + '/history', timeout=5) as r:
            d = json.loads(r.read() or b'{}')
        if isinstance(d, dict):
            ids.update(str(k) for k in d)
    except Exception:
        pass
    return ids


def _click_run_and_confirm(page, wait_s=3):
    """Press the Run button like a user, and verify a prompt was really queued.

    Preferred over POSTing /prompt because it exercises what a user actually
    does: the button, the frontend's graph->prompt conversion and its queue
    call. The caller falls back to _queue_prompt() when this returns False, so
    a UI regression degrades to a working run rather than 21 dead workflows.

    Returns True only when a NEW prompt_id appeared server-side.
    """
    before = _queued_prompt_ids()
    try:
        # Order matters. ComfyUI's Run control is a SPLIT button:
        #
        #   [data-testid="queue-button"]            79x32  text "Run"   <- runs
        #   [data-testid="queue-mode-menu-trigger"] 24x32  aria "Run"   <- menu
        #
        # The chevron is the one carrying aria-label="Run"; the real button has
        # no aria-label. Leading with button[aria-label="Run"] therefore
        # resolved the chevron, and every "clicked Run" just opened the
        # queue-mode dropdown -- measured in CADabra-1623, run-diag showed
        # elementFromPoint at its centre was the chevron itself, so the click
        # landed cleanly on the wrong control. 21/21 workflows queued nothing
        # and were rescued by the POST fallback.
        run_btn = None
        for sel in ('[data-testid="queue-button"]',
                    'button:has-text("Run"):visible',
                    'button[aria-label="Run"]:visible'):
            loc = page.locator(sel).first
            if loc.count():
                run_btn = loc
                log(f'  ext: Run button via [{sel}]')
                break
        if run_btn is None:
            log('  ext: Run button not found; falling back to POST /prompt')
            return False
        click_with_cursor(page, run_btn)
        log('  ext: clicked Run')
    except Exception as e:
        log(f'  ext: Run click failed ({e}); falling back to POST /prompt')
        return False
    deadline = time.time() + wait_s
    while time.time() < deadline:
        new = _queued_prompt_ids() - before
        if new:
            log(f'  ext: Run queued prompt {sorted(new)[0][:12]}')
            return True
        time.sleep(0.5)
    # Coordinate input does not reach this button on this host, so fall through
    # to a DOM-level click. THIS is what actually queues here.
    #
    # Measured in CADabra-1709: a listener attached to the button before the
    # coordinate click recorded pointerdown=0 mousedown=0 click=0, while
    # elementFromPoint at its centre returned the button and rafFired=1 proved
    # the window was live -- i.e. the button was right there and the synthesised
    # input never arrived. el.click() then queued immediately
    # (`DOM click queued prompt a1b2ec33-030`), which also proves the frontend
    # does NOT gate on isTrusted. So the button's own handler runs; only input
    # delivery to the top strip of this window is broken.
    #
    # The coordinate click above is kept deliberately: it is the real
    # interaction, it costs a few seconds, and if input delivery is ever fixed
    # we will see `Run queued prompt` instead of this path and know.
    try:
        run_btn.evaluate('el => el.click()')
        deadline = time.time() + 4
        while time.time() < deadline:
            new = _queued_prompt_ids() - before
            if new:
                log(f'  ext: Run queued prompt {sorted(new)[0][:12]} '
                    f'(via the button handler; coordinate input did not land)')
                return True
            time.sleep(0.5)
        log('  ext: DOM click also queued nothing')
    except Exception as e:
        log(f'  ext: DOM click failed: {e}')

    log('  ext: Run did not queue anything; falling back to POST /prompt '
        'for the rejection reason')
    return False


def _queue_prompt(page):
    """Queue the loaded graph programmatically. Returns True if submitted.

    We already LOAD each workflow through window.app.loadGraphData rather than
    clicking a template card, so queuing it through window.app too is the
    consistent move -- and it sidesteps the Run button entirely.

    That matters on this Windows host: the app window never presents frames, so
    playwright's actionability "stable" check cannot pass and clicking Run
    times out. Measured in CADabra-1406: force=True made the click itself
    "succeed" and the driver logged `clicked Run`, but /queue and /history
    stayed EMPTY -- the click reached the element without the frontend ever
    queuing anything, so every workflow still hit WORKFLOW TIMEOUT. A click
    that lands but does nothing is worse than one that fails loudly, so do not
    trust `clicked Run` as evidence a prompt was submitted.
    """
    try:
        res = json.loads(page.evaluate(_QUEUE_PROMPT_JS))
    except Exception as e:
        log(f'  ext: queue via window.app failed: {e}')
        return False
    kind = res.get('kind')
    if kind == 'queued':
        pid = res.get('prompt_id')
        log(f'  ext: queued prompt{" " + pid[:12] if pid else ""}')
        return True
    if kind == 'rejected':
        # ComfyUI refused the graph outright, so no execution event will ever
        # arrive. Surface it now instead of waiting out the full timeout.
        log(f'  ext: PROMPT REJECTED by ComfyUI ({res.get("status")}): '
            f'{res.get("detail")}')
        return 'rejected'
    log(f'  ext: programmatic queue unavailable ({kind}: '
        f'{res.get("detail", "")}); falling back to Run button')
    return False


def click_with_cursor(page, loc, timeout=3000):
    """Click, degrading through progressively less fussy strategies.

    Playwright's normal click waits for "actionability", and part of that is
    the STABLE check: it compares the element's bounding box across two
    consecutive animation frames. On this Windows host the app's window is
    not presenting -- captureScreenshot hangs, startScreencast emits nothing --
    so requestAnimationFrame callbacks do not fire, stability can never be
    proven, and the click times out after `timeout` no matter how ordinary the
    element is. That is what made all 21 workflows report `timeout` with no
    execution_success: the graph loaded fine and Run was simply never clicked.

    The element itself is perfectly fine -- the DOM is fully queryable
    throughout -- so falling back to force=True (skips actionability) and then
    to a raw DOM .click() gets the run moving. Each fallback is logged so a
    run that needed them is never mistaken for a clean one.
    """
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
    try:
        loc.click(timeout=timeout)
        return
    except Exception as e:
        first = str(e).split('\n')[0]
    # force=True skips the actionability wait (visible/stable/enabled/receives
    # events) but still dispatches a real trusted mouse event at the element's
    # position, so listeners that care about isTrusted still see a genuine one.
    try:
        loc.click(timeout=timeout, force=True)
        log(f'  click: normal click failed ({first}); succeeded with force=True')
        return
    except Exception as e2:
        second = str(e2).split('\n')[0]
    # Last resort: synthetic DOM click. Not a trusted event, so a handler
    # gating on isTrusted would ignore it -- hence last, not first.
    loc.evaluate('el => el.click()')
    log(f'  click: force click also failed ({second}); used DOM el.click()')

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

def _post_json_empty(path):
    """POST an empty JSON body (`{}`) with application/json Content-Type
    to a ComfyUI endpoint. Manager's /api/v2/manager/reboot (and other
    state-mutation endpoints) reject 'simple form' Content-Types
    (text/plain, form-urlencoded, multipart, absent) with HTTP 400 via
    a CSRF-like guard. JSON Content-Type bypasses that guard -- matches
    what Manager's own `api.fetchApi()` does client-side."""
    req = urllib.request.Request(
        f'{_comfy_base_url()}{path}', data=b'{}', method='POST',
        headers={'Content-Type': 'application/json'},
    )
    return urllib.request.urlopen(req, timeout=30)


def _reboot_via_manager_and_wait():
    """Restart ComfyUI's Python server (not the Electron app) via
    Manager's /api/v2/manager/reboot. Then wait for /system_stats
    down (server dying) -> up (fresh server ready -> custom_nodes
    re-scanned). Returns True on success.

    Keeps the Electron main process, CDP endpoint, and any attached
    playwright `page`/`browser` handles alive across the restart --
    only the Python child is bounced. This is what makes it viable
    over SSH (no `open <app>` bridging needed) AND between workflows
    (no need to reconnect_over_cdp)."""
    log('  ext: rebooting ComfyUI via Manager API')
    posted = False
    for reboot_path in ('/api/v2/manager/reboot', '/v2/manager/reboot'):
        try:
            with _post_json_empty(reboot_path) as resp:
                log(f'  ext: {reboot_path} -> HTTP {resp.status}')
                posted = True
                break
        except urllib.error.HTTPError as e:
            log(f'  ext: {reboot_path} HTTP {e.code} (server may be restarting)')
            if e.code in (404, 405):
                continue
            # 2xx-adjacent codes (500/502/503) can happen when the
            # server responds and dies mid-flight. Treat as posted.
            posted = True
            break
        except Exception as e:
            # ConnectionResetError / RemoteDisconnected -- server
            # died before finishing the response. Also counts as
            # successfully triggered a restart.
            log(f'  ext: {reboot_path} conn dropped: {e.__class__.__name__} (server restarting)')
            posted = True
            break
    if not posted:
        log('  ext: no Manager reboot endpoint accepted; /system_stats poll may not detect a real restart')
    # Wait for /system_stats to go DOWN then UP. Going-down proves
    # the server actually restarted (vs. the pre-fix 400 case where
    # /system_stats stayed up because Python never died).
    log('  ext: waiting for /system_stats to drop (proving restart)')
    went_down = False
    for i in range(30):   # up to 30s for it to die
        try:
            urllib.request.urlopen(f'{_comfy_base_url()}/system_stats', timeout=1)
            time.sleep(1)
        except Exception:
            log(f'  ext: /system_stats down after {i+1}s (server restarting)')
            went_down = True
            break
    if not went_down:
        log('  ext: WARNING: /system_stats never dropped -- reboot may not have fired')
    log('  ext: waiting for /system_stats back up')
    for i in range(180):
        try:
            urllib.request.urlopen(f'{_comfy_base_url()}/system_stats', timeout=2)
            log(f'  ext: /system_stats back up after {(i+1)*2}s')
            # Re-apply: the Manager reboot restarts the server, and the
            # frontend re-evaluates whether to show Templates on reload.
            _configure_comfy_settings()
            return True
        except Exception:
            time.sleep(2)
    return False


def _install_via_visible_shell(page, node_repo, node_branch,
                                custom_nodes, venv_python):
    """Skip the Manager UI entirely and run git clone / pip / install.py
    directly, streaming each command's output into the install-console
    overlay so the recorded video demonstrates the exact commands a
    dev-branch user would run.

    Uses ComfyUI Desktop's bundled `.venv/bin/python` for pip + install.py
    so the demo matches what the viewer's own Desktop app would use.
    Batches subprocess stdout every ~150ms -- one page.evaluate() per
    line would be too chatty over CDP.
    """
    node_name = node_repo.split('/')[-1]
    node_dir = custom_nodes / node_name
    _show_install_console(page)
    log(f'  install-shell: rendering console overlay in ComfyUI window')

    def _stream(cmd_argv, cwd=None):
        pretty = ' '.join(cmd_argv)
        log(f'  install-shell: $ {pretty}')
        _console_append(page, f'$ {pretty}\n')
        try:
            proc = subprocess.Popen(
                cmd_argv,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except FileNotFoundError as e:
            # log() as well as the overlay: in the sandbox guest a failed git
            # clone left NOTHING in the session log -- the only evidence went
            # to an in-app console overlay nobody records, and the install
            # "succeeded" into an empty custom_nodes. Every failure and every
            # output line must reach the durable log.
            log(f'  install-shell:   -> command not found: {e}')
            _console_append(page, f'  -> command not found: {e}\n\n')
            return 127
        except OSError as e:
            # e.g. WinError 1260: blocked by policy (Application Control).
            log(f'  install-shell:   -> could not start: {e}')
            _console_append(page, f'  -> could not start: {e}\n\n')
            return 126
        buf = []
        last_flush = time.time()
        for line in proc.stdout:
            log(f'  install-shell:   | {line.rstrip()}')
            buf.append(line)
            if time.time() - last_flush > 0.15:
                _console_append(page, ''.join(buf))
                buf.clear()
                last_flush = time.time()
        if buf:
            _console_append(page, ''.join(buf))
        rc = proc.wait()
        log(f'  install-shell:   -> exit {rc}')
        _console_append(page, f'  -> exit {rc}\n\n')
        return rc

    # If the dir already exists (Manager left a stub, prior run, etc),
    # git clone would refuse. Wipe first so the clone is clean.
    if node_dir.exists():
        _console_append(page, f'# clearing prior {node_dir.name}/ before clone\n')
        try:
            import shutil as _shutil
            _shutil.rmtree(node_dir)
        except Exception as e:
            _console_append(page, f'  -> wipe failed: {e}\n\n')

    # Prefer a host-provided local copy over cloning: inside the Windows
    # Sandbox guest, git's DNS fails intermittently no matter how the resolver
    # is configured (hosts-file pin and adapter-level public DNS both measured
    # insufficient on first attempts), so the sandbox runner clones on the
    # HOST and maps the tree in via COMFY_TEST_NODE_LOCAL_COPY.
    _local_copy = os.environ.get('COMFY_TEST_NODE_LOCAL_COPY', '')
    if _local_copy and Path(_local_copy).is_dir():
        log(f'  install-shell: using host-provided clone at {_local_copy}')
        _console_append(page, f'# copying host-provided clone {_local_copy}\n')
        import shutil as _shutil
        _shutil.copytree(_local_copy, node_dir,
                         ignore=_shutil.ignore_patterns('__pycache__', '*.pyc'))
        _rc_clone = 0
    else:
        _rc_clone = -1
    # Otherwise clone with retries: transient DNS/network failures inside
    # sandboxed guests are real, and git does not retry on its own. A failed
    # attempt leaves a partial dir that would make the next attempt refuse,
    # so wipe between tries.
    for _attempt in range(1, 4):
        if _rc_clone == 0:
            break
        rc = _stream(['git', 'clone', '--depth', '1', '-b', node_branch,
                      f'https://github.com/{node_repo}.git', str(node_dir)])
        if rc == 0:
            break
        log(f'  install-shell: clone attempt {_attempt}/3 failed (rc={rc})'
            + ('; retrying in 10s' if _attempt < 3 else '; giving up'))
        if node_dir.exists():
            try:
                import shutil as _shutil
                _shutil.rmtree(node_dir)
            except Exception as _e:
                log(f'  install-shell: partial-clone wipe failed: {_e}')
        if _attempt < 3:
            time.sleep(10)

    reqs = node_dir / 'requirements.txt'
    if reqs.is_file() and venv_python.exists():
        _stream([str(venv_python), '-m', 'pip', 'install', '--no-input',
                 '-r', str(reqs)])
    elif not reqs.is_file():
        _console_append(page, '# no requirements.txt -- skipping pip\n\n')

    install_py = node_dir / 'install.py'
    if install_py.is_file() and venv_python.exists():
        _stream([str(venv_python), 'install.py'], cwd=node_dir)
    elif not install_py.is_file():
        _console_append(page, '# no install.py -- skipping\n\n')

    _console_append(page, '# rebooting ComfyUI backend via Manager...\n')
    log('  install-shell: rebooting ComfyUI via Manager API')
    _reboot_via_manager_and_wait()
    # Manager reboot restarts Python but the frontend renderer's
    # extension-list cache is still stale (Templates panel is populated
    # from that cache -- without a reload the new node's template
    # section never appears). Same pattern as the old Manager-UI-install
    # post-Apply-Changes reload at ~L2075.
    _console_append(page, '# reloading renderer to refresh extension list\n')
    log('  install-shell: reloading renderer')
    page, _reloaded = _reload_renderer_hard(page, None)
    try:
        install_cursor(page)   # cursor injection is DOM-based; wiped by reload
    except Exception as e:
        log(f'  install-shell: post-reload cursor install failed (non-fatal): {e}')
    _console_append(page, '# done. running tests now.\n')
    time.sleep(4)   # let the viewer read the "done" line before we hide
    _hide_install_console(page)


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
            urllib.request.urlopen(f'http://127.0.0.1:{_CDP_PORT}/json/version', timeout=1)
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


def _browser_ui_executable(p_arg):
    """Path to a chromium-family browser to host the ComfyUI UI.

    Prefer playwright's own chromium (version-matched to the playwright we
    drive it with); fall back to installed Edge, which is chromium too and is
    present on every Windows box.

    Takes the already-open playwright instance -- sync_playwright() is not
    re-entrant and we are called from inside the driver's `with` block.
    """
    try:
        exe = p_arg.chromium.executable_path
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    for c in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
              r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'):
        if Path(c).exists():
            return c
    return None


def _browser_ui_profile_dir(debug_dir):
    """Where the browser-UI chromium keeps --user-data-dir.

    Normally inside debug_dir so the profile is part of the run's
    inspectable artifacts. In the Windows Sandbox guest, debug_dir sits on
    a VMSMB mapped folder and chromium cannot lock a profile there: every
    launch throws a modal 'Profile error occurred' dialog and
    DevToolsActivePort never appears (measured GeometryPack-1902:
    connect_over_cdp timed out after 180s). Guest-local %TEMP% instead --
    the guest is disposable, so losing the profile artifact costs nothing.
    """
    if os.environ.get('COMFY_TEST_IN_SANDBOX') == '1':
        import tempfile
        return Path(tempfile.gettempdir()) / 'comfy-test-browser-ui-profile'
    return Path(debug_dir) / 'browser-ui-profile'


def _launch_browser_ui(p_arg, url, debug_dir):
    """Serve the ComfyUI UI from a real browser window in session 1.

    Why: Comfy Desktop's Electron window never presents frames on this host --
    it destroys the installer window when setup finishes and the window that
    remains is hidden and paints black when shown. No presented frames means
    Page.captureScreenshot hangs, startScreencast emits nothing, and
    requestAnimationFrame never fires, so playwright's "stable" actionability
    check can never pass and every click times out.

    ComfyUI is just an HTTP app on 127.0.0.1:8188, so a plain browser window
    renders it fine -- measured at ~14 FPS in Edge on this same host while the
    Electron window sat black. Driving the UI there gets working screenshots,
    working video and working clicks, and what the operator watches on screen
    IS the test rather than a bystander tab.

    The browser must be launched INTO session 1 (schtasks /it) for the same
    reason the app is: a browser started from session 0 has no desktop and
    would reproduce the exact problem we are escaping. We then attach over CDP
    from session 0, which is session-agnostic.

    Returns (browser, page) or (None, None).
    """
    # (profile placement: see _browser_ui_profile_dir)
    exe = _browser_ui_executable(p_arg)
    if not exe:
        log('  browser-ui: no chromium/edge found; staying on the Electron page')
        return None, None
    profile = _browser_ui_profile_dir(debug_dir)
    port_file = profile / 'DevToolsActivePort'
    try:
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        profile.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f'  browser-ui: could not prepare profile dir: {e}')
        return None, None

    cmd_path = Path(debug_dir) / 'launch-browser-ui.cmd'
    flags = (
        f'--remote-debugging-port=0 --remote-allow-origins=* '
        f'--user-data-dir="{profile}" --no-first-run --no-default-browser-check '
        f'--disable-features=CalculateNativeWinOcclusion '
        f'--disable-backgrounding-occluded-windows --disable-renderer-backgrounding '
        f'--window-position=0,0 --window-size=1900,1030 '
        f'--new-window "{url}"'
    )
    cmd_path.write_text(f'@echo off\r\n"{exe}" {flags}\r\n', encoding='ascii')
    task = 'comfy-test-browser-ui'
    try:
        subprocess.run(['schtasks', '/end', '/tn', task],
                       capture_output=True, timeout=15)
        subprocess.run(['schtasks', '/delete', '/tn', task, '/f'],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    user = os.environ.get('COMFY_TEST_SESSION_USER') or os.environ.get('USERNAME') or ''
    if not user or user.upper() == 'SYSTEM' or user.endswith('$'):
        # Same systemprofile trap the rest of the runner guards against: the
        # harness runs as SYSTEM, and a task registered for SYSTEM would land
        # back in session 0.
        for cand in Path('C:/Users').glob('*'):
            if (cand / 'AppData' / 'Roaming').is_dir() and cand.name.lower() not in (
                    'default', 'public', 'default user', 'all users'):
                user = cand.name
                break
    log(f'  browser-ui: launching {Path(exe).name} in session 1 as {user!r} -> {url}')
    r = subprocess.run(['schtasks', '/create', '/tn', task, '/f',
                        '/sc', 'once', '/st', '23:59', '/ru', user, '/it',
                        '/tr', str(cmd_path)], capture_output=True, text=True)
    if r.returncode != 0:
        log(f'  browser-ui: schtasks create failed: {(r.stderr or r.stdout).strip()}')
        return None, None
    r = subprocess.run(['schtasks', '/run', '/tn', task],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f'  browser-ui: schtasks run failed: {(r.stderr or r.stdout).strip()}')
        return None, None

    port = None
    for _ in range(120):
        try:
            if port_file.exists():
                port = int(port_file.read_text().splitlines()[0].strip())
                if port:
                    break
        except Exception:
            pass
        time.sleep(0.5)
    if not port:
        log('  browser-ui: DevToolsActivePort never appeared; staying on Electron page')
        return None, None
    log(f'  browser-ui: CDP port {port}')
    try:
        b = p_arg.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
    except Exception as e:
        log(f'  browser-ui: connect_over_cdp failed: {e}')
        return None, None
    pg = None
    for _ in range(60):
        pg = main_page(b)
        if pg is not None:
            break
        time.sleep(1)
    if pg is None:
        log('  browser-ui: no ComfyUI page in the browser; staying on Electron page')
        try: b.close()
        except Exception: pass
        return None, None
    try:
        pg.wait_for_load_state('load', timeout=60000)
    except Exception:
        pass
    log(f'  browser-ui: attached at {pg.url}')
    # The window is detached from this process, so a crash or an early exit
    # anywhere below would otherwise leave it on the desktop for the next run
    # to photograph. atexit covers every exit path, not just the happy one.
    atexit.register(_stop_browser_ui, str(debug_dir))
    return b, pg


def _stop_browser_ui(debug_dir=None):
    """Tear down the browser-UI window.

    Ending the scheduled task is NOT enough: the wrapper launches the browser
    with `start ""`, which detaches it, so it outlives the task. Measured after
    CADabra-1709 -- the run finished and 8 msedge processes were still up, with
    the window sitting on the desktop where the next run's screencap would
    photograph it.

    Kill by --user-data-dir so we only ever touch the browser WE launched and
    never the user's own Edge/Chrome session.
    """
    for a in (['schtasks', '/end', '/tn', 'comfy-test-browser-ui'],
              ['schtasks', '/delete', '/tn', 'comfy-test-browser-ui', '/f']):
        try:
            subprocess.run(a, capture_output=True, timeout=15)
        except Exception:
            pass
    profile = str(_browser_ui_profile_dir(debug_dir or OUT))
    # $_.ProcessId -ne $PID is load-bearing: this very powershell has the
    # profile path in its own command line, so without the guard the filter
    # matches the cleanup process itself and Stop-Process kills it -- possibly
    # before it has killed the browser.
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and "
        f"$_.CommandLine -like '*{profile}*'" " } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, timeout=30)
        log('  browser-ui: stopped')
    except Exception as e:
        log(f'  browser-ui: teardown failed (non-fatal): {e}')


def _reload_renderer_hard(page_arg, browser_arg):
    """Reload the UI page until it actually loads, goto as a last resort.

    A single 30s page.reload is not enough in the sandbox guest: measured
    GeometryPack-2012, the reload timed out, the driver carried on with a
    stale renderer whose object_info predated the node install, the
    Templates panel had no section for the pack, and every workflow
    serialized class_type-less placeholder nodes -> PROMPT REJECTED x3.
    A half-loaded page after a timed-out reload is worse than no reload.

    Returns (page, ok). page may be a re-fetched main page when
    browser_arg is provided.
    """
    for attempt in range(1, 4):
        try:
            page_arg.reload(wait_until='domcontentloaded', timeout=120_000)
            return page_arg, True
        except Exception as e:
            log(f'  ext: renderer reload attempt {attempt}/3 failed: {e}')
            if browser_arg is not None:
                try:
                    fresh = main_page(browser_arg)
                    if fresh is not None:
                        page_arg = fresh
                except Exception:
                    pass
    try:
        page_arg.goto(_comfy_base_url(), wait_until='domcontentloaded',
                      timeout=120_000)
        log('  ext: renderer goto fallback succeeded')
        return page_arg, True
    except Exception as e:
        log(f'  ext: renderer goto fallback failed: {e}')
        return page_arg, False


def _restart_comfy(p_arg, current_browser):
    """Per-workflow restart. Bounces ONLY ComfyUI's Python server via
    Manager's /api/v2/manager/reboot; keeps the Electron main process,
    the CDP endpoint, and our attached playwright browser/page alive.

    Why not the old kill+relaunch: `pkill -f ComfyUI` + `open <app>`
    fails over SSH -- `open` can't reach the aqua session without
    `sudo launchctl asuser` bridging, so the relaunched app never
    writes DevToolsActivePort and the driver hangs. Manager reboot
    sidesteps that entirely: it kills+respawns the Python child from
    inside the running Electron process, so no `open` and no
    connect_over_cdp reconnection.

    Returns (page, browser) -- SAME browser as passed in (we never
    close it), and the current main page (re-fetched after reload).
    Callers check for None page on failure."""
    ok = _reboot_via_manager_and_wait()
    if not ok:
        log('  loop: WARN: Manager reboot did not confirm /system_stats back up')
    # Browser + CDP session stay valid -- Electron didn't die.
    page = main_page(current_browser)
    if page is None:
        log('  loop: no main page after Python restart, bailing')
        return None, current_browser
    log('  loop: reloading renderer to re-fetch object_info + node manifest')
    page, reloaded = _reload_renderer_hard(page, current_browser)
    if not reloaded:
        log('  loop: renderer never reloaded; workflows will see stale object_info')
    try:
        install_cursor(page)
        _wait_canvas_ready(page, 120)
    except Exception as e:
        log(f'  loop: post-restart canvas wait failed: {e}')
    sleep_capturing(page, 3, fps=5)
    return page, current_browser


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
        # Debug: is the section text literally present anywhere in the
        # Templates panel? Distinguishes "selector missed it" (text
        # present but our nav-selectors don't hit) from "backend never
        # registered the install" (text absent -- frontend still on the
        # pre-install extension list).
        try:
            diag = page_arg.evaluate(f"""
            (name) => {{
                const nav = document.querySelector('nav');
                if (!nav) return {{nav: false}};
                const text = (nav.textContent || '').toLowerCase();
                const hit = text.includes(name.toLowerCase());
                // Also enumerate the section labels we see for a full list.
                const labels = [...nav.querySelectorAll(
                    '[role="button"], button, span, a'
                )].map(el => (el.textContent||'').trim())
                  .filter(t => t && t.length < 80);
                // De-dup + top 40
                const seen = new Set(); const uniq = [];
                for (const l of labels) {{
                    if (!seen.has(l)) {{ seen.add(l); uniq.push(l); }}
                    if (uniq.length >= 40) break;
                }}
                return {{
                    nav: true,
                    text_contains: hit,
                    label_count: labels.length,
                    labels: uniq,
                }};
            }}""", node_package_name)
            log(f'  ext:   diag: nav-present={diag.get("nav")} '
                f'text_contains_{node_package_name}={diag.get("text_contains")} '
                f'label_count={diag.get("label_count")}')
            for lbl in (diag.get('labels') or []):
                log(f'  ext:     nav label: {lbl!r}')
        except Exception as e:
            log(f'  ext:   diag failed: {e}')
        # No pause here. This used to sleep 240s so a human could poke the DOM
        # over CDP, which made sense when the section could never be found and
        # the run was going to fail anyway. Now the section IS findable (the
        # backend serves cloned nodes at /api/workflow_templates -- verified:
        # {"ComfyUI-CADabra": ["analyse_CAD", ...]}), so a miss here is a real
        # regression worth failing fast on, and the caller falls back to
        # loading the graph from disk. At x21 workflows the pause alone was
        # over an hour. Set COMFY_TEST_TEMPLATES_PAUSE_S to re-enable it.
        _pause = int(os.environ.get('COMFY_TEST_TEMPLATES_PAUSE_S', '0') or 0)
        if _pause:
            log(f'  ext: PAUSING {_pause}s so external CDP inspection is possible')
            log('  ext:   pages: curl -s http://127.0.0.1:<cdp port>/json/list')
            try:
                sleep_capturing(page_arg, _pause, fps=5)
            except Exception:
                pass
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


def _workflow_json_path(target_name):
    """On-disk path of a workflow shipped by the node under test, or None.

    Only meaningful after a git-clone install (`--dev`), where
    _install_via_visible_shell put the repo at <custom_nodes>/<NODE_NAME>.
    COMFY_TEST_WORKFLOWS already holds exactly these stems.
    """
    try:
        _, _, custom_nodes, _ = _find_active_comfy_install()
    except Exception:
        return None
    node_dir = Path(custom_nodes) / os.environ.get('NODE_NAME', '')
    for sub in ('workflows', 'example_workflows'):
        p = node_dir / sub / f'{target_name}.json'
        if p.is_file():
            return p
    return None


def _load_graph_from_disk(page_arg, target_name):
    """Load a workflow straight into the canvas. Fallback for the Templates UI.

    CORRECTION: an earlier version of this docstring claimed a git-cloned node
    is absent from the frontend's template manifest, and that this was why all
    21 workflows failed with duration_seconds=0. Both halves were wrong.

    - The manifest DOES carry cloned nodes. Measured against a live backend:
      GET /api/workflow_templates -> {"ComfyUI-CADabra": ["analyse_CAD",
      "cad_curvature", ...]} -- ComfyUI scans custom_nodes/*/workflows/.
    - The real failure was the click. CADabra-1155:
      `Templates click failed: Locator.click: Timeout 10000ms exceeded`
      with `locator resolved to <button aria-label="Templates" ...>`. The
      button was found; playwright's actionability "stable" check could not
      pass because the window never presented a frame. The section lookup was
      never reached, so the manifest was never actually tested.

    Templates is the primary path again. This stays as the fallback: it is
    faster and immune to UI churn, and it is what runs if the sidebar or the
    card cannot be clicked.

    Returns True if the graph was loaded.
    """
    path = _workflow_json_path(target_name)
    if path is None:
        return False
    try:
        graph = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        log(f'  ext: could not read {path}: {e}')
        return False
    try:
        ok = page_arg.evaluate(
            """(g) => {
                const app = window.app || (window.comfyAPI && window.comfyAPI.app);
                if (!app) return 'no window.app';
                // API-format exports have no `nodes` array; the frontend
                // exposes a separate loader for those.
                if (!g.nodes && typeof app.loadApiJson === 'function') {
                    app.loadApiJson(g); return true;
                }
                if (typeof app.loadGraphData !== 'function') return 'no loadGraphData';
                app.loadGraphData(g); return true;
            }""", graph)
        if ok is not True:
            log(f'  ext: loadGraphData rejected {target_name}: {ok}')
            return False
        log(f'  ext: loaded {target_name} from {path}')
        return True
    except Exception as e:
        log(f'  ext: loadGraphData failed for {target_name}: {e}')
        return False


def _close_templates_panel(page_arg):
    """Dismiss the Templates browser if it is still open.

    It is an overlay across the canvas. Leaving it up hides the Run button:
    the actionability check fails, a force-click lands on the OVERLAY, and the
    driver logs `clicked Run` while /queue and /history stay empty. Measured in
    CADabra-1533 -- every workflow reported `Run clicked but nothing was
    queued` once the sidebar was re-enabled.

    Escape is deliberately not used: on Windows it hides the whole app window.
    """
    for sel in ('button[aria-label="Close"]:visible',
                'button[aria-label="Close dialog"]:visible',
                '[role="dialog"] button:has-text("Close"):visible'):
        try:
            btn = page_arg.locator(sel).first
            if btn.count():
                click_with_cursor(page_arg, btn)
                log(f'  ext: closed Templates panel via [{sel}]')
                sleep_capturing(page_arg, 1, fps=5)
                return True
        except Exception:
            continue
    # Nothing matched -- report whether an overlay is still up, since that is
    # the thing that will silently eat the Run click.
    try:
        still = page_arg.evaluate(
            """() => document.querySelectorAll(
                 '.p-dialog-mask, .p-overlay, [role="dialog"], .p-drawer-mask'
               ).length""")
        if still:
            log(f'  ext: WARNING Templates panel may still be open '
                f'({still} overlay element(s)); Run click may not land')
    except Exception:
        pass
    return False


def _run_named_card(page_arg, target_name):
    """Load the target workflow, install WS listener, click Run, wait up to
    600s. Returns {name, status, duration_seconds, error}."""
    # Click the template card first -- that is the user path, and it both loads
    # the graph AND dismisses the Templates overlay. Loading from disk while the
    # panel is open leaves the overlay covering the Run button.
    loaded = False
    try:
        # No `:visible` filter -- cards lower in the section's grid may be
        # off-screen until scrolled into view. data-testid is unique per
        # workflow, so the unfiltered locator is safe.
        card = page_arg.locator(f'[data-testid="template-workflow-{target_name}"]').first
        if card.count():
            log(f'  ext: clicking template {target_name}')
            card.scroll_into_view_if_needed()
            sleep_capturing(page_arg, 1, fps=5)
            click_with_cursor(page_arg, card)
            log(f'  ext: clicked template {target_name}')
            sleep_capturing(page_arg, 5, fps=5)
            loaded = True
        else:
            log(f'  ext: card {target_name} not in section DOM; loading from disk')
    except Exception as e:
        log(f'  ext: template click failed ({e}); loading from disk')

    if not loaded:
        if not _load_graph_from_disk(page_arg, target_name):
            return {'name': target_name, 'status': 'fail',
                    'duration_seconds': 0,
                    'error': f'could not load {target_name} from card or disk'}
        sleep_capturing(page_arg, 2, fps=5)

    # Whichever route loaded the graph, make sure nothing is left covering the
    # canvas before we go for Run.
    _close_templates_panel(page_arg)

    log('  ext: installing WS execution listener')
    try:
        page_arg.evaluate(_WS_LISTENER_JS)
    except Exception as e:
        log(f'  ext: WS listener install failed: {e}')

    # Drive the UI first -- click Run, exactly as a user would -- and only fall
    # back to POSTing /prompt when that demonstrably submitted nothing.
    log('  ext: clicking Run')
    if not _click_run_and_confirm(page_arg):
        _q = _queue_prompt(page_arg)
        if _q == 'rejected':
            # ComfyUI validated the graph and refused it, so it will never emit
            # an execution event. Report the real reason now rather than
            # burning the full workflow timeout and calling it a TIMEOUT.
            return {'name': target_name, 'status': 'fail',
                    'duration_seconds': 0,
                    'error': 'prompt rejected by ComfyUI (see session.log for '
                             'node_errors)'}

    log('  ext: waiting for execution_success / execution_error')
    run_deadline = time.time() + _WORKFLOW_TIMEOUT_S
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
    elif elapsed >= _WORKFLOW_TIMEOUT_S:
        log(f'  ext: WORKFLOW TIMEOUT (no execution_success/error in '
            f'{_WORKFLOW_TIMEOUT_S}s)')
    else:
        log(f'  ext: execution_success after {elapsed}s')

    if err:
        status = 'fail'
        err_str = json.dumps(err, default=str)
    elif elapsed >= _WORKFLOW_TIMEOUT_S:
        status = 'timeout'
        err_str = f'no execution_success/error in {_WORKFLOW_TIMEOUT_S}s'
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


def _load_node_toml():
    """Parse the node's comfy-test.toml, preferring the LOCAL copy the host
    staged (COMFY_TEST_NODE_LOCAL_COPY). The GitHub-raw fallback fetches the
    remote branch's file, which silently ignores uncommitted --dev changes
    to the toml (workflow lists, timeout)."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    local = os.environ.get('COMFY_TEST_NODE_LOCAL_COPY', '')
    if local:
        p = Path(local) / 'comfy-test.toml'
        if p.is_file():
            log(f'  ext: reading comfy-test.toml from local copy {p}')
            return tomllib.loads(p.read_text(encoding='utf-8'))
    node_repo = os.environ.get('NODE_REPO', '')
    node_branch = os.environ.get('NODE_BRANCH', 'main')
    if node_repo:
        toml_url = f'https://raw.githubusercontent.com/{node_repo}/{node_branch}/comfy-test.toml'
        log(f'  ext: fetching comfy-test.toml from {toml_url}')
        return tomllib.loads(
            urllib.request.urlopen(toml_url, timeout=10).read().decode('utf-8'))
    return {}


def _apply_workflow_timeout(data):
    """Honor [test.workflows].timeout unless the operator pinned
    COMFY_TEST_WORKFLOW_TIMEOUT_S. The 180s default killed remeshing_all
    mid-execution (measured GeometryPack-2121: legitimate 260s+ workload
    reported as WORKFLOW TIMEOUT while the toml said timeout = 300)."""
    global _WORKFLOW_TIMEOUT_S
    if os.environ.get('COMFY_TEST_WORKFLOW_TIMEOUT_S'):
        return
    t = data.get('test', {}).get('workflows', {}).get('timeout')
    if isinstance(t, (int, float)) and t > 0:
        _WORKFLOW_TIMEOUT_S = int(t)
        log(f'  ext: per-workflow timeout {_WORKFLOW_TIMEOUT_S}s (from comfy-test.toml)')


def _parse_cpu_spec():
    """Returns (mode, items) parsed from comfy-test.toml's
    [test.workflows].cpu (or .cuda when COMFY_TEST_CUDA=1)."""
    cpu_mode = 'all'
    cpu_items = []
    try:
        data = _load_node_toml()
        if data:
            _apply_workflow_timeout(data)
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
    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{_CDP_PORT}')
    _browser_ref[0] = browser
    page = main_page(browser)
    if not page:
        log('No usable page')
        sys.exit(0)
    log(f'Main page: {page.url} | {page.title()}')
    install_cursor(page)
    install_dialog_handler(page)
    install_viewport_size(page)
    # Start streaming ComfyUI's stdout into our session log so
    # execution_error tracebacks, missing-input warnings, etc. show up
    # live in the driver output (and the live viewer's log panel).
    _start_comfyui_log_tail()
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
    tiles_picked = []  # every TILE| click across all wizard screens
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
                if kind == 'tile':
                    tiles_picked.append(sig)
                sleep_capturing(page, 1, fps=4)
            except Exception as e:
                log(f'  click [{sig}] failed: {e}')
                clicked[sig] = time.time()
        else:
            time.sleep(2)
    else:
        log(f'  driver timed out after {int(time.time()-start)}s without /system_stats')

    if _CUDA_MODE:
        # Verify what the wizard actually installed by asking the server,
        # not by tile bookkeeping: on a detected-NVIDIA box the wizard
        # PRE-selects CUDA and enables Continue on entry, so zero tile
        # clicks is normal there (measured GeometryPack-2104: no tile
        # clicked, Device: cuda:0). The failure this guards is the wizard
        # silently installing CPU torch when GPU detection is broken in
        # the environment (measured GeometryPack-2012 in the sandbox guest
        # before nvidia-smi was exposed on PATH).
        dev_types = 'unknown'
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{_COMFY_PORT or 8188}/system_stats',
                    timeout=5) as r:
                stats = json.loads(r.read().decode())
            dev_types = ','.join(d.get('type', '?')
                                 for d in stats.get('devices', [])) or 'none'
        except Exception as e:
            dev_types = f'unknown ({e.__class__.__name__})'
        if 'cuda' in dev_types:
            log(f'  wizard: server devices [{dev_types}] confirm CUDA '
                f'(tiles clicked: {tiles_picked or "none -- wizard pre-selected"})')
        else:
            log(f'  wizard: WARN: COMFY_TEST_CUDA=1 but server devices are '
                f'[{dev_types}] (tiles clicked: {tiles_picked}); the app '
                f'installed a non-CUDA torch -- check GPU detection in the guest')

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
                urllib.request.urlopen(f'http://127.0.0.1:{_CDP_PORT}/json/version', timeout=1)
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
            nb = p.chromium.connect_over_cdp(f'http://127.0.0.1:{_CDP_PORT}')
            _browser_ref[0] = nb
            _capture_warned[0] = False
            np = main_page(nb)
            if np is None:
                log('  recovery: no page after reconnect')
                return nb, None
            install_cursor(np)
            install_dialog_handler(np)
            install_viewport_size(np)
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
    # Read branch early so we can decide whether to open the Extensions
    # sidebar. On --dev we do a visible shell install and never touch
    # Manager UI -- opening Extensions is pointless noise on the video.
    _early_node_branch = os.environ.get('NODE_BRANCH', 'main')
    POST_ACTIONS = [
        ('Close Templates', 'templates',
         ['button[aria-label="Close"]:visible'], 8),
    ]
    if _early_node_branch == 'main':
        POST_ACTIONS.append(
            ('Extensions', 'extensions',
             ['button[aria-label="Extensions"]:visible'], 8),
        )
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
            # Escape is NOT safe on Windows: it makes the whole ComfyUI window
            # vanish (see the long note in the wizard walker above), which
            # silently breaks every screenshot and every subsequent click for
            # the rest of the run. The dialog not being found is a non-event;
            # losing the window is fatal. Only take the fallback off-Windows.
            if kind in ('templates', 'cloud') and sys.platform != 'win32':
                try:
                    page.keyboard.press('Escape')
                    log(f'  post: pressed Escape ({name} fallback)')
                    sleep_capturing(page, 2, fps=5)
                except Exception:
                    pass
            elif kind in ('templates', 'cloud'):
                log(f'  post: skipping Escape fallback on Windows '
                    f'(it hides the app window)')

    # Install via Manager's /customnode/install/git_url. This is the
    # dev variant of cdp_driver -- the production cdp_driver.py uses the
    # registry-tile UI flow for main-branch CNR installs. Here we POST
    # `https://github.com/<repo>@<branch>` to Manager's git-clone
    # endpoint; gitclone_install (manager_core.py:2169) parses
    # `<url>@<gitref>` via extract_url_and_commit_id and runs
    # `git checkout <gitref>` after cloning. Requires security_level=weak,
    # pre-written by _desktop_runner._write_manager_security_config
    # before app launch.
    node_repo = os.environ.get('NODE_REPO', '')
    node_branch = os.environ.get('NODE_BRANCH', 'main')
    base = f'http://127.0.0.1:{_COMFY_PORT or 8188}'

    # --- --dev branch gate ---
    # For any non-main branch, skip the Manager UI clickthrough entirely
    # and do a visible git-clone + pip + install.py inside a console
    # overlay. The video becomes a live demo of the exact commands a
    # dev-branch user would run in their own Desktop terminal. Bare
    # --desktop (NODE_BRANCH=main) keeps the existing Manager-UI flow
    # below unchanged -- that's the "install the pyproject.toml version"
    # path CADabra publishes to CNR nightly.
    _did_visible_install = False
    if node_branch and node_branch != 'main':
        _, _install_path, _custom_nodes, _venv_python = _find_active_comfy_install()
        log(f'  ext: --dev path (branch={node_branch}) -- using visible shell install, skipping Manager UI')
        _install_via_visible_shell(page, node_repo, node_branch,
                                    _custom_nodes, _venv_python)
        _did_visible_install = True

    # Hand the UI over to a real browser window for everything that follows.
    #
    # The wizard and the node install are done at this point, and those are the
    # only parts that genuinely need Comfy Desktop's own window. Workflow
    # running only needs a ComfyUI frontend, and the Electron window cannot
    # provide a usable one here: it presents no frames, so screenshots hang and
    # every click times out on the actionability check.
    #
    # Swapping `page`/`browser` wholesale means _restart_comfy, _run_named_card,
    # snap() and frame() all keep working untouched -- the reboot is done via
    # Manager's HTTP API and the reload is a normal page.reload(), neither of
    # which cares which chromium is showing the page.
    #
    # The Electron process stays alive throughout: it owns the ComfyUI python
    # backend we are testing against. We just stop looking at its window.
    if sys.platform == 'win32' and os.environ.get('COMFY_TEST_BROWSER_UI', '1') != '0':
        _b_ui, _p_ui = _launch_browser_ui(p, _comfy_base_url(), OUT)
        if _p_ui is not None:
            _electron_browser = browser
            browser, page = _b_ui, _p_ui
            _browser_ref[0] = browser
            # Capture was disabled against the Electron page; the browser window
            # composites normally, so give it another chance.
            _capture_disabled[0] = False
            _capture_warned[0] = False
            _capture_fail_streak[0] = 0
            install_cursor(page)
            install_dialog_handler(page)
            # Deliberately NOT install_viewport_size() here.
            #
            # It pins the viewport via a device-metrics override, which existed
            # to make the Electron window's surface capture usable. On a real
            # browser window it actively breaks input: the emulated viewport
            # (1280x863) no longer matches the window (1900x1030), so the
            # coordinates javascript reports and the coordinates input events
            # are dispatched at diverge. Playwright's hit-test then refuses the
            # click -- which is why EVERY click needed force=True -- and
            # force=True dispatches at a point that maps somewhere else.
            #
            # Measured in CADabra-1636: with the override in place the Run
            # button reported pointerdown=0 mousedown=0 click=0 while
            # elementFromPoint at its centre returned the button itself, and
            # rafFired=1 proved the window WAS presenting. The window is
            # already a sensible size, so just use it as-is.
            _wait_canvas_ready(page, 120)
            log('  browser-ui: UI now driven from the browser window; '
                'the Electron app stays up to host the ComfyUI backend')

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

    # _post_json_empty and _reboot_via_manager_and_wait now live at
    # module scope (see ~L606) so per-workflow _restart_comfy can reuse
    # them. This block previously defined them locally.

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

    # Only the Manager-UI search below reads this, and that branch is skipped
    # after a git-clone install -- so on --dev it was two network round trips
    # whose results were fetched, logged and then never used.
    if _did_visible_install:
        NODE_DISPLAY_NAME = PUBLISHER = NODE_VERSION = None
    else:
        NODE_DISPLAY_NAME, PUBLISHER, NODE_VERSION = _fetch_node_meta()
        log(f'  ext: node meta = display={NODE_DISPLAY_NAME!r} '
            f'publisher={PUBLISHER!r} version={NODE_VERSION!r}')

    ui_install_done = False
    if NODE_DISPLAY_NAME and PUBLISHER and not _did_visible_install:
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
                        # See the Escape note in the wizard walker: on Windows
                        # this hides the app window and breaks the rest of the
                        # run. Leaving a dropdown open is the lesser problem.
                        if sys.platform != 'win32':
                            log('  ext: no Nightly/Latest option matched, dismissing')
                            try: page.keyboard.press('Escape')
                            except Exception: pass
                        else:
                            log('  ext: no Nightly/Latest option matched; not '
                                'sending Escape on Windows (hides the window)')
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

    if not ui_install_done and not _did_visible_install:
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
            # shutil, not `rm -rf`: there is no rm on Windows, and this used
            # to run with check=True and no try, so it hard-killed the driver
            # on any Windows re-run where the node dir already existed.
            # onerror clears the read-only bit git leaves on .git objects.
            import shutil as _shutil
            import stat as _stat

            def _on_rm_error(func, path, _exc):
                try:
                    os.chmod(path, _stat.S_IWRITE)
                    func(path)
                except Exception:
                    pass

            _shutil.rmtree(str(node_dir), onerror=_on_rm_error)
            if node_dir.exists():
                raise RuntimeError(f'could not remove {node_dir} before clone')
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

        # Match Manager's install-script step (manager_core.py:878): after pip,
        # run install.py from the repo dir if present. This is how nodes that
        # declare sibling-node deps via comfy-env-root.toml get those siblings
        # cloned (e.g. CADabra's install.py -> comfy_env.install() -> clones
        # ComfyUI-GeometryPack). Skipping it leaves the fallback with a
        # partial install where sibling nodes never come along.
        install_py = node_dir / 'install.py'
        if install_py.is_file() and venv_python.exists():
            inst_cmd = [str(venv_python), 'install.py']
            log(f'  ext:   $ {" ".join(inst_cmd)}  (cwd={node_dir})')
            r = subprocess.run(inst_cmd, cwd=str(node_dir),
                               capture_output=True, text=True, timeout=900)
            for line in (r.stdout + r.stderr).splitlines()[-25:]:
                log(f'  ext:     {line}')
            if r.returncode != 0:
                log(f'  ext:   WARNING: install.py exit={r.returncode} (continuing)')

        _reboot_via_manager_and_wait()

        # Even after Python restart, the frontend's extension-list cache
        # (loaded when the page first rendered) can be stale. Force a
        # renderer reload so it re-fetches the new object_info and
        # populates EXTENSIONS / comfyui-cadabra in the Templates nav.
        log('  ext: reloading renderer to refresh extension list')
        try:
            page.reload(wait_until='load', timeout=30_000)
            log('  ext: renderer reloaded')
        except Exception as e:
            log(f'  ext: renderer reload failed (non-fatal): {e}')

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

        # Skip the inline first-workflow run after a git-clone install, so all
        # workflows go through ONE code path (the loop below), which now drives
        # the Templates sidebar too. This block is a copy-paste duplicate of the
        # loop body; running #1 here and #2..N below meant fixing every bug
        # twice.
        #
        # NB the old reason given here -- "a non-registry clone is absent from
        # the frontend template manifest" -- was wrong; see the correction in
        # _load_graph_from_disk. The skip is kept for the de-duplication reason
        # above, not that one.
        #
        # The multi-workflow loop BELOW is deliberately left outside this guard
        # -- it lives in the same else-branch, and gating both made a run
        # execute 0 workflows. _workflow_results stays empty, so first_name is
        # None and every workflow runs through the loop.
        if _did_visible_install:
            log('  ext: git-clone install -- all workflows run via the loop below')
        else:
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
                    # Same diagnostic as _open_templates_and_section() -- see
                    # comment there. Tells us whether the section text is
                    # literally absent (backend never registered install) vs
                    # present but our nav-selectors missed it.
                    try:
                        diag = page.evaluate(f"""
                        (name) => {{
                            const nav = document.querySelector('nav');
                            if (!nav) return {{nav: false}};
                            const text = (nav.textContent || '').toLowerCase();
                            const hit = text.includes(name.toLowerCase());
                            const labels = [...nav.querySelectorAll(
                                '[role="button"], button, span, a'
                            )].map(el => (el.textContent||'').trim())
                              .filter(t => t && t.length < 80);
                            const seen = new Set(); const uniq = [];
                            for (const l of labels) {{
                                if (!seen.has(l)) {{ seen.add(l); uniq.push(l); }}
                                if (uniq.length >= 40) break;
                            }}
                            return {{
                                nav: true, text_contains: hit,
                                label_count: labels.length, labels: uniq,
                            }};
                        }}""", NODE_PACKAGE_NAME)
                        log(f'  ext:   diag: nav-present={diag.get("nav")} '
                            f'text_contains_{NODE_PACKAGE_NAME}={diag.get("text_contains")} '
                            f'label_count={diag.get("label_count")}')
                        for lbl in (diag.get('labels') or []):
                            log(f'  ext:     nav label: {lbl!r}')
                    except Exception as e:
                        log(f'  ext:   diag failed: {e}')
                    # Leave Comfy Desktop running for 4 minutes so we can
                    # inspect the live DOM via CDP from outside the driver.
                    log('  ext: PAUSING 240s so external CDP inspection is possible')
                    log('  ext:   port:  cat "$HOME/Library/Application Support/Comfy Desktop/DevToolsActivePort"')
                    log('  ext:   pages: curl -s http://localhost:<port>/json/list')
                    try:
                        sleep_capturing(page, 240, fps=5)
                    except Exception:
                        pass
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
                    data = _load_node_toml()
                    if data:
                        _apply_workflow_timeout(data)
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

                # UI first, POST /prompt only as the proven-nothing-queued
                # fallback -- same ordering as _run_named_card.
                log('  ext: clicking Run')
                if not _click_run_and_confirm(page):
                    _queue_prompt(page)

                # Wait for execution_success / execution_error from the WS.
                log('  ext: waiting for execution_success / execution_error')
                run_deadline = time.time() + _WORKFLOW_TIMEOUT_S
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
                elif elapsed >= _WORKFLOW_TIMEOUT_S:
                    log(f'  ext: WORKFLOW TIMEOUT (no execution_success/error in '
                f'{_WORKFLOW_TIMEOUT_S}s)')
                else:
                    log(f'  ext: execution_success after {elapsed}s')

                # Record this workflow's outcome for results.json.
                if err:
                    _status = 'fail'
                    _err_str = json.dumps(err, default=str) if err else None
                elif elapsed >= _WORKFLOW_TIMEOUT_S:
                    _status = 'timeout'
                    _err_str = f'no execution_success/error in {_WORKFLOW_TIMEOUT_S}s'
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
            # Drive the Templates sidebar on BOTH install paths.
            #
            # This used to be skipped after a git-clone install, on the premise
            # that "a non-registry clone is not in the frontend's template
            # manifest". That premise was wrong. ComfyUI scans
            # custom_nodes/*/workflows/ and serves them -- verified against a
            # live backend, /api/workflow_templates returns
            # {"ComfyUI-CADabra": ["analyse_CAD", "cad_curvature", ...]}.
            #
            # What actually broke the sidebar was the click, not the lookup:
            # CADabra-1155 logs `Templates click failed: Locator.click: Timeout
            # 10000ms exceeded` with the locator RESOLVED -- playwright's
            # actionability "stable" check on a window that never presented a
            # frame. The run never reached the section lookup at all. That is
            # fixed now (the UI runs in a real browser window on Windows), so
            # exercising the sidebar is both possible and closer to what a user
            # actually does.
            #
            # A miss is NOT fatal: fall through to _run_named_card, which loads
            # the JSON from disk. Previously a miss recorded a fail and skipped
            # the workflow entirely, so one flaky sidebar cost a real result.
            if not _open_templates_and_section(page, NODE_PACKAGE_NAME_outer):
                log(f'  loop: Templates+section not opened for {_wf_name}; '
                    f'falling back to loading the graph from disk')
            _start = fi[0]
            _result = _run_named_card(page, _wf_name)
            _workflow_results.append(_result)
            _frame_ranges.append((_wf_name, _start, fi[0]))

    snap(page, 'final')
    log(f'Captured {fi[0]} frames')
    browser.close()
    # The browser-UI window lives in session 1 under its own scheduled task, so
    # it survives this process unless we explicitly end it. A leftover one would
    # sit on the desktop and get captured by the next run's screencap.
    if sys.platform == 'win32':
        _stop_browser_ui()

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
# Anything that is not a pass counts as failed -- 'timeout' and 'rejected'
# included. Counting only status=='fail' reported success=true with a
# timed-out workflow (measured GeometryPack-2144: remeshing_all timeout,
# summary said failed=0).
_failed = sum(1 for w in _workflow_results if w.get("status") != "pass")
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

# Nothing to encode if capture never worked. Without this guard ffmpeg is
# handed a frame_%06d.png pattern matching no files and fails with a
# confusing "Could find no file with path ... and index in the range 0-4",
# which reads like an ffmpeg bug rather than "there were no frames".
_have_frames = fi[0] > 0 and any(FRAMES.glob('frame_*.png'))
if not _have_frames:
    log('No frames captured -- skipping video encode. results.json and the '
        'workflow results above are unaffected.')

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
if _have_frames:
    # Silence ffmpeg's per-encode chatter (~100 lines of libx264 config +
    # frame stats). `-loglevel error` mutes info/warning, `-nostats` mutes
    # the progress line. capture_output preserves stderr for the error path.
    r = subprocess.run(
        [ffmpeg_exe, '-y', '-loglevel', 'error', '-nostats',
         '-framerate', '5',
         '-i', str(FRAMES / 'frame_%06d.png'),
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
         '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
         str(mp4)],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        log(f'Wrote {mp4}')
    else:
        log(f'ffmpeg failed rc={r.returncode}: {r.stderr[:500]}')

# Per-workflow mp4 encoding. Each entry in _frame_ranges is
# (workflow_name, start_idx, end_idx). frame_NNNNNN.png is 1-indexed
# (fi[0] is incremented BEFORE the screenshot), so ffmpeg's
# -start_number is start_idx+1 and -frames:v is the count.
# Same guard as the master encode above -- without it this loop ran ffmpeg
# once per workflow against a frames dir that does not exist, producing 18
# identical "Could find no file with path" errors in a run where capture was
# disabled from the start.
_frame_ranges_local = locals().get('_frame_ranges', []) if _have_frames else []
videos_root = _RUN_DIR / 'videos'
try:
    for wf_name, start_idx, end_idx in _frame_ranges_local:
        count = max(1, end_idx - start_idx)
        wf_dir = videos_root / wf_name
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf_mp4 = wf_dir / 'driver.mp4'
        r = subprocess.run(
            [ffmpeg_exe, '-y', '-loglevel', 'error', '-nostats',
             '-start_number', str(start_idx + 1),
             '-framerate', '5',
             '-i', str(FRAMES / 'frame_%06d.png'),
             '-frames:v', str(count),
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
             '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
             str(wf_mp4)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            log(f'  videos/{wf_name}/driver.mp4 placed (frames {start_idx+1}..{end_idx})')
        else:
            log(f'  videos/{wf_name}/driver.mp4 encode failed rc={r.returncode}: {r.stderr[:300]}')
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
