# tradingview-depromo

A one-shot patcher for **TradingView Desktop** (macOS, Electron) that
suppresses the promo / upsell / paywall *prompts* sprinkled through the
desktop shell and its embedded web tabs.

It patches a **copy** of the app — your original `/Applications/TradingView.app`
is never touched, and the patched copy runs side-by-side with it
(separate `userData`, separate single-instance lock).

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

| # | Patch |
|---|-------|
| p1 | feature-requester predicate → always allow (no `createGoProDialog` injection on tab detach / multi-window) |
| p1b | `requestProFeature()` → no-op |
| p2 | `showAppOnboardingDialog()` → no-op (welcome screen gone) |
| p3 | `TrackEventService.sendEvent()` → immediate return (shell analytics events) |
| p10 | isolated `userData` → `~/Library/Application Support/TradingView-patched` |
| p11 | first-run auto sign-in prompt bypassed |
| p12 | file logger follows the isolated `userData` dir |

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

| # | Patch |
|---|-------|
| p13 | stub `window.TradingView.createGoProDialog` in the page's main world ("Analyze markets in TradingView Desktop" / `manyDesktopWindows` upsell) |
| p14 | DOM-level killer for site-internal paywall/gopro dialogs ("Join for free" gate and the signed-in desktop upsell — both render via `openWall`), un-locks body scroll |
| p15 | hide the "Invite friends. Earn cash" bottom-left banner (best-effort, matched by copy) |

## Requirements

- macOS (the script re-signs the bundle with `codesign`, so it is Mac-specific)
- Python 3
- TradingView Desktop installed in `/Applications` (or pass `--src`)

## Usage

```bash
python3 scripts/patch-tradingview.py            # default: /Applications/TradingView.app -> ./targets/TradingView-patched.app
python3 scripts/patch-tradingview.py --src /path/to/TradingView.app --dst /path/to/out.app
```

Then launch:

```bash
open -n targets/TradingView-patched.app
```

Each run starts from a fresh copy of the source app, so it is safe to re-run
after a TradingView update — as long as the bundled JS still matches the patch
patterns. On mismatch the script tells you which pattern drifted.

## How it works

1. Copies the app bundle to `targets/TradingView-patched.app`
2. Rewrites `Contents/Resources/app.asar` in place (pickle-layout aware,
   keeps `unpacked` entries intact) applying the patches above
3. Verifies every patched needle is present/absent as expected
4. Removes the `ElectronAsarIntegrity` declaration from `Info.plist`
5. Ad-hoc re-signs the bundle so macOS will launch it

## Repository layout

```
scripts/patch-tradingview.py   # the patcher
targets/                       # local build output & analysis artifacts (git-ignored)
```

## License

[MIT](LICENSE)
