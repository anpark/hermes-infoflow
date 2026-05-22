# hermes-infoflow

Baidu Infoflow（如流）Channel 插件 for [Hermes Agent](https://github.com/nousresearch/hermes-agent)。

把企业内的 Infoflow 群聊 / 私聊接入 Hermes，机器人可以接收并回复 Markdown / 链接 / 图片消息，支持 @-mention 触发、消息撤回（recall）、cron 定时投递等。本仓库对齐 [openclaw-infoflow](https://github.com/chbo297/openclaw-infoflow) 的发布形态：一个主插件包（`hermes-infoflow`） + 一个独立的安装 CLI（`hermes-infoflow-tools`）+ 与 OpenClaw 同款的 `scripts/deploy.sh` 脚本。

> **Hermes 主仓零改动**：完全靠 hermes-agent 已存在的 4 条插件加载路径接入。

---

## 功能（balanced scope · v0.2）

- ✅ Webhook 接入 + AES-ECB 解密 + echostr 签名校验
- ✅ 群聊 / 私聊 文本 & Markdown 双向（含分块）
- ✅ 图片入站（含 `Bearer-` 鉴权 fallback 下载）+ 出站（含 path-traversal 校验）
- ✅ @-mention 检测：`require_mention` + 五种 `reply_mode`（`ignore` / `record` / `mention-only` / `mention-and-watch` / `proactive`）+ `watch_mentions` + `watch_regex`
- ✅ 群聊 follow-up 窗口（`INFOFLOW_FOLLOW_UP`）与 per-group 配置覆盖（`INFOFLOW_GROUPS`）
- ✅ 自有消息回环防护（robotId 自动发现后丢弃 bot 自身转发）
- ✅ 消息撤回（recall）：私聊 + 群聊；agent tool `infoflow_recall_message`；SQLite 持久化 sent-message store（cron 子进程可 `count` 撤回）
- ✅ 长整型 message_id 精度保护（19 位数字不丢精度）
- ✅ cron `deliver=infoflow` 投递（home channel）+ 独立进程发送（`standalone_sender_fn`）
- ⚠️ 已知限制（计划在后续版本支持）：
  - 单账号（不支持 OpenClaw 的 `accounts.*` 子配置）
  - WebSocket 接入模式未实现（仅 webhook）

---

## 4 种安装方式

主推 directory-style 三种（A / B `--mode extract` / D）；entry-point 方式（B `--mode pip` / C）作为高级用法。

### A. `hermes plugins install`（最像 OpenClaw 体验）

```bash
hermes plugins install <github-owner>/hermes-infoflow
hermes plugins enable infoflow
hermes gateway restart
```

hermes 内置命令；`git clone --depth 1` 到 `~/.hermes/plugins/infoflow/`。仓库根目录已包含 `__init__.py`（代理到 `hermes_infoflow/` 子包），无需手动扁平化即可直接工作。

### B. `hermes-infoflow-tools`（对齐 `npx ... update`）

> 推荐用 `pipx run` 或 `uvx`，免去全局污染。

正式版（stable）：

<!-- sync:hermes-infoflow-version:latest -->
```bash
pipx run hermes-infoflow-tools update --version 2026.5.21
```
<!-- /sync:hermes-infoflow-version:latest -->

Beta 版（PEP 440 prerelease；不会被默认 `pip install` 拉到）：

<!-- sync:hermes-infoflow-version:beta -->
```bash
pipx run hermes-infoflow-tools update --version 0.2.2b1
```
<!-- /sync:hermes-infoflow-version:beta -->

子命令参数：

- `--version <ver>`：PyPI 版本号（缺省 `latest`，会被解析为当前正式版）
- `--index-url <url>`：PyPI 源（默认 `https://pypi.org/simple`）
- `--mode extract`（默认）：`pip download` sdist → 解包 → rsync 到 `~/.hermes/plugins/infoflow/`；体验对齐 OpenClaw。
- `--mode pip`：`pip install --upgrade hermes-infoflow==<ver>` 到 site-packages。**注意**：此模式 hermes 不读 `plugin.yaml`，`hermes config` 不会列 `INFOFLOW_*`。
- `--channel-id <id>`：目标目录名（默认 `infoflow`，仅在你知道自己在做什么时改）
- `--dry-run`：仅打印命令

### C. `pip install hermes-infoflow`（最 Pythonic）

```bash
pip install hermes-infoflow
hermes plugins enable infoflow
hermes gateway restart
```

通过 PyPI 包的 `hermes_agent.plugins` entry-point 让 hermes 自动发现。**限制**：hermes 在 entry-point 模式下不读 `plugin.yaml`，所以 `hermes config` 不会列 `INFOFLOW_*` —— 需要你手动 `export` 或 `hermes config set` 这些环境变量。

### D. 本地开发：`bash scripts/deploy.sh`

```bash
git clone https://github.com/chbo297/hermes-infoflow
cd hermes-infoflow
bash scripts/deploy.sh             # 同步到 ~/.hermes/plugins/infoflow/、重启 gateway
bash scripts/deploy.sh --dry-run   # 仅打印操作
```

`deploy.sh` 会同步插件并自动选择 Python：优先 `hermes` / `pipx` 的 `hermes-agent` venv，再尝试 `python3`。若缺少 `cryptography` / `aiohttp` / `pyyaml`，默认会尝试 `pipx inject hermes-agent …` 或对当前解释器 `pip install`（可用 `HERMES_DEPLOY_AUTO_PIP=0` 关闭）。若检测到的解释器与 gateway 实际用的 pipx venv 不一致，脚本会打印 warning。

镜像 [`openclaw-infoflow/scripts/deploy.sh`](https://github.com/chbo297/openclaw-infoflow/blob/main/scripts/deploy.sh)。

---

## 配置（环境变量）

任一安装路径完成后，下面这些环境变量都需要在 hermes 运行的 shell / `~/.hermes/.env` 里设置。

### 必需

| 变量 | 含义 |
|------|------|
| `INFOFLOW_API_HOST` | 如流 API 根地址，例如 `https://api.infoflow.example.com` |
| `INFOFLOW_APP_KEY` | 应用 appKey |
| `INFOFLOW_APP_SECRET` | 应用 appSecret（原始；插件会自动 MD5 lowercase hex） |
| `INFOFLOW_CHECK_TOKEN` | echostr 签名校验用 token |
| `INFOFLOW_ENCODING_AES_KEY` | base64-URL-safe 的 AES 密钥（16/24/32 字节明文，对应 AES-128/192/256） |

### 可选

| 变量 | 默认 | 含义 |
|------|------|------|
| `INFOFLOW_APP_AGENT_ID` | 无 | 私聊撤回必须；如流后台「应用 ID」 |
| `INFOFLOW_ROBOT_NAME` | 无 | 机器人显示名，用于 @-mention 识别 |
| `INFOFLOW_PORT` | `8646` | Webhook 监听端口 |
| `INFOFLOW_HOST` | `0.0.0.0` | Webhook 监听地址 |
| `INFOFLOW_WEBHOOK_PATH` | `/webhook/infoflow` | Webhook 路径 |
| `INFOFLOW_HOME_CHANNEL` | 无 | cron `deliver=infoflow` 缺省目标，如 `bob` 或 `group:12345` |
| `INFOFLOW_HOME_CHANNEL_NAME` | 同上 | Home channel 显示名 |
| `INFOFLOW_REPLY_MODE` | `mention-and-watch` | `ignore` / `record` / `mention-only` / `mention-and-watch` / `proactive` |
| `INFOFLOW_REQUIRE_MENTION` | `true` | 群消息是否仅在 @ 时响应 |
| `INFOFLOW_WATCH_MENTIONS` | 无 | 逗号分隔；命中后即使没 @ 机器人也会触发 |
| `INFOFLOW_WATCH_REGEX` | 无 | 正则匹配触发（多行或 `|||` 分隔） |
| `INFOFLOW_FOLLOW_UP` | `true` | 机器人回复后群聊 follow-up 窗口是否开启 |
| `INFOFLOW_FOLLOW_UP_WINDOW` | `300` | follow-up 窗口秒数 |
| `INFOFLOW_GROUPS` | 无 | 按群 ID 的 JSON 配置覆盖 |
| `INFOFLOW_ADMIN_USER` | 无 | 管理员 uuapName（敏感工具权限） |
| `INFOFLOW_ALLOWED_USERS` | 无 | 逗号分隔的 uuapName allowlist |
| `INFOFLOW_ALLOW_ALL_USERS` | `false` | 允许所有人（仅开发） |
| `HERMES_STATE_DIR` | `~/.hermes/state` | sent-messages.db 等状态目录 |
| `INFOFLOW_DASHBOARD_ENABLED` | `true` | 是否启用 localhost session 仪表盘 |
| `INFOFLOW_DASHBOARD_EVENT_BUFFER` | `2000` | 每个 session 在内存中保留的最大事件条数 |

设置完后：

```bash
hermes config show                  # 验证当前生效配置
hermes gateway restart              # 重新加载插件
hermes gateway status               # 期望看到 "infoflow: running"
```

---

## Session 仪表盘（Dashboard）

插件在 webhook 同一端口上提供 **仅 localhost** 可访问的 Web UI，用于查看 Hermes gateway 内 agent session 的实时运行情况。

### 访问地址

Gateway 启动且 infoflow 插件连接成功后，在本机浏览器打开：

```
http://127.0.0.1:8646/webhook/infoflow/dashboard
```

（端口与路径可通过 `INFOFLOW_PORT`、`INFOFLOW_WEBHOOK_PATH` 调整；路径为 `{WEBHOOK_PATH}/dashboard`。）

- **列表页**：默认只显示 `platform=infoflow` 的 session；URL 加 `?scope=all` 可查看 gateway 内所有平台。
- **Session 页**：点击某个 session 进入详情（无返回按钮）；通过 SSE 增量刷新事件时间线。

### 安全

- 所有 dashboard 路由仅接受来源 `127.0.0.1` / `::1`，其他 IP 返回 403。
- **请勿**在 Caddy/Nginx 等反代上把 `/webhook/infoflow/dashboard` 暴露到公网。

### 展示粒度（与 CLI 的差异）

仪表盘通过 hermes-agent **插件 hooks** 收集事件（`pre_llm_call`、`post_llm_call`、`pre_tool_call`、`post_tool_call`、`on_session_*` 等），属于 **turn 级别** 时间线：

- 每次 LLM 请求/回复各一条
- 每次 tool 调用开始/结束各一条（可展开 args/result）

这与 `hermes` 交互式 CLI / TUI 的 **逐 token 流式** 输出不同。CLI 的 `thinking.delta` / `message.delta` 运行在独立的 TUI 进程内，gateway 进程中的 platform 插件无法在不修改 hermes-agent 的前提下订阅同等粒度的流式事件。

关闭仪表盘：`INFOFLOW_DASHBOARD_ENABLED=false`。

---

## Session Tracker（终端风格实时视图）

在 webhook 同一端口上提供 **可通过反代访问** 的只读 Web UI，按群或私聊目标跟踪单个 Hermes session，以 CLI 风格终端展示 tool 行、Hermes 回复框与状态行。

### 访问地址

```
/webhook/infoflow/sessiontracker?chatType=2&chatId=<群ID>
/webhook/infoflow/sessiontracker?chatType=7&chatId=<占位>&code=<私聊code>
```

示例（私聊需有效 `code`，由如流 OAuth 回调提供）：

```
https://<your-domain>/webhook/infoflow/sessiontracker?chatType=7&chatId=3950087625&code=2cecba82ba9686cb75596bfbe5637f03
```

- `chatType=2`：群聊，`chatId` 为群号 → 目标 `group:{chatId}`
- `chatType=7`：私聊，必须带 `code`；插件调用 Infoflow `getuserinfo` 解析为 uuap（`UserId`）作为 DM 目标

详见 [docs/infoflow-getuserinfo-api.md](docs/infoflow-getuserinfo-api.md)。

### Session 对应关系（与 Hermes gateway）

| 场景 | Hermes `session_key`（持久） | `chat_id`（Tracker 绑定键） |
|------|------------------------------|---------------------------|
| 私聊 | `agent:main:infoflow:dm:{uuap}` | 与 gateway 相同：`{uuap}`（getuserinfo 的 `UserId`） |
| 群聊 | `agent:main:infoflow:group:group:{群ID}:{发送者uuap}` | `group:{群ID}`（**每个发送者独立 session**） |

- **同一私聊 / 同一群+发送者**：gateway **复用**同一 `session_key`，在 idle 重置或 `/new` 前保持同一 `session_id`。
- **群聊 Tracker URL** 只带群号时，页面展示该群下 **最近活跃** 的那条 session（顶栏会显示 `user: {uuap}`）；不是把全群多人合并成一条 session。
- **SSE 订阅**（`/sessiontracker/api/stream`）须携带与打开页面相同的 query（`chatType`、`chatId`、`code`），用于校验 `session_id` 属于该目标。

### 与 Dashboard 的区别

| | Dashboard | Session Tracker |
|---|-----------|-----------------|
| 路径 | `{WEBHOOK_PATH}/dashboard` | `{WEBHOOK_PATH}/sessiontracker` |
| 访问 | 仅 localhost | 可经反代（`code_auth`：私聊需有效 code） |
| 视图 | Session 列表 + 事件时间线 | 单目标 CLI 终端 + 自动滚动 |

### 展示粒度（Phase A）

当前 **不改 hermes-agent**，通过插件 hooks + outbound 启发式复刻 CLI 输出：

- `post_tool_call`：尽量用 `agent.display.get_cute_tool_message` 生成 tool 行
- `post_llm_call`：Hermes 回复框
- `post_api_request`：模型 / token 状态行
- `outbound.infoflow` 中含 tool emoji 的短文本当作 progress 镜像

**局限**：无逐 token 流式、无 thinking spinner；interim 句仅在最终 `post_llm_call` 出现。

### Phase B（可选，需改 hermes-agent）

在 `hermes_cli/plugins.py` 增加并在 `gateway/run.py` 调用：

- `on_tool_progress` — gateway tool progress 回调
- `on_stream_delta` — 流式 delta
- `on_interim_assistant` —  interim 助手句

注册后 Session Tracker 可接近 CLI/TUI 体验。

关闭：`INFOFLOW_SESSIONTRACKER_ENABLED=false`。

---

## Infoflow 后台 webhook 设置

进入如流企业后台的应用页面，把回调地址填成：

```
https://<your-domain>/webhook/infoflow
```

> 注意：必须配 HTTPS。如果你的服务器对公网仅暴露 8646 / 内网端口，请在前面挂反向代理 + TLS。

### Caddy 示例

```caddyfile
hermes.example.com {
    reverse_proxy /webhook/infoflow localhost:8646
}
```

### Nginx 示例

```nginx
server {
    listen 443 ssl http2;
    server_name hermes.example.com;
    ssl_certificate     /etc/letsencrypt/live/hermes.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hermes.example.com/privkey.pem;

    location /webhook/infoflow {
        proxy_pass http://127.0.0.1:8646/webhook/infoflow;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

### Cloudflare Tunnel 示例

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /Users/bo/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: hermes.example.com
    service: http://localhost:8646
  - service: http_status:404
```

---

## 撤回（recall）

机器人可调用 `infoflow_recall_message` agent tool 撤回自己刚发的消息。两种用法：

```jsonc
// 按具体 message_id（任意源）
{ "target": "group:12345", "message_id": "1859713223686736431" }

// 不传 message_id，自动撤回最近 N 条
{ "target": "alice", "count": 1 }
```

⚠️ **跨进程撤回**：自 v0.2.0 起，出站消息默认写入 `~/.hermes/state/infoflow/sent-messages.db`（可用 `HERMES_STATE_DIR` 覆盖），cron 子进程与 gateway 共享 `count` 撤回。若 SQLite 不可用则回退为 gateway 进程内内存 ring buffer。

⚠️ 切勿把 *用户发来的入站 message_id* 当成撤回目标 —— 那是用户的消息，不是机器人的，API 会返回失败。机器人发出的消息 id 在 LLM 的工具结果里能拿到；或者用 `count=1` 撤回最近一条。

---

## 安全注意事项

- **AES-ECB**：Infoflow 服务端的设计选择（与 wecom 的 CBC 不同）。ECB 在通常情况下是不安全的密码模式；本插件做了我们能做的（PKCS7、严格 key length、签名校验）。详见 [`hermes_infoflow/crypto.py`](hermes_infoflow/crypto.py)。
- **Webhook 公网暴露**：必须配 TLS + 反向代理；端口上的明文请求只能被 echostr 签名 + AES 密文形式保护，**不要** 0.0.0.0 直接暴露。
- **本地图片 path traversal**：插件只接受 `~/.hermes/media/`、`/tmp`、系统临时目录里的 `file://` 路径。其余路径会被拒绝。
- **大整数 message_id**：内部统一存字符串；撤回 API 调用时手工拼 JSON 字面量，避免被 `json.dumps` 误加引号。

---

## 发版流程

PyPI 没有 npm 的 dist-tag 概念。我们用 **PEP 440 prerelease** 后缀来表达 beta。两条 stream 各自的完整流程：

### A. 正式版（stable）发布流程

将下方所有 `<X.Y.Z>` 替换为目标正式版本号（不带 `b/a/rc` 后缀）：

```bash
# 1) 改主包 pyproject.toml 的 version 字段
hatch version <X.Y.Z>
# 同步 tools 子包版本（与主包对齐）
hatch version <X.Y.Z> --pyproject tools/hermes-infoflow-tools/pyproject.toml

# 2) 同步 README 安装命令版本号
python scripts/sync_readme_install_version.py

# 3) 编辑 CHANGELOG.md 顶部，添加本版本章节

# 4) 发布前校验
ruff check hermes_infoflow tools tests
pytest -q

# 5) 提交、打 tag、push
git add pyproject.toml tools/hermes-infoflow-tools/pyproject.toml README.md CHANGELOG.md
git commit -m "<X.Y.Z>"
git tag <X.Y.Z>
git push origin main <X.Y.Z>

# 6) CI 自动构建并发布到 PyPI（主包 + tools 子包）
#    见 .github/workflows/publish.yml
```

### B. Beta 预发布流程

将下方所有 `<X.Y.Z-beta.N>` 替换为目标预发版本号，**写成 PEP 440 形式**：`<X.Y.Z>b<N>`（例如 `0.1.0b1`，**不要** 写 `0.1.0-beta.1`，PyPI 不接受）：

```bash
hatch version <X.Y.ZbN>
hatch version <X.Y.ZbN> --pyproject tools/hermes-infoflow-tools/pyproject.toml
python scripts/sync_readme_install_version.py
# ...同上 commit/tag/push...
```

PyPI 默认 **不** 把 prerelease 当成 `pip install <pkg>` 的目标，所以发完 beta 后用户安装 stable 不受影响。要装 beta：

```bash
pip install --pre hermes-infoflow                # 拉最新 prerelease
pip install hermes-infoflow==<X.Y.ZbN>           # 显式锁定
```

### 当前版本

<!-- sync:hermes-infoflow-version -->
```bash
hatch version 2026.5.21
git tag 2026.5.21
git push origin 2026.5.21
```
<!-- /sync:hermes-infoflow-version -->

### 排查：PyPI 上拉不到刚发的版本

```bash
# 1) 检查 PyPI 元数据已对外可见
pip index versions hermes-infoflow --pre
curl -s https://pypi.org/pypi/hermes-infoflow/json | python -m json.tool | head -40

# 2) 强制忽略本地缓存重拉
pip install --no-cache-dir --force-reinstall --upgrade hermes-infoflow==<version>

# 3) 看 GH Actions publish job 是否真的成功了
gh run list --workflow publish.yml --limit 3
```

---

## 开发

```bash
git clone https://github.com/chbo297/hermes-infoflow
cd hermes-infoflow

# 安装开发依赖
pip install -e ".[dev]"

# 运行单元测试（无需 hermes-agent）
pytest -q

# 运行所有测试（包括 adapter / registration，需要 hermes-agent 在 PYTHONPATH）
PYTHONPATH=/path/to/hermes-agent pytest -q
```

Linting / typecheck：

```bash
ruff check hermes_infoflow tools tests
```

部署到本地 hermes 一键测试：

```bash
bash scripts/deploy.sh
```

---

## 与 OpenClaw 仓库的对照

| OpenClaw | hermes-infoflow |
|----------|------------------|
| npm 主包 `@chbo297/infoflow` | PyPI 主包 `hermes-infoflow` |
| npm tools `@chbo297/infoflow-openclaw-tools` | PyPI tools `hermes-infoflow-tools` |
| `~/.openclaw/extensions/infoflow/` | `~/.hermes/plugins/infoflow/` |
| `openclaw.json` 里 `plugins.entries.<id>.enabled` (dict) | `~/.hermes/config.yaml` 里 `plugins.enabled: [...]` (list) |
| `openclaw plugins install @chbo297/infoflow@X` | `hermes plugins install <repo>` 或 `hermes-infoflow-tools update --version X` |
| `npx -y @chbo297/infoflow-openclaw-tools update` | `pipx run hermes-infoflow-tools update` |
| `--tag beta`（npm dist-tag） | PEP 440 prerelease `0.1.0b1` + `pip install --pre` |

---

## License

MIT — see [LICENSE](LICENSE).
