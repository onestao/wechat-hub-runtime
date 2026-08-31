# SOURCE_AUDIT_A - Multi-account WeChat Runtime

Audit date: 2026-08-31 (Asia/Shanghai)

## 1. Repositories actually read

- Primary implementation baseline: `wechat-selkies`
  - local upstream reference: `../../upstream/wechat-selkies`
  - implementation checkout: this repository (`work/runtime`)
  - branch: `feat/multi-account-runtime`
- Project-level handoff documents read before implementation:
  - `../../docs/Linux_WeChat_Hub_Source_Based_Execution_Taskbook_V3.0.md`
  - `../../docs/UPSTREAM_LOCK.md`
  - `../../docs/SOURCE_MAP.md`
  - `../../docs/INTERFACE_CONTRACT_V1.md`
  - `../../docs/WORK_PACKAGE_HANDOFFS.md`
  - `../../docs/INTEGRATION_STATUS.md`

No alternate runtime was created from scratch. This package remains a derivative of the actual `wechat-selkies` source tree.

## 2. Upstream commit

`wechat-selkies` is locked to:

```text
b3b5341a26b803e06a1a7daaf420151297da4e79
```

Remote retained by this checkout:

```text
upstream https://github.com/nickrunning/wechat-selkies.git
```

The local `source-cache` remote points at `../../upstream/wechat-selkies`.

## 3. Source files actually read

| File | What was inspected |
|---|---|
| `AGENTS.md` | Repository conventions, Selkies/Openbox startup model, `/config`, process-control conventions and anti-patterns. |
| `Dockerfile` | Selkies base image, official WeChat installation, AMD64/ARM64 handling, GPU-preserving base, Python Xlib dependency and current environment variables. |
| `docker-compose.yml` | `/config` persistence, `/dev/dri`, ports 3000/3001, PUID/PGID, Selkies credentials, `shm_size` and existing WeChat schedule/login variables. |
| `.env.example` | User-facing environment template for ports, PUID/PGID, Selkies credentials, schedule and auto-login controls. |
| `root/defaults/autostart` | Selkies desktop autostart delegates to `/scripts/start.sh`. |
| `root/defaults/menu.xml` | Current Openbox WeChat action calls the single-process `wechat-start.sh`. |
| `root/scripts/start.sh` | Openbox setup, dynamic menu watcher, stalonetray, current single WeChat launch, auto-login, QQ launch and nightly scheduler. |
| `root/scripts/wechat/wechat-start.sh` | Direct single `/usr/bin/wechat` launch. |
| `root/scripts/wechat/wechat-stop.sh` | Global `pgrep/pkill -f /usr/bin/wechat`; unsuitable for multi-account isolation. |
| `root/scripts/wechat/wechat-restart.sh` | Global SIGKILL plus one process restart and one auto-login helper. |
| `root/scripts/wechat/wechat-nightly-schedule.sh` | Global nightly stop/start behavior that currently affects every WeChat process. |
| `root/scripts/wechat/wechat-auto-login.py` | DISPLAY-scoped `xdotool`/screen automation; currently chooses the first large visible window and therefore is unsafe with multiple WeChat windows. |
| `root/scripts/window_switcher.py` | Existing Xlib `_NET_CLIENT_LIST` enumeration and window activation code; useful as the window-registry reference even though the UI itself is deprecated. |

## 4. Code/design that can be reused directly

- `Dockerfile` base image and official WeChat package installation remain the runtime image baseline.
- Existing Selkies/WebRTC/X11/Openbox behavior remains owned by the LinuxServer Selkies base image; this package must layer multi-account process control on top rather than replace it.
- `/config` remains the persistence root.
- `root/defaults/autostart -> /scripts/start.sh` remains the desktop-session entry path.
- The Openbox initialization, menu refresh watcher, `stalonetray`, QQ startup and dynamic `.desktop` menu behavior in `root/scripts/start.sh` can be retained.
- Xlib `_NET_CLIENT_LIST` enumeration in `window_switcher.py` is a valid reference for a non-UI account/window registry.
- Existing environment variables such as `AUTO_START_WECHAT`, `AUTO_START_QQ`, `PUID`, `PGID`, schedule controls and auto-login controls remain compatibility inputs.

## 5. Code/design that must be modified

- `root/scripts/start.sh`
  - replace direct single WeChat launch with registry-driven account startup;
  - keep legacy single-account behavior when no explicit account registry is configured.
- `root/scripts/wechat/wechat-start.sh`, `wechat-stop.sh`, `wechat-restart.sh`
  - replace global process matching with account-specific operations;
  - accept an account id while preserving a sensible default account for the old Openbox menu.
- `root/scripts/wechat/wechat-nightly-schedule.sh`
  - stop/start registered enabled accounts instead of globally killing `/usr/bin/wechat`.
- `root/scripts/wechat/wechat-auto-login.py`
  - make automation account/window aware or fail safely when a unique account window cannot be identified;
  - serialize display-global UI automation when accounts share one DISPLAY.
- `root/defaults/menu.xml`
  - retain the old WeChat entry while routing it through the account-aware control path.
- `Dockerfile`
  - add only runtime dependencies required for account bootstrap/control; do not replace the Selkies base or WeChat installation path.
- `docker-compose.yml`
  - expose optional multi-account registry settings without removing existing settings.
- `.env.example`
  - document the new optional account registry variables alongside the upstream settings.

## 6. New functionality that is required

The upstream repository has no account registry or account-scoped process model, so the following must be added:

- persistent account runtime registry under `/config`;
- safe account-id validation;
- one Unix user identity per registered WeChat account;
- account-specific `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` and `XDG_RUNTIME_DIR`;
- account-specific PID files and process ownership validation;
- account `start`, `stop`, `restart`, `status`, `list` and bootstrap operations;
- `account_id -> pid/user/uid/display/home/window` status data;
- X11 window discovery using `_NET_CLIENT_LIST` + `_NET_WM_PID`, correlated to account process ownership;
- per-DISPLAY `flock` helper for future clipboard/xdotool sender operations when multiple accounts share a display;
- a root-time bootstrap hook that creates account Unix users and writable persistent account directories before the desktop launches accounts;
- a POC harness/documentation for the mandated order: first same container + same DISPLAY + different Unix users/HOME/XDG, and only then separate displays if the first topology is proven impossible.

## 7. Upstream code deliberately not reused

- Global `pkill`/`pgrep -f /usr/bin/wechat` process control is not reusable because one account operation would affect all accounts.
- The deprecated `window_switcher.py` GUI is not reused as the registry itself. Only its Xlib discovery approach is used as a reference; the new registry must emit machine-readable account-scoped state.
- The current `wechat-auto-login.py` first-large-window selection is not safe to reuse unchanged on a shared display because it can activate/click another account's login window.
- QQ process scripts are intentionally not generalized in package A; this work package is scoped to WeChat multi-account runtime and must avoid unrelated rewrites.

## 8. Test entry points

Planned/required tests after implementation:

```text
python -m unittest discover -s tests -v
bash -n root/scripts/start.sh root/scripts/wechat/*.sh
python -m py_compile root/scripts/wechat/*.py
```

Linux/container validation when Docker is available:

```text
docker compose config
docker compose build
docker compose up -d
docker exec wechat-selkies /scripts/wechat/wechat-runtime status --json
```

Gate-0 POC validation must additionally prove two real official WeChat processes under distinct Unix UIDs in one container, first on the same DISPLAY.

## 9. Risks and blockers

1. The current development host is Windows and has no Docker executable according to Session 0 status. Real Linux UID/X11/official-WeChat behavior therefore cannot be truthfully claimed from this host.
2. The exact WeChat child-process/window ownership chain can vary by client version. Window correlation must tolerate `_NET_WM_PID` pointing to a child while still requiring that the process belongs to the account Unix UID.
3. A shared X11 DISPLAY means input focus, clipboard and xdotool-style operations are global. Those operations require one display-level lock even though WeChat processes have separate Unix users/HOME/XDG.
4. Existing auto-login is display-global and screen-color based. Multi-account mode must prefer safe non-action over clicking an unverified window.
5. LinuxServer Selkies initializes PUID/PGID and its desktop user separately from the additional WeChat account users. The account bootstrap must not mutate the `abc` desktop identity or break `/config` ownership expected by the base image.
6. A separate-DISPLAY fallback would require additional X server/window-manager/Selkies routing design. It must not be selected merely because the current Windows host cannot execute the same-DISPLAY POC.

## 10. Real modification location for work package A

Implementation stays in this derivative checkout:

```text
work/runtime
```

Expected modified/new locations are:

```text
Dockerfile
docker-compose.yml
.env.example
root/defaults/menu.xml
root/scripts/start.sh
root/scripts/wechat/*
root/custom-cont-init.d/*       # root-time account bootstrap using the Selkies/LinuxServer init hook
tests/*
docs/*                          # package-A POC/runbook only
```

The upstream reference in `../../upstream/wechat-selkies` remains read-only.

