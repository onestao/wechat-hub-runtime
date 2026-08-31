# Gate 0 POC Result

Status: **PASSED AT PROCESS LEVEL ON UNRAID; MANUAL LOGIN PROOF REMAINS**

Date: 2026-08-31 19:29 +08:00
Host: Unraid (`x86_64`, Docker `27.5.1`, Compose `v2.40.3`)
Project: `wechat-hub-a-gate0`
Container: `wechat-hub-a-gate0-runtime`
Official WeChat package: `4.1.1.8`
Official package SHA-256:
`c9765e87ee5133bf4bb50d585c1814fafd995e3fb0da62c5ed07259b43dada7b`

## Topology A

The mandated topology was tested first and succeeded:

1. same container;
2. same X DISPLAY (`:1`);
3. different Unix users;
4. different HOME/XDG paths;
5. two real official Linux WeChat processes.

Separate DISPLAY (topology B) was not used and remains unnecessary under this
result.

## Runtime Evidence

`/scripts/wechat/wechat-runtime health --json` reported `healthy: true`.

| Field | gate0-a | gate0-b |
|---|---:|---:|
| Unix user | `wx_gate0_a` | `wx_gate0_b` |
| UID | `20000` | `20001` |
| HOME | `/config/wechat-accounts/gate0-a/home` | `/config/wechat-accounts/gate0-b/home` |
| DISPLAY | `:1` | `:1` |
| Main PID | `521` | `528` |
| Main command | `/usr/bin/wechat` | `/usr/bin/wechat` |
| Window | `0xC00006` | `0xE00006` |
| Window title | `Weixin` | `Weixin` |

The two launch chains were:

```text
runuser --user wx_gate0_a -- dbus-run-session -- /usr/bin/wechat
  -> wx_gate0_a(20000) dbus-run-session
  -> wx_gate0_a(20000) /usr/bin/wechat (pid 521)

runuser --user wx_gate0_b -- dbus-run-session -- /usr/bin/wechat
  -> wx_gate0_b(20001) dbus-run-session
  -> wx_gate0_b(20001) /usr/bin/wechat (pid 528)
```

The child processes and per-account crashpad/Radium paths also remained
separate under the two account HOME trees.

## Host Access

The isolated container was left running for the manual login check:

```text
HTTP : http://192.168.22.102:13000
HTTPS: https://192.168.22.102:13001
```

## Build Note

The Unraid Docker host initially failed to connect to `dldir1v6.qq.com`
during image build. The official WeChat `.deb` was downloaded separately and
copied into the isolated Gate-0 build directory. The remote-only Dockerfile
then installed that local package. The local source Dockerfile was not changed
for this test.

QQ was also installed because it is enabled by the upstream default build.
Its download and dependency repair succeeded. QQ is outside the Gate-0 proof.

## Remaining Human Check

The automated result proves process isolation, separate profiles and separate
X11 windows. The remaining Gate-0 login proof is to open the Selkies UI above,
log both official clients into different WeChat accounts, and record that the
two distinct windows remain active after login. This is a browser and mobile
WeChat action and cannot be safely automated by this package.
