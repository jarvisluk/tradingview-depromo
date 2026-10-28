# tradingview-depromo

A patcher for **TradingView Desktop** (macOS, Electron) that suppresses the
promo / upsell / paywall *prompts* sprinkled through the desktop shell and
its embedded web tabs.

By default it patches the **installed app in place**:

1. quits `/Applications/TradingView.app` if it is running
2. patches it directly (idempotent: pristine `*.orig` backups inside the
   bundle are restored before every run)
3. relaunches it when done

Because it works in place, the patched client is indistinguishable from the
stock one everywhere that matters: same path, same name/icon, same bundle id,
and — since the userData-isolation patches are skipped in this mode — the same
`~/Library/Application Support/TradingView` data (settings, layouts, login
session). The only tell is the ad-hoc code signature, which macOS requires
after a bundle is modified.

> **Disclaimer** — This project modifies the TradingView Desktop client, which
> likely violates the [TradingView Terms of Service](https://www.tradingview.com/terms/).
> It is provided for educational purposes, with no warranty. Use at your own
> risk — if TradingView ever detects a modified client, the worst realistic
> outcome is account action. Not affiliated with or endorsed by TradingView.
>
> **What this does NOT do**: no server-side limit is bypassed. Layout count
> limits, plan quotas, indicator/alert entitlements — all enforced by
> TradingView's servers and fully honored. The patches only silence the
> client-side promotional dialogs wrapped around them.

## What gets patched

Every patch must match **exactly once** in the target build; the script
aborts loudly if a pattern drifts (i.e. after an app update).

`index.js` (main process):

| # | Patch | Mode |
|---|-------|------|
| p1 | feature-requester predicate → always allow (no `createGoProDialog` injection on tab detach / multi-window) | both |
| p1b | `requestProFeature()` → no-op | both |
| p2 | `showAppOnboardingDialog()` → no-op (welcome screen gone) | both |
| p3 | `TrackEventService.sendEvent()` → immediate return (shell analytics events) | both |
| p16 | web-tab windows always get `backgroundThrottling:false` (stock code gates it on a server-side feature flag) + `disable-backgrounding-occluded-windows` switch | both |
| p11 | first-run auto sign-in prompt bypassed | both |
| p10 | isolated `userData` → `~/Library/Application Support/TradingView-patched` | copy only |
| p12 | file logger follows the isolated `userData` dir | copy only |

p10/p12 are skipped in the default in-place mode on purpose: keeping the
stock `userData` is what preserves your settings, layouts and login session,
and is part of looking like the original client.

`app/new-tab/bundle.js`:

| # | Patch |
|---|-------|
| p4 | `createLayout` rejected as `count_limit` → close modal, no GoPro upsell |
| p5 | `copyLayout` rejected as `count_limit` → close modal, no GoPro upsell |
| p6 | /chart context-menu "Open in new window" → really opens a new window instead of the "Sign up for free" modal |
| p7 | "Create new layout" button no longer demands sign-in |

`configs/nav-rules.json`:

| # | Patch |
|---|-------|
| p8 | `gopro/` / `pricing/` / `black-friday/` entries removed from the open-external rule |

`configs/config.json`:

| # | Patch |
|---|-------|
| p9 | `sentry.upload = false` (no crash/attachment upload), GA keys stripped |

`preload/target-tvd-host-preload.js` (injected into web tabs via
`webFrame.executeJavaScript`, since web tabs run `contextIsolation:true`):

| # | Patch | Mode |
|---|-------|------|
| p13 | stub `window.TradingView.createGoProDialog` in the page's main world ("Analyze markets in TradingView Desktop" / `manyDesktopWindows` upsell) | both |
| p14 | DOM-level killer for site-internal paywall/gopro dialogs ("Join for free" gate and the signed-in desktop upsell — both render via `openWall`), un-locks body scroll | both |
| p15 | hide the "Invite friends. Earn cash" bottom-left banner (best-effort, matched by copy) | both |
| p18 | streamer watchdog: `close()` a data socket silent for 45s so the site reconnects; rate-limited reload fallback | both |
| p18 | streamer watchdog: hook `WebSocket`, track last inbound frame on data-streamer sockets, `close()` a socket that has been silent for 45s (healthy ping cadence is 5-10s) so the site's own reconnect logic rebuilds the stream; rate-limited `location.reload()` fallback if the site fails to reconnect within 90s |

## Free-plan data stalls (and what p18 does about it)

TradingView's servers silently cut free/anonymous data streams after a few
minutes of a session's life — observed ~5 minutes after page load, signed-in
or not, foreground or background. The cut is one-sided: the WebSocket stays
ESTABLISHED, the server just stops sending everything (including its own
app-level pings), no error is raised, and the site never notices — the chart
simply freezes. Reloading the page always restarts the stream (for another
few minutes).

Since the patches above also hide the upsell dialogs that normally appear
around these nudge moments, a freeze would look completely silent. p18
restores continuity without reloading: the watchdog closes the starved
socket, the site reconnects on its own, and data resumes within seconds —
repeating for as long as the session lives.

Related hardening: the desktop shell only disables Chromium background
throttling for web-tab windows when a server-side feature flag
(`tvd_background_throttling`) says so; p16 forces it off unconditionally and
adds the `disable-backgrounding-occluded-windows` switch, so backgrounded or
fully covered windows keep their timers and render loop alive.

## Requirements

- macOS (the script re-signs the bundle with `codesign`, so it is Mac-specific)
- Python 3
- TradingView Desktop installed in `/Applications` (or pass `--src`)

## Usage

```bash
# double-click this in Finder, or run it from a terminal:
scripts/patch-tradingview.command

# same thing, spelled out:
python3 scripts/patch-tradingview.py                     # in-place on /Applications/TradingView.app
python3 scripts/patch-tradingview.py --no-restart        # patch but don't quit/relaunch
python3 scripts/patch-tradingview.py --src /path/to/TradingView.app
python3 scripts/patch-tradingview.py --dst /path/to/out.app   # legacy: patch a copy instead
```

Double-clicking works because the wrapper is a `.command` file — Finder runs
those in Terminal directly (a bare `.sh` usually just opens in a text editor).

The script is safe to re-run at any time. It restores the pristine
`app.asar.orig` / `Info.plist.orig` snapshots stored inside the bundle before
each run, so re-patching never stacks patches. **Uninstall** = delete those
two `.orig` files after restoring them (or just reinstall the app).

Notes:

- A TradingView **auto-update overwrites the patch**; just run the script
  again after an update (the patch patterns abort loudly if the build changed).
- Because the bundle signature changes to ad-hoc, macOS may ask **once** to
  re-grant keychain access (the app stores credentials via keytar).
- In copy mode (`--dst`) the patched copy uses a separate `userData` dir and
  single-instance lock, so it can run side-by-side with the original.

## How it works

1. (in-place) quits the running app — graceful `osascript` quit first,
   `SIGTERM`/`SIGKILL` as fallback — and snapshots/restores the pristine
   `*.orig` backups
2. Rewrites `Contents/Resources/app.asar` in place (pickle-layout aware,
   keeps `unpacked` entries intact) applying the patches above
3. Verifies every patched needle is present/absent as expected
4. Removes the `ElectronAsarIntegrity` declaration from `Info.plist`
5. Ad-hoc re-signs the bundle so macOS will launch it
6. (in-place) relaunches the app with a plain `open`

## Repository layout

```
scripts/patch-tradingview.py       # the patcher
scripts/patch-tradingview.command  # double-click wrapper
```

Build output and analysis artifacts land in `targets/` (git-ignored, never
published — it holds TradingView's proprietary binaries).

## License

[MIT](LICENSE)
