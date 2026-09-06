# Flash-B — Desktop F5 Closure Report

日期：2026-09-06
Lane：Flash-B（仅 Desktop F5；独立 worktree `rc5/flash-b-desktop-f5`，未触碰 AgentWechat wh.3 / NAS live / Sending / H2 / Production）

## 结论

```text
F5 = FIXED（server-side + HTTP/LAN supported mode；真实浏览器 mouse/keyboard/resize smoke 留给 Integration-Live 的 L4 gate）
```

- RB-002 的根因是：Selkies web client 在非 secure context 下硬拒绝运行（`This application requires a secure connection (HTTPS)`），而 RC.5 部署的 Desktop Gateway 无 TLS listener，`WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME` 默认 `http`，`auto` provider 选择仍然返回 Selkies 会话 → Console 打开必然失败的页面。
- 修复采用任务书优先方案：**HTTPS 时继续 Selkies；HTTP/LAN 自动 noVNC fallback**。不改 TLS、不引入自签证书默认路径。

## Source SHA

- 基于 Runtime RC.5 source：`57773abe807fa49eb044bbca266c6fb30ac83f36`（branch 起点）
- 修复 commit：`372da75708c39989f44929da3d5f173fa3b5651c`（branch `rc5/flash-b-desktop-f5`，已推送 origin）
- 变更范围（exact diff 摘要，共 3 文件 +466/-16）：
  - `root/scripts/wechat/agent_wechat_runtime.py`：新增 `public_gateway_scheme()`；`desktop()` scheme-aware provider selection；session descriptor/result 新增 `novnc_reconciled`。
  - `root/scripts/wechat/desktop_gateway.py`：新增 `secure_public_origin()` / `descriptor_novnc_reconciled()` / `downgraded_novnc_descriptor()` / `downgraded_novnc_location()` / `secure_origin_required_response()` / `downgrade_selkies_to_novnc()`；`desktop_handler` 在非 secure origin 下对 Selkies 会话走 downgrade 分支。
  - `tests/test_wechat_runtime.py`：更新 2 个既有测试（补 `WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME=https`、descriptor 断言），新增 12 个测试（详见 Tests 一节）。

## Provider / Fallback 设计（Desktop provider selection 规则）

唯一可信的 secure-origin 信号是部署级声明 `WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME`（生产 compose 已接线，默认 `http`）。Gateway 自身不做 TLS 终结，浏览器可达的 HTTPS 只能来自受信 front proxy；客户端可伪造的 `X-Forwarded-Proto` 一律不信任（若信任，HTTP 浏览器可诱导 Gateway 下发 Selkies → 直接复现 F5 失败；反向误判则只是少功能仍可用）。

**会话创建时（`AgentWechatManager.desktop()`）：**

1. `explicit novnc` → noVNC 会话（与 RC.5 行为一致，强制 `ensure_interactive_desktop`）。
2. `explicit selkies`：
   - public scheme 非 https → **fail closed**，抛出 `desktop_provider=selkies requires WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME=https`（在启动 companion 之前），绝不返回已知无法工作的 provider。
   - public scheme = https → 行为同 RC.5（companion 失败则报错）。
3. `auto`（Core/Console 生产路径的默认值）：
   - public scheme = https → 尝试 `ensure_selkies_desktop`；companion 失败 → 回退 noVNC 并带 `fallback_reason`（RC.5 行为）。成功时额外 best-effort 执行 `ensure_interactive_desktop`（幂等 reconciliation；x11vnc 与 Selkies 共存、互不依赖），结果写入 descriptor 的 `novnc_reconciled`。
   - public scheme = http（LAN 默认部署）→ **直接选择 noVNC，完全不启动 Selkies companion**（不多消耗一个容器），无 `fallback_reason`（这是设计内选择，不是降级）。

**会话服务时（Gateway `desktop_handler`）：**

- Selkies 会话 + secure origin → 与 RC.5 完全一致（本地 bundle + companion 代理）。
- Selkies 会话 + 非 secure origin（例如 operator 翻转 scheme env、或旧会话残留）→ 同一 opaque session 内自动降级：
  - `novnc_reconciled=true`：`/desktop/<s>/` 与 `/index.html` 302 到 `/desktop/<s>/vnc/?autoconnect=true&path=desktop%2F<s>%2Fvnc%2Fwebsockify`；`vnc*` 路由（含 websockify WS）按原生 noVNC 会话完全相同的代码路径代理（per-request 浅拷贝 descriptor 翻转 provider，不改动存储的 descriptor）。
  - `novnc_reconciled=false`（或 legacy descriptor 缺字段，默认 false）→ 全部 fail closed 返回 503 HTML，明确提示“Selkies 需要 HTTPS / 从 Console 重开以使用 noVNC”。
- noVNC 会话 → 与 RC.5 完全一致（HTTP/HTTPS 均可用）。

**已知边界（如实声明）：**

- 同一部署同时暴露 HTTP 与 HTTPS 入口时，provider 由部署级 scheme 决定 + Gateway 按连接降级；`features` 字段按会话创建时的选择上报，HTTP 降级页面不会改写 Core 侧 features（Core 白名单透传，功能性无影响）。
- rc.5 时代已存在的 Selkies descriptor（无 `novnc_reconciled`）在升级后按 fail-closed 处理：HTTP 访问得 503 说明页，HTTPS 访问不受影响；descriptor TTL 默认 4h 自行过期。

## Security Invariants（全部保持，未放宽）

- child `:6174` 无 Host publish：本 lane 未触碰任何 container payload / 端口代码；既有测试 `test_selkies_companion_is_display_only_account_scoped_and_has_no_host_port` 等继续 PASS。
- upstream token 不进 browser URL / redirect：downgrade 302 的 Location 仅含 opaque session id（有测试断言无 `token=`）；`upstream_url(websocket=True)` 的服务端注入逻辑未改动。
- Core/Console JSON 不出现 token：descriptor 字段只有 `novnc_reconciled`（bool），与 token 无关。
- access log 不记录 token：Gateway `access_log=None` 未改动。
- 服务端注入 upstream auth：noVNC 降级路径复用 `upstream_headers` / `upstream_url(websocket=True)`（同原生 noVNC 会话）；Selkies 路径的 `X-WeChat-Hub-Desktop-Token` 注入未改动。
- HTTP + WebSocket Upgrade + binary frame：降级路径的 `vnc/websockify` WS 代理与原生 noVNC 会话同路径（有集成测试）。
- clipboard 继续 HARD_DISABLED：`_selkies_clipboard_enabled()` 恒 False；noVNC features 本身 clipboard=False；未引入任何启用途径。
- expired/unknown session fail closed：`load_session` 逻辑未改动，降级分支在 session 校验之后。
- path traversal / symlink escape protection：`_safe_web_file` 等未改动，既有 fail-closed 测试继续 PASS。

## Tests

本地（Windows，Python 3.12 + aiohttp 3.14.3）与 CI（ubuntu, `python3 -m unittest discover`）双跑：

- 全量套件：**95 passed, 0 failed**（pytest 与 unittest 各跑一次，3.0–3.6s）。
- Shell compile checks（CI preflight 同款 `bash -n`）：PASS。

新增/更新的测试清单：

单元（RuntimeRegistryTests）：
1. `test_desktop_auto_selects_novnc_for_http_public_scheme_without_companion` — HTTP 部署 auto 直选 noVNC、不启动 companion、无 fallback_reason、descriptor 落盘 `novnc_reconciled=true`。
2. `test_desktop_auto_https_keeps_selkies_and_records_novnc_reconciled` — HTTPS 部署 auto 保持 Selkies 且执行 reconciliation，descriptor/result 记录 `novnc_reconciled=true`。
3. `test_desktop_selkies_session_survives_novnc_reconciliation_failure` — reconciliation 失败不阻断 Selkies 会话，仅标记 fail-closed。
4. `test_desktop_explicit_selkies_fails_closed_for_http_public_scheme` — 显式 selkies over HTTP 在 companion 启动前 fail closed。
5. `test_public_gateway_scheme_is_normalized_and_fail_closed` — scheme env 规范化/非法值回退 http。

集成（DesktopGatewaySelkiesWebClientTests，aiohttp TestClient/TestServer）：
6. `test_selkies_session_on_http_origin_redirects_entry_to_novnc_client` — 302 → noVNC client 路径，无 token，后续 `vnc/` 请求走 novnc provider 代理（descriptor 翻转验证）。
7. `test_selkies_session_on_http_origin_proxies_websocket_vnc_routes` — `vnc/websockify` Upgrade 走 novnc 代理路径。
8. `test_selkies_session_on_http_origin_fails_closed_without_reconciliation` — 未 reconciled 时 entry/asset/vnc 全部 503，body 无 token。
9. `test_selkies_session_on_http_origin_serves_only_vnc_and_entry` — 非 vnc Selkies asset 503 fail closed。
10. `test_selkies_websocket_on_http_origin_rejects_non_vnc_routes` — Selkies companion WS 路由对 HTTP 浏览器 503 拒绝。
11. `test_public_scheme_env_decides_selkies_serving_or_downgrade` — 同一 descriptor：https → Selkies 200；http → 302 降级。

既有测试更新：
- `test_desktop_auto_falls_back_to_novnc_without_recreating_old_live_account`（补 https env，保持其 companion-failure 回退意图）。
- `test_selkies_desktop_descriptor_is_opaque_and_advertises_rich_input_features`（补 `ensure_interactive_desktop` patch + `novnc_reconciled` 断言）。
- `DesktopGatewaySelkiesWebClientTests._patch_env` 默认注入 `WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME=https`（Selkies serving 测试即 secure-origin 语义）。

既有覆盖继续有效（无需重写）：landing 200 / assets 200 + content-type / WS 101 + text+binary frame / reload（重复 landing）/ expired+unknown session 404 / no token leakage（body+headers+redirect）/ path traversal & symlink / manifest+fallback branding / noVNC 会话不读 Selkies web root。

**Integration-only（本 lane 无法执行，明确留给 Integration-Live 的 L4 gate）：**
- 真实浏览器 mouse / keyboard / resize smoke（本环境无 Playwright/真实浏览器 + 不允许部署 NAS）。
- 真实 Selkies companion + 真实 websockify 的端到端帧流（本地为 TestServer 仿真）。
- 判定标准提醒：L4 必须以真实浏览器可交互为准，server-side 200/101 不能替代。

## Artifact

- Runtime source 已改变 → **需要新 immutable Runtime artifact：`0.1.0-rc.6`**（rc.5 未被触碰、未被覆盖）。
- tag-absence 预检：本会话无 `read:packages` scope，匿名/交换 token 均 403（按 fail-closed 规则不得视为 absent）；权威判定由 publish workflow 的凭据化 `docker buildx imagetools inspect` 完成：**"Release tag is unused: ghcr.io/onestao/wechat-hub-runtime:0.1.0-rc.6"**（run 34011628064 log）。主仓库 manifest/docs 无任何 rc.6 占用记录，与任务书"下一个未占用 tag = 0.1.0-rc.6"一致。
- 发布结果：
  ```yaml
  release: 0.1.0-rc.6
  image: ghcr.io/onestao/wechat-hub-runtime
  tag: 0.1.0-rc.6            # 另有 sha-372da75
  registry_digest: sha256:b5a0b0088ff7ff40803964d735fe110cd689c1dcfb6ea092a817204d01f85cb4
  platform: linux/amd64
  oci_revision: 372da75708c39989f44929da3d5f173fa3b5651c   # = build-arg OCI_REVISION=${{ github.sha }}
  source_commit: 372da75708c39989f44929da3d5f173fa3b5651c
  ```
- Workflow 证据：
  - `Runtime Publish RC Image` run **34011628064**：preflight（全量 unittest + shell checks）success → tag-absence PASS → build+push success。URL: https://github.com/onestao/wechat-hub-runtime/actions/runs/34011628064
  - `Runtime CI`（branch push）run 34011621185：success。
  - **事故与处置（如实记录）**：git tag `v0.1.0-rc.6` 同时触发了上游 `docker.yml`（`Build and Publish Docker Image`，run 34011628089），其 metadata 会对同一 `ghcr.io/onestao/wechat-hub-runtime:0.1.0-rc.6` tag 推送（且不带 OCI_REVISION build-arg，多架构），存在覆盖权威 artifact、破坏 OCI revision 一致性的竞争。该 run 在启动后 ~1m19s（构建阶段、未到 push 步骤）被本会话取消（conclusion=cancelled），权威 publish 的 push 完成于其之后（04:33:35Z > 04:30:50Z），tag 最终 digest 唯一来自 publish workflow。**后续建议**：RC 发布统一走 `workflow_dispatch`（RC.4/RC.5 即如此），或给 `docker.yml` 增加 concurrency guard / tag 过滤；本 lane 不改动已打 tag 的 commit。

## 对 Integration-Live 会话的输入

```text
RUNTIME_FINAL_SOURCE_SHA      = 372da75708c39989f44929da3d5f173fa3b5651c
RUNTIME_FINAL_REGISTRY_DIGEST = sha256:b5a0b0088ff7ff40803964d735fe110cd689c1dcfb6ea092a817204d01f85cb4
RUNTIME_FINAL_TAG             = 0.1.0-rc.6（immutable，platform linux/amd64，OCI revision 372da75…）
```

- NAS 默认 `WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME=http` → 新 desktop 会话将是 noVNC，真实浏览器在 HTTP 下可打开并交互（F5 闭环）。若 operator 配置了受信 HTTPS front proxy，将 scheme env 置 `https` 即恢复 Selkies，HTTP 入口自动降级 noVNC。
- 升级后首次 L4 前：旧 Selkies descriptor 若仍存活（<4h），HTTP 访问得 503 说明页属预期 fail-closed，重新从 Console 打开即可。
- 清单更新提醒：`release/manifest-0.1.0-rc.6.yaml`（或最终 manifest）runtime 项应写 digest `sha256:b5a0b008…` + `source_commit/oci_revision 372da75…`；Flash-C 不应提前写入，以本报告与 registry 实查为准。

## Garbage Check

```text
resource cleanup: PASS
```

- 本任务创建的资源：runtime worktree `G:/LLM/WeChat_Hub/.worktrees/flash-b-desktop-f5`（registered git worktree，任务要求保留为 lane 工作区）、git branch `rc5/flash-b-desktop-f5`、git tag `v0.1.0-rc.6`、GHCR artifact `0.1.0-rc.6`。
- 保留理由：worktree/branch 为 Flash-B 交付物（供 review/Integration 引用）；tag 与 GHCR artifact 为 immutable release candidate（Owner：WeChat Hub release 流程，后续由 Integration-Live/manifest 生命周期管理）。均非临时垃圾。
- 无容器 / 无临时 volume / 无测试目录残留（本地未运行 Docker；测试全部使用进程内 tempfile 自动回收）；CI 运行于 GitHub 托管 runner，无宿主残留。
- 无 intentionally preserved 的临时资源需要他人删除。
