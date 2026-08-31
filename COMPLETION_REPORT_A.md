# Completion Report A - Multi-account Runtime

Date: 2026-08-31 (Asia/Shanghai)

Branch: `feat/multi-account-runtime`

Gate result: **Gate 0 PASS on Unraid.**

## Upstream used

```text
https://github.com/nickrunning/wechat-selkies.git
@ b3b5341a26b803e06a1a7daaf420151297da4e79
```

The derivative checkout retains the real `upstream` remote and upstream
`LICENSE`.

## Reused code

| Upstream source | Reused behavior | New/modified location | Adaptation |
|---|---|---|---|
| `Dockerfile` | LinuxServer Selkies base, official WeChat AMD64/ARM64 installation, GPU-capable base, existing ENV | `Dockerfile` | Kept intact as the image baseline; only added runtime tools/env/bootstrap copy and Windows-CRLF hardening. |
| `docker-compose.yml` | `/config`, `/dev/dri`, 3000/3001, PUID/PGID, credentials, shm and restart policy | `docker-compose.yml` | Preserved; added optional account registry variables. |
| `.env.example` | Existing user-facing runtime variable template | `.env.example` | Preserved existing values and documented the optional multi-account variables. |
| `root/defaults/autostart` | Selkies desktop entry delegates to `/scripts/start.sh` | unchanged | Still the desktop entry path. |
| `root/scripts/start.sh` | Openbox config, menu refresh watcher, stalonetray, QQ launch, nightly daemon | same file | Only the WeChat single-process launch block is replaced by registry bootstrap/start-all. |
| `root/scripts/wechat/wechat-start.sh` | Existing manual/menu start entry | same file | Now a thin account-aware wrapper instead of direct global launch. |
| `root/scripts/wechat/wechat-stop.sh` | Existing stop entry and graceful-stop intent | same file | Delegates to account-scoped SIGTERM/SIGKILL logic. |
| `root/scripts/wechat/wechat-restart.sh` | Existing restart entry and auto-login behavior | same file | Delegates to account-scoped restart; auto-login is launched per account. |
| `root/scripts/wechat/wechat-nightly-schedule.sh` | Existing timing/schedule loop and ENV controls | same file | Stops/starts registered autostart accounts rather than all WeChat processes globally. |
| `root/scripts/wechat/wechat-auto-login.py` | Existing login-button feature heuristic and xdotool flow | same file | Restricts candidate windows to current account UID, crops the verified window and serializes UI automation by DISPLAY. |
| `root/scripts/window_switcher.py` | Xlib `_NET_CLIENT_LIST` discovery approach | `root/scripts/wechat/wechat_runtime.py` | Reused the X11 discovery concept for machine-readable window registry; deprecated GUI itself is not revived. |

## New code

### `root/scripts/wechat/wechat_runtime.py`

Added because upstream has no multi-account process model. It implements:

- persistent `/config/wechat-runtime/accounts.json` registry;
- safe account IDs and deterministic Unix usernames;
- legacy `default -> abc + /config` compatibility;
- dedicated Unix UID/HOME/XDG trees for additional accounts;
- root bootstrap/user creation;
- account-scoped WeChat process discovery and signal control;
- `start/stop/restart/start-all/stop-all/restart-all`;
- `list/status/health/display/window` machine-readable operations;
- account `register/unregister` without destructive data deletion;
- `account_id -> uid/pids/window/display/home` status;
- Xlib `_NET_WM_PID` window correlation;
- per-account D-Bus session launch for dedicated Unix users when
  `dbus-run-session` is available;
- safe inheritance of only Selkies device-oriented groups
  (`audio/input/plugdev/render/video`), never blanket cloning privileged groups.

### `root/custom-cont-init.d/50-wechat-account-bootstrap`

Added because dynamic Unix users must be created as root before desktop-owned
WeChat processes launch. `start.sh` repeats bootstrap through the runtime wrapper
as a race-safe guard.

### `root/scripts/wechat/wechat-display-lock.sh`

Added because X11 focus, clipboard and synthetic input remain global when
several isolated WeChat accounts share the same DISPLAY. Future Core sender UI
operations can serialize their critical section with this helper.

### Tests and runbooks

- `tests/test_wechat_runtime.py`
- `tests/poc_same_display.sh`
- image path: `/scripts/wechat/poc_same_display.sh`
- `docs/MULTI_ACCOUNT_RUNTIME.md`
- `docs/GATE0_POC_RESULT.md`
- `.gitattributes` plus Docker build CRLF normalization for Windows checkouts.

## Not reused

| Upstream behavior/code | Reason |
|---|---|
| Global `pgrep/pkill -f /usr/bin/wechat` | One account operation would stop every WeChat account. No global process kill remains in the WeChat control scripts. |
| Deprecated `window_switcher.py` GUI | The project needs machine-readable account/window state, not another desktop widget. Its Xlib discovery approach was retained as reference. |
| Original first-large-window auto-login selection | Unsafe on a shared DISPLAY because it could click a different account. |
| QQ process-control rewrite | Outside package A scope; existing QQ behavior is preserved. |
| Separate-display runtime | Taskbook forbids choosing topology B until a real topology-A failure is proven. The registry can store display overrides, but A does not fabricate extra X servers. |

## Compatibility preserved

- Selkies Web UI/WebRTC remains the base runtime.
- Official WeChat install/update source remains unchanged.
- `/config` remains persistent.
- `/dev/dri` GPU mapping remains unchanged.
- ports 3000/3001 remain unchanged.
- PUID/PGID, CUSTOM_USER, PASSWORD and existing WeChat/QQ ENV controls remain.
- no `WECHAT_ACCOUNTS` setting keeps the original single-account `abc` user and
  `/config` HOME, avoiding loss of existing login/profile data.

## Validation performed on current host

Passed:

```text
python -m py_compile root/scripts/wechat/wechat_runtime.py \
  root/scripts/wechat/wechat-auto-login.py tests/test_wechat_runtime.py

python -m unittest discover -s tests -v
# 6 tests passed

# Bash syntax after CRLF normalization (the Dockerfile performs this same normalization)
bash -n ...

PyYAML parse: docker-compose.yml
wechat_runtime.py --help
global pkill/pgrep residue scan: none in active WeChat control code
```

Blocked:

```text
docker --version
-> 'docker' is not recognized as an internal or external command
```

The Windows host therefore could not run the real test locally.

## Unraid Gate 0 validation

The isolated launcher was run on Unraid with project `wechat-hub-a-gate0`
and container `wechat-hub-a-gate0-runtime`. Docker `27.5.1`, Compose
`v2.40.3`, and `/dev/dri` were available. The process-level Gate 0 proof
passed with two real official WeChat clients (`4.1.1.8`) in the same container
and on the same DISPLAY:

```text
gate0-a: uid=20000, home=/config/wechat-accounts/gate0-a/home, pid=521
gate0-b: uid=20001, home=/config/wechat-accounts/gate0-b/home, pid=528
both accounts: display=:1
health: healthy=true
```

Window discovery found one distinct `Weixin` window per account before login:

```text
gate0-a: window_id=0xC00006
gate0-b: window_id=0xE00006
```

The operator then logged both official clients in through the Selkies UI. The
post-login `health --json` read still reported `healthy: true`, with separate
UID/HOME trees and live windows:

```text
gate0-a: uid=20000, window_id=0xC00037, windows=1
gate0-b: uid=20001, window_id=0xE00037/0xE0003D, windows=2
both accounts: display=:1
```

Full evidence and the official package hash are recorded in
`docs/GATE0_POC_RESULT.md`. The isolated container remains running at ports
`13000/13001`.

## Gate-0 reproduction

On a Linux Docker host:

```bash
docker compose build
docker compose up -d
docker exec -it wechat-selkies /scripts/wechat/poc_same_display.sh gate0-a gate0-b
docker exec -it wechat-selkies /scripts/wechat/wechat-runtime health --json
```

Then open the Selkies Web UI, log both real official WeChat clients in, and
record the two distinct UIDs/HOMEs plus shared DISPLAY, PIDs and window IDs in
`docs/GATE0_POC_RESULT.md`. This step has now been completed for the current
Unraid proof.

Do not test separate displays unless this real same-DISPLAY topology fails for a
documented technical reason.

## Runtime/Core integration handoff

The frozen project contract states that Runtime process control is internal to
Runtime/Core integration. Package A exposes that internal boundary as JSON-capable
commands rather than changing Core API V1:

```bash
/scripts/wechat/wechat-runtime list --json
/scripts/wechat/wechat-runtime status <account_id> --json
/scripts/wechat/wechat-runtime health --json
/scripts/wechat/wechat-runtime start|stop|restart <account_id> --json
/scripts/wechat/wechat-display-lock.sh <account_id> <ui-automation-command...>
```

Core/integration work should treat `account_id` from the registry as stable and
must never fall back to global `pkill` or display-global UI input without the
DISPLAY lock.
