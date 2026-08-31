# Multi-account Runtime Operations

This package is still the upstream `wechat-selkies` image. The multi-account
changes add account-aware WeChat process control without replacing Selkies,
WebRTC, Openbox, GPU support, `/config`, or the official Linux WeChat package.

## Compatibility mode

With no `WECHAT_ACCOUNTS` value, startup creates one registry entry:

```text
account_id: default
unix user:  abc
HOME:       /config
DISPLAY:    inherited Selkies DISPLAY (normally :1)
```

This intentionally preserves the upstream single-account login/data path.

## Multi-account mode

Example Compose environment:

```yaml
environment:
  - WECHAT_ACCOUNTS=default,work,personal
  - WECHAT_DEFAULT_ACCOUNT_ID=default
  - WECHAT_ACCOUNT_UID_BASE=20000
```

`default` can remain the legacy `abc + /config` account. Other accounts get a
dedicated Unix user and persistent home:

```text
/config/wechat-accounts/work/home
/config/wechat-accounts/personal/home
```

Each dedicated account receives its own `HOME`, `XDG_CONFIG_HOME`,
`XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR`, Unix UID and PID set.
Dedicated accounts do not reuse the `abc` desktop session D-Bus; when available,
the runtime launches WeChat under a per-account `dbus-run-session`. Device group
inheritance is limited to `audio`, `input`, `plugdev`, `render` and `video` when
the Selkies `abc` user belongs to those groups, so GPU/audio access can be kept
without granting account users privileged groups such as `sudo` or `docker`.

The persistent registry is:

```text
/config/wechat-runtime/accounts.json
```

The registry is generated once. Later environment changes do not silently
rewrite an existing registry; use `register`/`unregister` or deliberately edit
the persisted registry while all WeChat processes are stopped.

## Runtime commands

Inside the container:

```bash
/scripts/wechat/wechat-runtime list --json
/scripts/wechat/wechat-runtime health --json
/scripts/wechat/wechat-runtime status default --json
/scripts/wechat/wechat-runtime status work --json

/scripts/wechat/wechat-runtime register work --display :1 --json
/scripts/wechat/wechat-runtime start work --json
/scripts/wechat/wechat-runtime restart work --json
/scripts/wechat/wechat-runtime stop work --json
/scripts/wechat/wechat-runtime unregister work --json
```

If a WeChat client was minimized, `start <account_id>` is also the restore
command. It activates that account's `Weixin` window through the shared-DISPLAY
lock instead of launching a duplicate process; `start --json` then reports
`action: restored`.

`unregister` intentionally preserves both the Unix user and its data directory.
That prevents an operator typo from deleting a logged-in profile.

`status --json` exposes the runtime handoff needed by Core:

```text
account_id
username / uid
home
display
running / pids
windows[]: window_id / pid / title
display_lock
```

## Shared-DISPLAY input lock

Separate HOME/XDG/UID state does not isolate X11 focus, clipboard or synthetic
input. Any Core sender that uses clipboard or xdotool-style automation must run
the display-global section through:

```bash
/scripts/wechat/wechat-display-lock.sh work <command> [args...]
```

All accounts on the same DISPLAY resolve to the same `flock` file.

The bundled auto-login helper also takes that lock and refuses to click a
window unless its X11 PID belongs to the current account Unix UID and the
window title/class looks like WeChat.

## Root bootstrap

`/custom-cont-init.d/50-wechat-account-bootstrap` creates dedicated Unix users
and writable account directories as root. `/scripts/start.sh` calls bootstrap
again before autostart as a race-safe guard; the wrapper uses LinuxServer's
passwordless sudo when the desktop session runs as `abc`.

If a hardened Selkies configuration disables sudo, explicit multi-account
control must be invoked by a root-side supervisor/API. For the implicit legacy
single account, startup retains a direct `/usr/bin/wechat` fallback.

## Separate DISPLAY fallback

`WECHAT_ACCOUNT_DISPLAY_MAP` can persist a different display value per account,
for example:

```text
WECHAT_ACCOUNT_DISPLAY_MAP=work=:1,test=:2
```

This does **not** create an extra X server. Per the project taskbook, topology B
(separate displays) is not permitted as the default design until a real Linux
test proves topology A (same display + different Unix users/HOME/XDG) fails.

## Gate-0 POC

Run the same-display process proof inside a real built Linux container:

```bash
/scripts/wechat/poc_same_display.sh gate0-a gate0-b
```

The script proves two official WeChat process sets use different UIDs/HOMEs and
the same DISPLAY. The final Gate-0 proof still requires opening Selkies, logging
both real clients in, and recording the status/window evidence.
