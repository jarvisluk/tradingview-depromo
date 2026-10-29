#!/usr/bin/env python3
"""Patch TradingView Desktop (Electron) with promo/upsell suppression.

By default patches the *installed* app in place:

  1. finds /Applications/TradingView.app (or --src)
  2. quits the app if it is running (graceful, then forced)
  3. restores the pristine backup if one exists (=> idempotent re-runs),
     else snapshots app.asar / Info.plist as *.orig before touching anything
  4. rewrites app.asar with the patches below, removes the
     ElectronAsarIntegrity declaration in Info.plist, ad-hoc re-signs
  5. relaunches the app

In-place mode deliberately keeps everything identical to the stock client
from the outside: same path, same name/icon, same bundle id, and — because
the userData-isolation patches (p10/p12) are skipped — the same
~/Library/Application Support/TradingView data (settings, layouts, login).
The only visible difference is the ad-hoc code signature (required after
modifying the bundle).

Pass --dst to fall back to the old behaviour: patch a *copy* of the app
(kept side-by-side via a separate userData dir).

Patches applied (each must match EXACTLY once, else abort):
  index.js (main process):
    p1  feature-requester predicate -> always allow (no createGoProDialog
        injection on tab detach / multi-window)
    p1b requestProFeature() -> no-op (belt & braces)
    p2  showAppOnboardingDialog() -> no-op (welcome screen gone)
    p3  TrackEventService.sendEvent() -> immediate return (shell GA events)
    p11 first-run auto sign-in prompt ("welcome" dialog, closable:false)
        bypassed p2 via doSignIn("first_sign") -> signIn -> showWelcomeDialog
    p16 web-tab windows always get backgroundThrottling:false (stock code
        gates it on the server-side feature flag tvd_background_throttling;
        when the flag is on, a backgrounded/occluded window gets timer-
        throttled, the site's streamer keepalive starves and the data feed
        silently stops after a few minutes). Also appends the Chromium
        switch disable-backgrounding-occluded-windows so fully covered
        windows on macOS keep rendering (rAF stays alive).
    p10 (copy mode only) isolate userData ->
        ~/Library/Application Support/TradingView-patched
    p12 (copy mode only) file logger follows the isolated userData dir
  app/new-tab/bundle.js:
    p4  count_limit on createLayout  -> close modal, no GoPro upsell
    p5  count_limit on copyLayout    -> close modal, no GoPro upsell
    p6  /chart layout context menu "Open in new window" -> really open
        a new window instead of the "Sign up for free" modal
    p7  "Create new layout" button no longer demands sign-in for bubbles
  configs/nav-rules.json:
    p8  remove gopro/pricing/black-friday entries from the open-external rule
  configs/config.json:
    p9  sentry.upload = false (no crash/attachment upload)
  preload/target-tvd-host-preload.js:
    p13 web pages: stub window.TradingView.createGoProDialog in the page's
        main world (site-side "Analyze markets in TradingView Desktop" /
        manyDesktopWindows upsell shown to Basic accounts). Injected via
        webFrame.executeJavaScript because web tabs are contextIsolation:true.
    p14 web pages: DOM-level killer for the site-internal paywall/gopro
        dialogs ("Join for free" signup gate AND the signed-in
        "Analyze markets in TradingView Desktop" upsell -- both render via
        the site's openWall(PAYWALL_KIND) and bypass the window.TradingView
        alias): hides the body-level overlay, un-locks body scroll.
    p15 web pages: hide the "Invite friends. Earn cash" bottom-left AD
        banner (matched by its 'Get my link' button + 'Invite friends'
        copy; best-effort).
    p18 web pages: streamer watchdog. A data stream can go silent while
        the socket stays ESTABLISHED (server stops sending, even app-level
        pings stop) and the site never notices.
        Hooks WebSocket, tracks last inbound frame on data-streamer sockets,
        closes a 15s-silent socket so the site's own reconnect logic
        rebuilds the stream; reload is a last resort (120s reconnect
        grace first, then exponential backoff, max 5 per session).
    p19 web pages: CSS injected at document start hides gopro-class
        dialogs from the first paint; kill() also tries the dialog's
        close button before hiding (logged-in upsell walls).

    The bottom-left TradingView logo watermark (painted by the charting
    library directly onto the chart pane canvas, no DOM) is NOT removed:
    see the NOTE inside INDEX_JS_COMMON for why no remaining Electron
    hook in TVD's custom build can rewrite the bundle.

Note: server-side enforcement (layout limits, plan quotas, web ads highly
gated by the account) is NOT changed; client prompts only.

Usage:
  python3 scripts/patch-tradingview.py                 # in-place on /Applications/TradingView.app
  python3 scripts/patch-tradingview.py --src /path/to/TradingView.app
  python3 scripts/patch-tradingview.py --no-restart    # don't quit/relaunch
  python3 scripts/patch-tradingview.py --dst out.app   # legacy: patch a copy

Uninstall: restore the *.orig backups inside the bundle (or reinstall the
app). The script restores them automatically before every run.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_DEFAULT = Path("/Applications/TradingView.app")
DST_DEFAULT = REPO / "targets" / "TradingView-patched.app"


# --------------------------------------------------------------------------
# Patch table: file -> list of patches. Every entry must appear exactly once.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Patch:
    id: str
    old: bytes
    new: bytes


INDEX_JS_COMMON = [
    # p1: wrapper predicate -- unauthenticated users no longer get the
    # "window.TradingView.createGoProDialog({feature:'manyDesktopWindows'})"
    # injection; every wrapped feature simply runs.
    Patch(
        "p1",
        b"M_(n,(()=>(0,xt.Dv)().getCurrentUser().isAuthenticated))",
        b"M_(n,(()=>!0))",
    ),
    # p1b: hard-stop the platform GoPro dialog trigger itself.
    Patch(
        "p1b",
        b"async requestProFeature(){",
        b"async requestProFeature(){return void 0;",
    ),
    # p2: welcome/onboarding dialog -> no-op
    Patch(
        "p2",
        b"showAppOnboardingDialog(e,t){(0,M.uY)(this.authHandler.showWelcomeDialog(e||H_.AppFirstLaunch,t))}",
        b"showAppOnboardingDialog(e,t){}",
    ),
    # p3: shell analytics events -> never sent
    Patch(
        "p3",
        b"async sendEvent(e){",
        b"async sendEvent(e){return void 0;",
    ),
    # p11: first-run auto sign-in prompt ("welcome" dialog, closable:false)
    #      bypassed p2 via doSignIn("first_sign") -> signIn -> showWelcomeDialog
    Patch(
        "p11",
        b'(0,xt.Dv)().doSignIn("first_sign")',
        b"void 0",
    ),
    # p16: web-tab windows must never run with background throttling.
    #      bt() only sets backgroundThrottling:false when the server-side
    #      feature flag tvd_background_throttling (resolved by asking the
    #      SITE: window.TradingView.isFeatureEnabled) is false. On fresh
    #      profiles or after a flag flip the throttle stays ON: an occluded
    #      or hidden window gets its timers clamped (Chromium Intensive Wake
    #      Up Throttling after ~5 min), the site's streamer keepalive
    #      starves and the server silently stops pushing chart data.
    #      TV's own renderer-worker / toast windows already hardcode false.
    #      The appended Chromium switch also stops macOS from pausing
    #      rAF/painting for windows fully covered by other windows.
    Patch(
        "p16",
        b"const r=ht().isFeatureEnabled(pt,!1);return(0,M.iT)()||r||(n.webPreferences.backgroundThrottling=!1),",
        b"const r=ht().isFeatureEnabled(pt,!1);"
        b'try{require("electron").app.commandLine.appendSwitch("disable-backgrounding-occluded-windows")}catch(_e){}'
        b"return n.webPreferences.backgroundThrottling=!1,",
    ),
    # NOTE: an attempt to strip the bottom-left TradingView logo watermark
    #      (painted by the charting library directly onto the chart pane
    #      canvas via _drawWatermark()) was made and abandoned. The needle
    #      is unique and the patch itself is trivial, but TVD's custom
    #      Electron strips session.webRequest.filterResponseData (verified:
    #      not a function), and custom-scheme protocol proxying
    #      (registerSchemesAsPrivileged + protocol.handle + onBeforeRequest
    #      redirect) registers fine yet never receives any request --
    #      renderer fetches to the custom scheme fail before reaching the
    #      network stack. No remaining hook can rewrite response bodies.
]

# Copy-mode only: a patched *copy* must not share state with the original app.
# Skipped in in-place mode on purpose — keeping the stock userData is exactly
# what makes the patched app behave (and look) like the original.
INDEX_JS_ISOLATION = [
    # p10: isolate userData -> ~/Library/Application Support/TradingView-patched
    #      Injected at the VERY START of the bundle IIFE, before any webpack
    #      module factory runs (config store / logging init otherwise cache
    #      the old path). Also yields a separate single-instance lock, so
    #      the original app can run side-by-side.
    Patch(
        "p10",
        b'(()=>{var e,t,n={82069(e,t)',
        b'(()=>{try{require("electron").app.setPath("userData",'
        b'require("electron").app.getPath("appData")+"/TradingView-patched")}catch(_e){}'
        b"var e,t,n={82069(e,t)",
    ),
    # p12: file logger resolved appData/<name>/logs instead of userData;
    #      make it follow the isolated userData dir
    Patch(
        "p12",
        b'this.logsPath=W.l.makeAppDataPath("logs")',
        b'this.logsPath=require("electron").app.getPath("userData")+"/logs"',
    ),
]

NEW_TAB_PATCHES = [
    # p4: createLayout rejected as count_limit -> close the modal, skip GoPro
    Patch("p4", b"j===YS&&(M(),L())", b"j===YS&&M()"),
    # p5: copyLayout rejected as count_limit -> close modal, skip GoPro
    Patch("p5", b"M===YS&&(u(),N())", b"M===YS&&u()"),
    # p6: /chart context-menu "Open in new window" was a sign-up prompt
    #     (u() == openSignUpModal); restored to the real open action.
    Patch(
        "p6",
        b'{type:"common",icon:UL.j,onClick:()=>{u(),I()},title:M("Open in new window")}',
        b'{type:"common",icon:UL.j,onClick:()=>{xL().handleOpen(DL.NewWindow,j,T,L),I()},title:M("Open in new window")}',
    ),
    # p7: create-new-layout button no longer switches to sign-up modal
    Patch("p7", b"onClick:()=>{N?.userId?M():L()}", b"onClick:()=>{M()}"),
]

PRELOAD_PATCHES = [
    # p13: the "Analyze markets in TradingView Desktop" GoPro upsell is
    #      rendered by the WEBSITE itself (window.TradingView.createGoProDialog)
    #      for Basic accounts -- it never goes through the shell's
    #      requestProFeature. Web tabs run contextIsolation:true/sandbox:true,
    #      so the preload cannot touch the page world directly; instead
    #      schedule a main-world script (webFrame.executeJavaScript) that
    #      installs an assignment trap + poll and stubs the dialog factory.
    # p14: the site ALSO opens that dialog family through internal modules
    #      (anonymous "Join for free" gate -- the window.TradingView alias
    #      is never consulted), and the dialog has no close button while
    #      body gets i-no-scroll. Kill it at the DOM level: hide the
    #      top-level overlay of any dialog carrying a gopro- css-module
    #      class (or the known upsell copy), un-lock body scroll. Only the
    #      dialog's own wrapper is hidden: #overlap-manager-root hosts every
    #      dialog, menu and toast. The server's connection-limit notice
    #      ("We've closed this connection", Basic = 2 connections) is never
    #      hidden: it is the only explanation of a cut and carries the
    #      site's "Restore connection" button.
    # p15: the "Invite friends. Earn cash" bottom-left banner is an ad
    #      iframe in the charting-ad toast group of #overlap-manager-root
    #      (its copy is not in the page DOM): hidden by CSS, then dismissed
    #      through the group's own close button.
    # p18: streamer watchdog. A data stream can go silent while the socket
    #      stays ESTABLISHED: the server stops sending everything, including
    #      app-level pings -- a silent starvation the site never notices.
    #      (The stalls traced in detail were plan connection-limit cuts,
    #      which are left alone; see below.)
    #      Hook WebSocket, track last inbound frame on each data-streamer
    #      socket (*/socket.io/websocket*), and when it goes quiet for 15s
    #      (healthy ping cadence is 5-10s) close() it so the site's own
    #      reconnect logic rebuilds the stream seamlessly. If no new socket
    #      shows up within 10s, fall back to a rate-limited page reload.
    #      Plan-limit cuts are left alone: while a connection-limit or
    #      multi-tab upsell dialog is up, no close/reload happens (reloading
    #      just steals the connection from another tab, which then gets cut,
    #      and so on). Only the tab the user is looking at takes the
    #      connection back: a cut tab reloads when it is (or becomes) the
    #      visible one, at most once per 30s.
    # p19: instant CSS suppression -- gopro-class dialogs are display:none
    #      from the first paint (no JS latency), and the TradingView chart
    #      watermark is hidden. Complements the JS killers above.
    # The preload is registered session-wide (defaultSession.setPreloads),
    # so it also runs in the shell's own file:// pages (tab bar, menus,
    # toasts). Everything above is gated to http(s) tradingview.com pages:
    # otherwise p18 sees no streamer socket in the tab bar, "reloads" it
    # after 120s, and the shell page comes back without its tabs (its
    # initial state is pushed only once by the main process).
    Patch(
        "p13-18",
        b"localStorage.setItem(s,process.env[s]??c)})()",
        b"localStorage.setItem(s,process.env[s]??c);"
        b'try{e.webFrame.executeJavaScript('
        b'"(function(){try{'
        b"if(!/^https?:$/.test(location.protocol)||!/(^|\\\\.)tradingview\\\\.com$/i.test(location.hostname))return;"
        b"var s=function(v){try{if(v&&typeof v==='object'&&typeof v.createGoProDialog==='function'){v.createGoProDialog=function(){};}}catch(e){}return v;};"
        b"var d=null;try{d=Object.getOwnPropertyDescriptor(window,'TradingView');}catch(e){}"
        b"if(!d){var t;try{t=s(window.TradingView);}catch(e){}"
        b"try{Object.defineProperty(window,'TradingView',{configurable:true,get:function(){return t;},set:function(v){t=s(v);}});}catch(e){}}"
        b"var i=setInterval(function(){try{var v=window.TradingView;if(v&&typeof v.createGoProDialog==='function'&&!v.createGoProDialog.__tvp){v.createGoProDialog=function(){};v.createGoProDialog.__tvp=1;}}catch(e){}},200);"
        b"var TXT=['Join for free','Make every move count','Look first / Then leap','Analyze markets in TradingView Desktop','Upgrade your plan and discover the full power'];"
        b"var KEEP='closed this connection';var LIM=[KEEP,'Analyze markets in TradingView Desktop'];"
        b"var __lim=function(){try{var L=document.querySelectorAll('div[class*=dialog-]');for(var k=0;k<L.length;k++){var tx=L[k].textContent||'';for(var j=0;j<LIM.length;j++){if(tx.indexOf(LIM[j])>=0)return true;}}}catch(e){}return false;};"
        b"try{var __st=document.createElement('style');__st.textContent='div[class*=dialog]:has([class*=gopro]):not([data-tvp-keep]){display:none!important}[class*=watermark]{display:none!important}[class*=toastGroup-]:has([data-name=toast-group-close-button-charting-ad]),[class*=toast-]:has([class*=adContainer-]){display:none!important}';document.documentElement.appendChild(__st);}catch(e){}"
        b"var kill=function(){try{var L=document.querySelectorAll('div[class*=dialog-]');for(var k=0;k<L.length;k++){var g=L[k];if((g.textContent||'').indexOf(KEEP)>=0){var kt=g;while(kt.parentElement&&kt.parentElement!==document.body&&kt.parentElement.id!=='overlap-manager-root')kt=kt.parentElement;if(kt.id!=='overlap-manager-root'){kt.setAttribute('data-tvp-keep','1');if(kt.style.display==='none')kt.style.display='';var kd=kt.querySelectorAll('div[class*=dialog]');for(var q=0;q<kd.length;q++)kd[q].setAttribute('data-tvp-keep','1');}continue;}var hit=!!g.querySelector('[class*=gopro-]');if(!hit){var tx=g.innerText||'';for(var j=0;j<TXT.length;j++){if(tx.indexOf(TXT[j])>=0){hit=true;break;}}}if(!hit)continue;var top=g;while(top.parentElement&&top.parentElement!==document.body&&top.parentElement.id!=='overlap-manager-root')top=top.parentElement;if(top&&top!==document.body&&top.id!=='overlap-manager-root'&&top.style.display!=='none'){var cb=top.querySelector('[aria-label*=lose i],[aria-label*=ismiss i],button[class*=lose],[class*=loseButton],[data-name=close]');if(cb){try{cb.click();}catch(e){}}top.style.display='none';try{document.body.classList.remove('i-no-scroll');document.documentElement.classList.remove('i-no-scroll');}catch(e){}try{if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();}catch(e){}}}}catch(e){}};"
        b"try{new MutationObserver(function(){kill();}).observe(document.documentElement,{childList:true,subtree:true});}catch(e){}"
        b"var killAd=function(){try{var cb=document.querySelectorAll('[data-name=toast-group-close-button-charting-ad]');for(var k=0;k<cb.length;k++){try{cb[k].click();}catch(e){}}}catch(e){}};"
        b"setInterval(killAd,1000);killAd();"
        b"setInterval(kill,500);kill();"
        b"var __oWS=window.WebSocket;var __tv=[];var __tvN=0;var __tvMax=Date.now();"
        b"try{var __nWS=function(u,p){var ws=p===undefined?new __oWS(u):new __oWS(u,p);try{if(typeof u==='string'&&u.indexOf('/socket.io/websocket')>=0){var rec={ws:ws,last:Date.now(),closed:false};__tv.push(rec);if(__tv.length>8)__tv.shift();ws.addEventListener('message',function(){rec.last=Date.now();if(Date.now()>__tvMax)__tvMax=Date.now();});ws.addEventListener('close',function(){rec.closed=true;});__tvN++;}}catch(e){}return ws;};__nWS.prototype=__oWS.prototype;__nWS.CONNECTING=0;__nWS.OPEN=1;__nWS.CLOSING=2;__nWS.CLOSED=3;window.WebSocket=__nWS;}catch(e){}"
        b"var __tvPend=0,__tvPendN=0,__tvRlBusy=false,__tvRlTimer=0,__tvRlN=0;"
        b"setInterval(function(){try{var now=Date.now();var live=[],i,r;"
        b"for(i=0;i<__tv.length;i++){r=__tv[i];if(!r.closed&&(r.ws.readyState===0||r.ws.readyState===1))live.push(r);}"
        b"if(__tvRlTimer&&__tvN>__tvRlN){clearTimeout(__tvRlTimer);__tvRlTimer=0;__tvRlBusy=false;}"
        b"window.__tvpDbg={live:live.length,tvN:__tvN,maxAge:Math.round(now-__tvMax),rlB:__tvRlBusy};"
        b"if(__lim()){__tvPend=0;window.__tvpDbg.lim=true;if(__tvRlTimer){clearTimeout(__tvRlTimer);__tvRlTimer=0;__tvRlBusy=false;}__tvTake();return;}"
        b"if(live.length>0){for(i=0;i<live.length;i++){r=live[i];if(now-r.last>15000){try{r.ws.close();}catch(e){}r.closed=true;__tvPend=now;__tvPendN=__tvN;}}"
        b"if(__tvN>__tvPendN)__tvPend=0;"
        b"if(__tvPend&&__tvN===__tvPendN&&now-__tvPend>120000){__tvPend=0;__tvForceReload();}}"
        b"else if(now-__tvMax>120000){__tvForceReload();}"
        b"}catch(e){}},2000);"
        b"var __tvForceReload=function(){if(__tvRlBusy)return;__tvRlBusy=true;__tvRlN=__tvN;"
        b"var n=0;try{n=parseInt(sessionStorage.getItem('__tvp_rl')||'0',10)||0;}catch(e){}"
        b"if(n>=5){try{window.__tvpDbg&&(window.__tvpDbg.gaveUp=true);}catch(e){}return;}"
        b"try{sessionStorage.setItem('__tvp_rl',String(n+1));}catch(e){}"
        b"var d=[10000,30000,90000,300000,900000][n]||900000;"
        b"__tvRlTimer=setTimeout(function(){location.reload();},d);};"
        b"var __tvTake=function(){try{if(document.visibilityState!=='visible'||!__lim())return;for(var i=0;i<__tv.length;i++){var r=__tv[i];if(!r.closed&&r.ws.readyState===1&&Date.now()-r.last<15000)return;}var t=0;try{t=parseInt(sessionStorage.getItem('__tvp_take')||'0',10)||0;}catch(e){}if(Date.now()-t<30000)return;try{sessionStorage.setItem('__tvp_take',String(Date.now()));}catch(e){}location.reload();}catch(e){}};"
        b"document.addEventListener('visibilitychange',__tvTake);"
        b'}catch(e){}})()")'
        b"}catch(_e){}})()",
    ),
]


def build_patches(isolate: bool) -> dict[str, list[Patch]]:
    return {
        "index.js": list(INDEX_JS_COMMON) + (list(INDEX_JS_ISOLATION) if isolate else []),
        "app/new-tab/bundle.js": list(NEW_TAB_PATCHES),
        "preload/target-tvd-host-preload.js": list(PRELOAD_PATCHES),
    }


# Pristine index.js marker: proves the asar we are about to back up / patch
# has not been patched by a previous run (whose *.orig backup is gone).
PRISTINE_NEEDLE = b"M_(n,(()=>(0,xt.Dv)().getCurrentUser().isAuthenticated))"
PATCHED_NEEDLE = b"M_(n,(()=>!0))"

NAV_RULES_DROP_SUBSTRINGS = ("gopro/", "pricing/", "black-friday/")


def die(msg: str) -> "None":
    raise SystemExit(f"[patch-tradingview] ABORT: {msg}")


# --------------------------------------------------------------------------
# App lifecycle: is it running / quit it / relaunch it
# --------------------------------------------------------------------------
def read_info_plist(app: Path) -> dict:
    with (app / "Contents/Info.plist").open("rb") as fh:
        return plistlib.load(fh)


def app_is_running(app: Path, info: dict) -> bool:
    exe = info.get("CFBundleExecutable", app.stem)
    pat = f"{app}/Contents/MacOS/{exe}"
    return subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0


def quit_app(app: Path) -> None:
    """Gracefully quit a running instance; escalate to TERM/KILL if needed."""
    info = read_info_plist(app)
    if not app_is_running(app, info):
        print("[0] app not running")
        return

    print(f"[0] {app.name} is running -- quitting it first")
    bundle_id = info.get("CFBundleIdentifier")
    if bundle_id:
        subprocess.run(
            ["osascript", "-e", f'tell application id "{bundle_id}" to quit'],
            check=False,
            capture_output=True,
        )
    if not wait_gone(app, info, 15):
        print("  still running after graceful quit -- sending SIGTERM")
        subprocess.run(["pkill", "-TERM", "-f", f"{app}/Contents/MacOS/"], check=False)
        if not wait_gone(app, info, 8):
            print("  still running -- sending SIGKILL")
            subprocess.run(["pkill", "-KILL", "-f", f"{app}/Contents/MacOS/"], check=False)
            wait_gone(app, info, 5)


def wait_gone(app: Path, info: dict, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not app_is_running(app, info):
            print("  app quit")
            return True
        time.sleep(0.5)
    return not app_is_running(app, info)


def launch_app(app: Path) -> None:
    # plain `open` (no -n): normal single-instance launch, just like
    # clicking the icon in Launchpad / Dock.
    print(f"[6] launching {app.name}")
    subprocess.run(["open", str(app)], check=False)


# --------------------------------------------------------------------------
# ASAR reader/writer (pickle-layout aware, keeps 'unpacked' entries intact)
# --------------------------------------------------------------------------
def read_asar(path: Path):
    data = path.read_bytes()
    first, pickle_len = struct.unpack_from("<II", data, 0)
    if first != 4:
        die(f"unexpected asar magic at offset 0: {first}")
    payload_size, str_len = struct.unpack_from("<II", data, 8)
    json_start = 16
    json_bytes = data[json_start : json_start + str_len]
    header = json.loads(json_bytes.decode("utf-8"))
    data_base = 8 + pickle_len  # region where file contents start
    if data_base < json_start + str_len:
        die(f"inconsistent asar header: data_base={data_base} json end={json_start + str_len}")
    return header, data, data_base


def walk_files(node: dict):
    """Yield (key, entry) for every file leaf, preserving JSON key order."""
    for name, entry in node.get("files", {}).items():
        if "files" in entry:
            yield from walk_files(entry)
        else:
            yield name, entry


def write_asar(path: Path, header: dict, contents: dict[str, bytes]):
    # Rebuild header: recompute offset/size for each non-unpacked leaf.
    payload: dict[str, bytes] = {}
    offset = 0

    def fill(node: dict):
        nonlocal offset
        prefix = []

        def walk(sub: dict, path: str):
            nonlocal offset
            files = sub.get("files", {})
            for name, entry in files.items():
                logical = f"{path}/{name}" if path else name
                if "files" in entry:
                    walk(entry, logical)
                    continue
                if entry.get("unpacked"):
                    continue  # unchanged, lives in app.asar.unpacked
                blob = contents[logical]  # KeyError would mean a leaf we missed
                entry["offset"] = str(offset)
                entry["size"] = len(blob)
                payload[logical] = blob
                offset += len(blob)

        walk(node, "")

    fill(header)

    json_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    pad = (-len(json_bytes)) % 4
    inner = struct.pack("<II", 4 + len(json_bytes) + pad, len(json_bytes)) + json_bytes + b"\x00" * pad
    out = struct.pack("<II", 4, len(inner)) + inner + b"".join(payload.values())
    tmp = path.with_suffix(".asar.tmp")
    tmp.write_bytes(out)
    tmp.replace(path)


def collect_original(header: dict, data: bytes, data_base: int) -> dict[str, bytes]:
    """Snapshot every non-unpacked leaf keyed by logical path."""
    out: dict[str, bytes] = {}

    def walk(sub: dict, path: str):
        for name, entry in sub.get("files", {}).items():
            logical = f"{path}/{name}" if path else name
            if "files" in entry:
                walk(entry, logical)
                continue
            if entry.get("unpacked"):
                continue
            off, size = int(entry["offset"]), int(entry["size"])
            out[logical] = data[data_base + off : data_base + off + size]

    walk(header, "")
    return out


def load_file(header: dict, data: bytes, data_base: int, logical: str) -> bytes:
    node = header
    for part in logical.split("/"):
        node = node["files"][part]
    if node.get("unpacked"):
        die(f"{logical} is an unpacked entry; not supported for patching")
    off = int(node["offset"])
    size = int(node["size"])
    return data[data_base + off : data_base + off + size]


def apply_patch(logical: str, blob: bytes, patches: list[Patch]) -> bytes:
    for patch in patches:
        n = blob.count(patch.old)
        if n != 1:
            die(f"patch {patch.id} on {logical}: expected exactly 1 occurrence, found {n} "
                f"(pattern changed in this build?)")
        blob = blob.replace(patch.old, patch.new)
    return blob


def patch_nav_rules(blob: bytes) -> bytes:
    rules = json.loads(blob)
    removed = []
    for group in ("common-rules", "dialog", "dialog-unauth"):
        section = rules.get(group)
        if not isinstance(section, list):
            continue
        for rule in section:
            urls = rule.get("url")
            if not isinstance(urls, list):
                continue
            kept = []
            for u in urls:
                if any(s in u for s in NAV_RULES_DROP_SUBSTRINGS):
                    removed.append(u)
                else:
                    kept.append(u)
            rule["url"] = kept
        rules[group] = [r for r in section if r.get("url") != []]
    if not removed:
        die("nav-rules.json: nothing to remove (rule set changed?)")
    out = json.dumps(rules, indent="\t", ensure_ascii=False) + "\n"
    print(f"  p8 nav-rules: removed {len(removed)} promo/upsell external links {removed}")
    return out.encode("utf-8")


def patch_config(blob: bytes) -> bytes:
    cfg = json.loads(blob)
    try:
        before = cfg["sentry"]["upload"]
    except (KeyError, TypeError):
        die("config.json: sentry.upload missing (structure changed?)")
    if before is not True:
        die(f"config.json: sentry.upload already {before!r}")
    cfg["sentry"]["upload"] = False
    if "googleAnalytics" in cfg:
        ga = cfg["googleAnalytics"]
        ga.pop("measurementId", None)
        ga.pop("key", None)
    return (json.dumps(cfg, indent="\t", ensure_ascii=False) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# In-place backups: pristine *.orig snapshots make re-runs idempotent and
# give a one-file-per-artifact uninstall path.
# --------------------------------------------------------------------------
def prepare_in_place(app: Path) -> None:
    asar = app / "Contents/Resources/app.asar"
    asar_orig = asar.with_suffix(".asar.orig")
    plist = app / "Contents/Info.plist"
    plist_orig = plist.with_suffix(".plist.orig")

    if asar_orig.exists():
        print("  restoring pristine backups (*.orig)")
        shutil.copy2(asar_orig, asar)
        if plist_orig.exists():
            shutil.copy2(plist_orig, plist)
        return

    # First run: snapshot the current (must be pristine) bundle.
    data = asar.read_bytes()
    if PATCHED_NEEDLE in data or PRISTINE_NEEDLE not in data:
        die(f"{asar} looks already patched but no *.orig backup exists.\n"
            f"  Reinstall TradingView (or restore a pristine app.asar), then re-run.")
    if "ElectronAsarIntegrity" not in read_info_plist(app):
        die("Info.plist has no ElectronAsarIntegrity key (already patched, no backup?)")
    shutil.copy2(asar, asar_orig)
    shutil.copy2(plist, plist_orig)
    print(f"  backed up pristine app.asar / Info.plist -> *.orig")


def strip_integrity(app: Path) -> None:
    plist_path = app / "Contents/Info.plist"
    with plist_path.open("rb") as fh:
        info = plistlib.load(fh)
    if "ElectronAsarIntegrity" not in info:
        die("Info.plist has no ElectronAsarIntegrity key (fuse layout changed?)")
    del info["ElectronAsarIntegrity"]
    with plist_path.open("wb") as fh:
        plistlib.dump(info, fh, fmt=plistlib.FMT_XML)


def resign(app: Path) -> None:
    result = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        result.check_returncode()
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


# --------------------------------------------------------------------------
def patch_app(app: Path, isolate: bool) -> None:
    patches = build_patches(isolate)

    asar_path = app / "Contents/Resources/app.asar"
    header, data, data_base = read_asar(asar_path)

    print("[2] patching asar entries")
    contents = collect_original(header, data, data_base)
    for logical in list(patches) + ["configs/nav-rules.json", "configs/config.json"]:
        original = contents[logical]
        if logical in patches:
            ids = " ".join(p.id for p in patches[logical])
            contents[logical] = apply_patch(logical, original, patches[logical])
            print(f"  {logical}: {len(original)} -> {len(contents[logical])} bytes ({ids})")

    contents["configs/nav-rules.json"] = patch_nav_rules(contents["configs/nav-rules.json"])
    contents["configs/config.json"] = patch_config(contents["configs/config.json"])

    new_header = json.loads(json.dumps(header))  # deep copy keeps key order
    write_asar(asar_path, new_header, contents)

    print("[3] verify patched asar")
    vh, vd, vb = read_asar(asar_path)
    index_checks = [
        (b"M_(n,(()=>!0))", 1, True),
        (b"requestProFeature(){return void 0;", 1, True),
        (b"M_(n,(()=>(0,xt.Dv)().getCurrentUser().isAuthenticated))", 0, False),
        (b"showAppOnboardingDialog(e,t){}", 1, True),
        (b"async sendEvent(e){return void 0;", 1, True),
        (b'(0,xt.Dv)().doSignIn("first_sign")', 0, False),
        (b"return n.webPreferences.backgroundThrottling=!1,", 1, True),
        (b"(0,M.iT)()||r||(n.webPreferences.backgroundThrottling=!1)", 0, False),
        (b"disable-backgrounding-occluded-windows", 1, True),
    ]
    if isolate:
        index_checks += [
            (b'require("electron").app.setPath("userData"', 1, True),
            (b'"/TradingView-patched"),i.app.requestSingleInstanceLock()', 0, False),
            (b'getPath("userData")+"/logs"', 1, True),
        ]
    else:
        index_checks += [
            (b'require("electron").app.setPath("userData"', 0, False),
            (b'this.logsPath=W.l.makeAppDataPath("logs")', 1, True),
        ]
    patched_check = {
        "index.js": index_checks,
        "app/new-tab/bundle.js": [
            (b"j===YS&&M()", 1, True),
            (b"M===YS&&u()", 1, True),
            (b"onClick:()=>{u(),I()}", 0, True),
            (b"onClick:()=>{M()}", 1, True),
        ],
        "preload/target-tvd-host-preload.js": [
            (b"webFrame.executeJavaScript", 1, True),
            (b"createGoProDialog=function(){}", 2, True),
            (b"i-no-scroll", 2, True),
            (b"window.WebSocket=__nWS", 1, True),
            (b"__tvp_rl", 2, True),
        ],
    }
    for logical, checks in patched_check.items():
        blob = load_file(vh, vd, vb, logical)
        for needle, expected, want in checks:
            n = blob.count(needle)
            if want and n != expected:
                die(f"verify {logical}: {needle!r} count {n} != {expected}")
            if not want and n != 0:
                die(f"verify {logical}: leftover {needle!r} count {n}")
    # nav rules / config round-trip
    nr = load_file(vh, vd, vb, "configs/nav-rules.json")
    assert all(s not in nr.decode() for s in ("gopro/", "pricing/", "black-friday/"))
    cfg = json.loads(load_file(vh, vd, vb, "configs/config.json"))
    assert cfg["sentry"]["upload"] is False
    print("  verified: all patch needles present/absent as expected")


def main() -> None:
    argv = sys.argv[1:]

    def value_of(flag: str):
        return argv[argv.index(flag) + 1] if flag in argv else None

    src = Path(value_of("--src") or SRC_DEFAULT)
    dst_arg = value_of("--dst")
    no_restart = "--no-restart" in argv
    in_place = dst_arg is None

    if not (src / "Contents/Resources/app.asar").exists():
        die(f"no app.asar under {src}")

    if in_place:
        target = src
        print(f"[1] in-place mode: {src}")
        if not no_restart:
            quit_app(src)
        prepare_in_place(src)
    else:
        dst = Path(dst_arg)
        print(f"[1] copy mode: {src} -> {dst}")
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True)
        subprocess.run(["xattr", "-cr", str(dst)], check=False, capture_output=True)
        # If the source bundle is already patched in place, fall back to its
        # pristine *.orig backups so the patch table can match again.
        asar = dst / "Contents/Resources/app.asar"
        asar_orig = src / "Contents/Resources/app.asar.orig"
        if asar_orig.exists() and PATCHED_NEEDLE in asar.read_bytes():
            print("  source is already patched -- using its pristine *.orig backups")
            shutil.copy2(asar_orig, asar)
            plist_orig = src / "Contents/Info.plist.orig"
            if plist_orig.exists():
                shutil.copy2(plist_orig, dst / "Contents/Info.plist")
        target = dst

    patch_app(target, isolate=not in_place)

    print("[4] drop ElectronAsarIntegrity from Info.plist")
    strip_integrity(target)

    print("[5] ad-hoc re-sign")
    resign(target)

    if in_place:
        print(f"\nDONE: {src} patched in place (pristine backups: *.orig inside the bundle)")
    else:
        print(f"\nDONE: {dst}")
        print("Run it with: open -n", dst)

    if in_place and not no_restart:
        launch_app(src)


if __name__ == "__main__":
    main()
