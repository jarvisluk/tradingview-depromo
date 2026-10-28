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
    #      class (or the known upsell copy), un-lock body scroll.
    # p15: hide the "Invite friends. Earn cash" bottom-left banner
    #      (best-effort, matched by copy).
    Patch(
        "p13-15",
        b"localStorage.setItem(s,process.env[s]??c)})()",
        b"localStorage.setItem(s,process.env[s]??c);"
        b'try{e.webFrame.executeJavaScript('
        b'"(function(){try{'
        b"var s=function(v){try{if(v&&typeof v==='object'&&typeof v.createGoProDialog==='function'){v.createGoProDialog=function(){};}}catch(e){}return v;};"
        b"var d=null;try{d=Object.getOwnPropertyDescriptor(window,'TradingView');}catch(e){}"
        b"if(!d){var t;try{t=s(window.TradingView);}catch(e){}"
        b"try{Object.defineProperty(window,'TradingView',{configurable:true,get:function(){return t;},set:function(v){t=s(v);}});}catch(e){}}"
        b"var i=setInterval(function(){try{var v=window.TradingView;if(v&&typeof v.createGoProDialog==='function'&&!v.createGoProDialog.__tvp){v.createGoProDialog=function(){};v.createGoProDialog.__tvp=1;}}catch(e){}},500);"
        b"var TXT=['Join for free','Make every move count','Look first / Then leap','Analyze markets in TradingView Desktop','Upgrade your plan and discover the full power'];"
        b"var kill=function(){try{var L=document.querySelectorAll('div[class*=dialog-]');for(var k=0;k<L.length;k++){var g=L[k];var hit=!!g.querySelector('[class*=gopro-]');if(!hit){var tx=g.innerText||'';for(var j=0;j<TXT.length;j++){if(tx.indexOf(TXT[j])>=0){hit=true;break;}}}if(!hit)continue;var top=g;while(top.parentElement&&top.parentElement!==document.body)top=top.parentElement;if(top&&top!==document.body&&top.style.display!=='none'){top.style.display='none';try{document.body.classList.remove('i-no-scroll');document.documentElement.classList.remove('i-no-scroll');}catch(e){}try{if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();}catch(e){}}}}catch(e){}};"
        b"try{new MutationObserver(function(){setTimeout(kill,30);}).observe(document.documentElement,{childList:true,subtree:true});}catch(e){}"
        b"var killAd=function(){try{var cands=document.querySelectorAll('button,a');for(var k=0;k<cands.length;k++){if((cands[k].textContent||'').indexOf('Get my link')<0)continue;var top=cands[k];while(top.parentElement&&top.parentElement!==document.body&&top.parentElement!==document.documentElement)top=top.parentElement;if(top&&top!==document.body&&top.tagName!=='HTML'&&top.tagName!=='BODY'&&top.style.display!=='none'){top.style.display='none';}}}catch(e){}};"
        b"setInterval(killAd,2000);"
        b"setInterval(kill,1000);kill();"
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
