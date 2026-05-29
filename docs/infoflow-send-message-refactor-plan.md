# infoflow_send_message、InfoflowSendService 与 ServerAPI 消息发送改造方案

本文档记录 `infoflow_send_message` 工具、`InfoflowSendService` 应用发送层与 `ServerAPI` 底层发送接口的最终改造方案。方案依据 `docs/infoflow-message-format.md` 中的真实发送测试结论制定。

本文档描述最终设计和当前实现对照；后续修改发送链路时，应以本文档和 `docs/infoflow-message-format.md` 为准。

## 1. 目标

当前问题：

- `infoflow_send_message` 在构建群消息时，部分场景把 `body[].type` 和 `message.header.msgtype` 硬编码为 `TEXT`，导致 Markdown 被如流按纯文本渲染。
- 发送链路曾同时在 tool、adapter/Bot、ServerAPI 多处承担 reply 补全、格式选择、payload 构造等职责，边界不清晰。
- 如流群聊和私聊的底层协议差异很大，大小写、字段位置、reply 形态、AT 占位等规则容易在后续修改中被破坏。

改造目标：

- 大模型只使用通用、语义化参数表达发送意图。
- `tools.py` 只作为大模型接口层，负责 target 解析、tool schema 和 tool 返回包装。
- `InfoflowSendService` 作为应用发送层，统一处理通用发送意图、`reply_to` 标准化、本地 preview/被引用消息发送者 imid 补全和私聊结构化 @ warning。
- `serverapi.py` 作为 Infoflow 协议层，统一沉淀如流接口特性、格式路由和 payload 构造。
- `reply_to` 对大模型保持简单，公开契约只要求 `message_id`，允许用户自定义 `preview`。
- 已验证的底层接口特性必须固化到 `ServerAPI`，实现不得偏离 `docs/infoflow-message-format.md`。

## 2. 分层边界

### 2.1 tools.py 职责

`tools.py` 负责：

- 暴露 `infoflow_send_message` schema。
- 解析 `target` 是群聊还是私聊。
- 拒绝已删除字段 `richtext_links`。
- 调用 `InfoflowSendService.send_group()` 或 `InfoflowSendService.send_private()`。
- 根据 `SentResult.sent_messages` 记录 sent_store、bot sent 记录和 outbound event。
- 包装 tool JSON 返回。

`tools.py` 不负责：

- 不解析 link 格式。
- 不解析 `MEDIA:` 图片占位。
- 不标准化 `reply_to`，不补 `preview`。
- 不选择 `MD`、`TEXT`、`IMAGE`、`richtext`。
- 不构造群聊 `body[]`、`message.reply`、私聊 `reply[]`。
- 不处理 `msgid2`、`imid`、`robot_id`、`msgtype`、`body.type`。
- 不处理群聊 AT 占位和底层 AT item 拆分。

### 2.2 InfoflowSendService 职责

`InfoflowSendService` 负责：

- 接收 tool、Bot、standalone 等高层发送入口传入的通用发送意图。
- 将外部 `reply_to` 形态统一转成 `ServerAPI` 要求的标准数组格式。
- 对 `reply_to.preview` 做应用层预览补全：外部传了用外部值；外部未传则尝试按 `message_id` 查本地消息库生成符合客户端展示上限的简短预览。
- 对 `reply_to` 做应用层被引用消息发送者补全：按 `message_id` 从本地消息库/recall 上下文查原消息发送者 imid。该值在 webhook 原始字段中表现为 `fromid` / `FromId`，并以内部分层字段传给 ServerAPI，用于群聊 `message.reply.imid` 和私聊 `reply[].uid`。
- 私聊场景处理结构化 @ 字段：不传给私聊 `ServerAPI`，返回 `private_mentions_ignored` warning。
- 调用 `serverapi.send_group_message_intent()` 或 `serverapi.send_private_message_intent()`。
- 不构造如流最终 payload，不读取图片文件，不解析 links，不选择底层 `msgtype/body.type`。

### 2.3 serverapi.py 职责

`serverapi.py` 负责：

- 接收 `InfoflowSendService` 转好的标准 `reply_to` 数组；数组元素可包含内部字段 `sender_imid`，该字段语义是被引用消息发送者 imid，来源通常是原消息 webhook `fromid` / `FromId`。
- 对 `reply_to` 做底层协议处理，例如缺少 preview/content 时按接口契约省略对应字段。
- 判断 intent 是否包含可发送语义内容；无正文、无图片、无链接、无引用、无有效群 @ 时返回 `error_code="empty_message"`。
- 归一化 `message`、`format`、`links`、`image_paths`、群聊 @。
- 解析 links、`MEDIA:`、图片路径、群聊 inline @。
- 使用注入的 image loader 安全读取图片。
- 按接口文档选择真实发送格式。
- 构造如流群聊大写 payload 和私聊小写 payload。
- 处理多包发送、partial failure、warnings、receipts。

## 3. Tool 对外接口

工具名：

```text
infoflow_send_message
```

schema 只暴露：

```json
{
  "target": "string",
  "message": "string",
  "format": "auto | text | markdown",
  "links": ["string 或 {href,label}"],
  "image_paths": ["string"],
  "reply_to": "string | {message_id,preview} | array",
  "at_all": "boolean",
  "mention_user_ids": ["string"],
  "mention_agent_ids": ["string"]
}
```

不暴露：

```text
richtext_links
msgid2
msg_id2
imid
robot_id
msgtype
body
body.type
```

### 3.1 target

支持：

```text
group:4507088
infoflow:group:4507088
4507088
chengbo05
user:chengbo05
infoflow:chengbo05
infoflow:user:chengbo05
```

规则：

- 纯数字按群聊处理。
- `group:<id>` 按群聊处理，群 ID 必须是数字。
- 其它非空字符串按私聊 uuapName 处理。
- `bot:<agentId>` 不能作为私聊 target，tool 直接报错 `unsupported_target`，不得降级为 `touser=<agentId>`。

### 3.2 message

示例：

```json
{
  "message": "正文 MEDIA:/tmp/blue-200.png 后续说明"
}
```

规则：

- 普通字符串作为正文。
- 可包含 `MEDIA:/abs/path.png` 控制图片与文字顺序。
- `MEDIA:` 路径不能作为正文发出。
- `message` 允许为空字符串，用于只发送 reply、links、图片或群聊 @ 这类不需要正文的消息。
- 只有空白字符的 `message` 不算可发送正文；如果没有其它语义内容，由 ServerAPI 返回 `empty_message`。

### 3.3 format

可选值：

```text
auto
text
markdown
```

规则：

- 默认 `auto`，通常不需要传。
- `auto` 普通正文优先 Markdown。
- 为保留 reply、links、image、复杂 @ 语义而自动不用 Markdown 时，不返回 warning。

### 3.4 links

示例：

```json
[
  "https://example.com",
  "[示例](https://example.com)",
  "[示例]https://example.com",
  {"href": "https://example.com", "label": "示例"}
]
```

规则：

- `href` 必须非空。
- `label` 可省略，默认等于 `href`。
- `links` 是唯一公开链接参数。
- `richtext_links` 不兼容。入参出现时，tool 直接返回错误：

```json
{
  "success": false,
  "reason": "invalid_parameter",
  "error": "unsupported link parameter; use links"
}
```

### 3.5 reply_to

tool 对外支持三种形式。

字符串：

```json
"1866420577904309248"
```

对象：

```json
{
  "message_id": "1866420577904309248",
  "preview": "被引用消息预览"
}
```

数组：

```json
[
  "1866420577904309248",
  {
    "message_id": "1866420577904309888",
    "preview": "第二条引用"
  }
]
```

公开契约：

- `message_id` 是唯一必需字段。
- `preview` 可选，允许外部自定义；显式 preview 只做 NUL/连续空白清理，不按自动预览长度截断。
- tool schema 不暴露 `msgid2` 或 `msg_id2`。
- 如果对象 item 额外传了 `messageid/msgid/msgid2/msg_id2/content` 等非公开字段，应用发送层返回 `invalid_reply_to`，不向 `ServerAPI` 透传。

### 3.6 结构化 @

参数：

```json
{
  "at_all": true,
  "mention_user_ids": ["chengbo05"],
  "mention_agent_ids": ["17212", "bot:17212"]
}
```

规则：

- 结构化 @ 仅群聊有效。
- 私聊正文里的 `@xxx` 是普通文本。
- 私聊传结构化 @ 字段时，tool 不传给私聊 `ServerAPI`，并返回 `private_mentions_ignored` warning。
- 如果私聊除了结构化 @ 之外没有任何可发送内容，最终应返回 `empty_message`。

## 4. reply_to 标准化策略

### 4.1 高层入口到 ServerAPI 的 reply_to 形态

`ServerAPI` 的 intent 接口只接受标准数组：

```python
reply_to: list[dict[str, str]] | None
```

数组 item 必须是 dict，且至少包含：

```python
{"message_id": "..."}
```

允许包含：

```python
{
    "message_id": "...",
    "preview": "..."     # 可选
}
```

因此，tool、Bot、standalone 等高层入口必须经由 `InfoflowSendService`，由 service 把外部形式转换成该数组后再调用 serverapi。
service 到 serverapi 之间的内部数组元素还可以带 `sender_imid`：

```python
{
    "message_id": "...",
    "preview": "...",
    "sender_imid": "1744775667"  # 被引用消息发送者 imid，来自 webhook fromid/FromId
}
```

`sender_imid` 不暴露给大模型，也不是 tool 入参字段；它只用于 ServerAPI 写入如流 reply 身份字段。

### 4.2 InfoflowSendService 预览补全

`InfoflowSendService` 标准化每个 `reply_to` item 时按以下顺序处理 `preview`：

1. 外部 item 显式传了 `preview`，使用外部值；仅去除 NUL 字符并把连续空白折叠成单个空格，不按自动预览长度截断。
2. 外部未传 `preview`，service 用 `message_id` 查询本地可见消息。
3. 查询顺序为：`MessageStore.find_any(message_id).content`，`recall.get_inbound_body(message_id)`。
4. 查到消息文本后，从文本取前 100 个字符作为 preview。
5. 如果文本长度不超过 100 个字符，全部使用。
6. 如果文本长度超过 100 个字符，使用前 100 个字符加 ASCII 三点 `...`。
7. 如果查不到消息记录，service 不报错，只传 `{"message_id": "..."}` 给 serverapi。

预览生成示例：

| 原文 | service preview |
|---|---|
| `好的` | `好的` |
| `这是一个短消息` | `这是一个短消息` |
| 101 个 `一` | 前 100 个 `一` + `...` |
| `@chengbo5.1 (agent_id:6471)  请引用这条消息` | `@chengbo5.1 请引用这条消息` |

说明：

- 自动补齐 preview 时，100 个字符按 Python 字符串切片计数，不按字节计数；实测群聊 reply 预览展示超过 100 字会被回声/客户端截成前 100 字 + `...`。
- preview 需要去除 NUL 字符，把连续空白折叠成单个空格，把 data image base64 替换为 `[image]`，并移除 `@xxx (agent_id:...)` / `@xxx (user_id:...)` 这类仅供插件内部识别的 @ 元数据。
- preview 只用于客户端引用预览，不用于判断 message_id 是否有效。
- service 不做 preview cache；每次按上述顺序读当前 store/recall 状态。
- preview 补全不读 `SentMessageStore`。`MessageStore` 是全量消息事实源，已包含入站和本机器人发出的消息；`SentMessageStore` 只用于发送去重、reply-to-self 检测、按条数/ID 撤回和跨进程 recent sent 索引。

### 4.3 InfoflowSendService reply 发送者补全

`InfoflowSendService` 标准化每个 `reply_to` item 时，还会尽力补齐被引用消息发送者 imid：

1. 优先查询 `MessageStore.find_any(message_id).raw_json`。
2. 群聊原始消息读取 `fromid` 或 `message.header.fromid`；这些字段值语义为发送者 imid。
3. 私聊原始消息读取 `FromId` 或 `fromid`；这些字段值语义为发送者 imid。
4. 如果 raw JSON 缺少发送者 imid，则根据 `MessageStore` 记录的 `sender` 字段查 participants 表：`user:<uuapName>` 走 `find_user_by_user_id()`，`bot:<agentId>` 走 `find_bot_by_agent_id()`，读取其中的数字 imid。
5. 如果 MessageStore/participants 仍查不到，再尝试 `recall.get_inbound_sender_imid(message_id)`。
6. 查到后作为内部 `sender_imid` 传给 ServerAPI。
7. 查不到不报错，仍继续发送 reply；但客户端引用卡片开头的 `Reply <name>:` 可能无法准确显示原发送者。

实测 marker `20260529-095643`：

- 群聊 `G-RID-03-SENDER-FROMID`：`message.reply.imid` 传被引用消息发送者 imid `1744775667`，该值来自原消息 `fromid`，客户端引用卡片正确显示 `Reply chengbo05:`。
- 群聊不传 `imid` 或用当前机器人 imid 兜底均不能正确显示被引用者。只有当被引用消息本身由该机器人发送时，当前机器人 imid 才是正确的 sender imid。
- 私聊 `P-RID-04-UID-FROMID`：`reply[].uid` 传被引用消息发送者 imid `1744775667`，该值来自原消息 `FromId`，客户端引用卡片正确显示 `Reply chengbo05:`。
- 私聊 `uid="0"`、`uid="chengbo05"` 或不传 `uid` 均不能正确显示被引用者。

### 4.4 ServerAPI reply_to 校验与兜底

serverapi 接收标准数组后只做底层相关处理：

- 入参不是 list：返回 `invalid_reply_to`。
- item 不是 dict：返回 `invalid_reply_to`。
- item 含 `messageid`、`msgid`、`msgid2`、`content` 等非 `message_id/preview/sender_imid` 字段：返回 `invalid_reply_to`。
- item 缺少 `message_id` 或为空：返回 `invalid_reply_to`。
- item 有 `preview`：优先使用该 preview；ServerAPI 在最终 wire 层仍会做 NUL/空白/内部 @ 元数据清理，并按客户端展示上限裁成前 100 字符 + `...`。
- item 没有 `preview`：根据具体底层接口需要处理。
- item 有 `sender_imid`：必须是数字字符串；ServerAPI 将其写入群聊 `message.reply.imid` 或私聊 `reply[].uid`。
- item 没有 `sender_imid`：ServerAPI 仍发送 reply；群聊省略 `message.reply.imid`，私聊省略 `reply[].uid`，不会用当前机器人 imid 或 `"0"` 兜底冒充被引用者。

serverapi 的 preview/content 兜底规则：

- 群聊已实测 `message.reply` 只传 `messageid`、不传 `preview` 可成功保留 `replyData`；`preview=""` 也可成功保留 `replyData`。因此群聊缺 preview 时可以不传 `preview`，不必为了服务可用性强行填 `"引用消息"`。
- 群聊如果有 service 补齐或外部自定义的 preview，应写入 `message.reply.preview`，改善客户端引用预览；ServerAPI 会按 100 字符展示上限裁剪。若有 service 补齐的 `sender_imid`，必须写入 `message.reply.imid`，否则 `Reply <name>:` 前缀可能错误。
- 私聊已实测 `reply[]` item 只传 `uid/msgid`、不传 `content` 时 API 接受，且收件人确认引用正常展示；`content=""` 也 API 接受且引用正常展示。因此私聊缺 preview/content 时可以不传 `content`，不必填 `"引用消息"`。如果有 preview，ServerAPI 会按 100 字符展示上限裁剪后写入 `reply[].content`。如果有 service 补齐的 `sender_imid`，必须写入 `reply[].uid`；如果没有 `sender_imid`，省略 `uid`，避免写入错误身份值。
- 私聊错误 `msgid` 也不会阻断消息发送；收件人确认消息正文正常展示，reply 区域展示错误态。这属于服务端/客户端容错，serverapi 无需提前拦截。

如果 message_id 在如流服务侧不存在：

- service 不应因本地查不到 preview 提前报错，因为本地消息库可能缺失但服务端 message_id 仍有效。
- serverapi 只按接口契约构造 payload 并发送。
- 如果如流服务报错，原样返回 send failure。
- 如果如流服务接受并正确展示 reply，则视为成功。

### 4.5 reply_to 情况矩阵

| 外部输入 | Service 输出给 ServerAPI | ServerAPI 行为 | 预期结果 |
|---|---|---|---|
| 未传 `reply_to` | `None` 或 `[]` | 不构造 reply payload | 普通消息发送 |
| `"MID"`，本地库查到短正文 `你好` 和 sender imid | `[{"message_id":"MID","preview":"你好","sender_imid":"1744775667"}]` | 使用该 preview 和 reply 身份字段 | 如果服务端 MID 有效，则展示 reply，且引用卡片前缀显示原发送者 |
| `"MID"`，本地库查到长正文和 sender imid | `[{"message_id":"MID","preview":"前100字符...","sender_imid":"1744775667"}]` | 使用该 preview 和 reply 身份字段 | 如果服务端 MID 有效，则展示 reply，且引用卡片前缀显示原发送者 |
| `"MID"`，本地库查不到 | `[{"message_id":"MID"}]` | 群聊可不传 preview；私聊可不传 content | 如果服务端/客户端能解析 MID，则展示 reply；如果 MID 错误，群聊服务拒绝，私聊消息发送成功但 reply 区域显示错误态 |
| `{"message_id":"MID","preview":"自定义"}` | `[{"message_id":"MID","preview":"自定义"}]` | 使用自定义 preview | 如果服务端 MID 有效，则展示 reply |
| `{"message_id":"MID","preview":""}` | `[{"message_id":"MID"}]` 或补库预览 | 空字符串不作为有效自定义 preview；按缺省处理 | 群聊服务支持空 preview，私聊也支持空 content；service 不把空字符串当自定义预览 |
| `[MID1, MID2]` 群聊 | 两个标准 item | 群聊只取第一条，warning `group_reply_truncated` | 只回复第一条 |
| `[MID1, MID2]` 私聊 | 两个标准 item | 私聊保留多条 reply | 已验证 5 条以内可展示 |
| 群聊错误 `message_id` + preview | 标准 item | 透传给服务 | 服务返回 `请求参数错误`，不发送消息 |
| 群聊错误 `message_id` 且无 preview | 标准 item | 透传给服务 | 服务返回 `请求参数错误`，不发送消息 |
| 私聊错误 `message_id` + content | 标准 item | 透传给服务 | API 返回成功；客户端正文正常展示，reply 区域显示错误态 |
| 私聊错误 `message_id` 且无 content | 标准 item | 透传给服务 | API 返回成功；客户端正文正常展示，reply 区域显示错误态 |
| item 缺 `message_id` | service 不调用 serverapi，直接返回错误 | `invalid_reply_to` | 不发送 |
| item 带 `messageid/msgid/msgid2/msg_id2/content` 等非公开字段 | service 不调用 serverapi，直接返回错误 | `invalid_reply_to` | 不发送 |
| 直接调用 ServerAPI 时 item 带 `messageid/msgid/msgid2/content` | ServerAPI 返回错误 | `invalid_reply_to` | 不发送 |

### 4.5 边界实测结论

当前接口文档已经验证：

- 群聊 `TEXT + reply` 可用。
- 群聊 `reply-only` 可用。
- 群聊 reply 数组 2/3 条失败，单 object 成功。
- 私聊 `text + reply[]` 可用。
- 私聊 `richtext + reply[]` 可用。
- 私聊 `image + reply[]` 可用。
- 私聊 5 条以内多 reply 可展示。

新增实测：

- 脚本：`scripts/sim/probe_reply_preview_edges.py`。
- marker：`20260528-195317`。
- 群聊 `G_VALID_REPLY_NO_PREVIEW`：`message.reply={"messageid": valid_mid}`，API 成功，回声包含 `replyData`。
- 群聊 `G_VALID_REPLY_EMPTY_PREVIEW`：`preview=""`，API 成功，回声包含 `replyData`。
- 群聊 `G_WRONG_REPLY_WITH_PREVIEW`：错误 `messageid` + preview，API 失败 `请求参数错误`。
- 群聊 `G_WRONG_REPLY_NO_PREVIEW`：错误 `messageid` 且无 preview，API 失败 `请求参数错误`。
- 私聊 `P_VALID_REPLY_NO_CONTENT`：`reply[]` item 无 `content`，API 成功，msgkey `1866432997747965952`，收件人确认引用正常展示。
- 私聊 `P_VALID_REPLY_EMPTY_CONTENT`：`content=""`，API 成功，msgkey `1866432998379208704`，收件人确认引用正常展示。
- 私聊 `P_WRONG_REPLY_WITH_CONTENT`：错误 `msgid` + content，API 成功，msgkey `1866432998957039616`，收件人确认正文正常展示，reply 区域展示错误态。
- 私聊 `P_WRONG_REPLY_NO_CONTENT`：错误 `msgid` 且无 content，API 成功，msgkey `1866432999532707840`，收件人确认正文正常展示，reply 区域展示错误态。

实现结论：

- 群聊不用为了服务可用性补默认 preview；有 preview 就传，没有就不传。
- 群聊错误 messageid 会由服务端拒绝，serverapi 如实返回错误。
- 私聊 API 不校验 reply `msgid` 是否存在；错误 `msgid` 会让客户端 reply 区域展示错误态，但不会阻断正文消息。
- 私聊缺 content 或空 content 已确认能正常展示引用；serverapi 可以在没有 preview 时省略 `reply[].content`。

## 5. Tool 与 Service 内部流程

tool 伪代码：

```python
target = parse_target(args["target"])

if "richtext_links" in args:
    return invalid_parameter("unsupported link parameter; use links")

send_service = adapter._send_service

if target.chat_type == "group":
    result = await send_service.send_group(
        target.group_id,
        message=args.get("message"),
        format=args.get("format", "auto"),
        links=args.get("links"),
        image_paths=args.get("image_paths"),
        reply_to=args.get("reply_to"),
        at_all=args.get("at_all"),
        mention_user_ids=args.get("mention_user_ids"),
        mention_agent_ids=args.get("mention_agent_ids"),
        session=session,
    )
else:
    result = await send_service.send_private(
        target.user_id,
        message=args.get("message"),
        format=args.get("format", "auto"),
        links=args.get("links"),
        image_paths=args.get("image_paths"),
        reply_to=args.get("reply_to"),
        at_all=args.get("at_all"),
        mention_user_ids=args.get("mention_user_ids"),
        mention_agent_ids=args.get("mention_agent_ids"),
        session=session,
    )
```

tool 必须使用 `result.sent_messages` 记录实际发出的每条消息，不再自己根据入参猜测包数量。

service 伪代码：

```python
reply_items = normalize_reply_to(args.reply_to)

if private and has_structured_mentions:
    warnings.append(private_mentions_ignored)
    drop at_all/mention_user_ids/mention_agent_ids

if group:
    result = await serverapi.send_group_message_intent(..., reply_to=reply_items)
else:
    result = await serverapi.send_private_message_intent(..., reply_to=reply_items)

return result with service warnings merged
```

## 6. ServerAPI 依赖注入

建议 `ServerAPI` 增加可选依赖：

```python
ServerAPI(
    *,
    settings: dict[str, Any],
    image_loader: Callable[[str], Awaitable[bytes]] | None = None,
)
```

如果初始化顺序不方便，也可以使用 setter：

```python
serverapi.set_image_loader(self._load_image_bytes)
```

`image_loader` 用于：

- 读取 `MEDIA:` 和 `image_paths` 指向的本地图片或 URL。
- 复用 adapter 当前安全策略：只读允许的 media root，限制大小，URL 走安全 fetch。
- 如果 intent 接口需要发送图片但没有 image_loader，返回明确错误。

## 7. ServerAPI 群聊接口

```python
async def send_group_message_intent(
    self,
    group_id: str,
    *,
    message: str | None = None,
    format: str = "auto",
    links: Any = None,
    image_paths: Any = None,
    image_bytes: Any = None,
    reply_to: list[dict[str, str]] | None = None,
    at_all: Any = False,
    mention_user_ids: Any = None,
    mention_agent_ids: Any = None,
    session: aiohttp.ClientSession | None = None,
) -> SentResult
```

参数：

- `group_id`: 数字字符串，例如 `"4507088"`。
- `message`: 正文，可含 `MEDIA:/abs/path.png`。
- `format`: `auto/text/markdown`。
- `links`: URL、`[label](url)`、`[label]url`、`{href,label}` 或数组。
- `image_paths`: 本地图片路径数组，追加到 message 中 inline MEDIA 之后。
- `image_bytes`: 内部调用专用，bytes 或 bytes 数组，追加到 `image_paths` 之后；不暴露给 tool/prompt。
- `reply_to`: 标准 reply item 数组，群聊最终只用第一条。
- `at_all`: bool。
- `mention_user_ids`: uuapName 数组。
- `mention_agent_ids`: agentId 数组，支持 `bot:<agentId>`。

群聊路由规则：

- 普通正文 `auto/markdown`：走 `MD`。
- `format=text`：走 `TEXT`。
- 有 reply：走 `TEXT`；若同包有图片则走 `IMAGE`。
- 有 links：无图片走 `TEXT`，有图片走 `IMAGE`。
- 有 image：走 `IMAGE`，图片包内所有文字强制 `TEXT`。
- `@all + 具体用户/机器人`：`auto/markdown` 可继续走 `MD`，但只有 @all 原生生效，具体对象按 MD 正文文本展示；若同包有 reply/link/image 或显式 `format=text`，走 `TEXT/IMAGE` 并拆多个 `AT` item 保留全部原生 AT。
- 群多 reply：只取第一条，warning `group_reply_truncated`。

群聊 payload 必须严格遵守：

- `header.msgtype` 只允许大写 `TEXT/MD/IMAGE`。
- body item `type` 只允许大写 `TEXT/MD/AT/LINK/IMAGE`。
- 不出站使用回声里的 `MIXED`。
- `totype` 固定 `"GROUP"`，`role` 固定 `"robot"`。
- `message.reply` 只能是单个 object，不能是数组。

body item 示例：

```json
{"type": "TEXT", "content": "hello"}
{"type": "MD", "content": "@chengbo05 **hello**"}
{"type": "AT", "atall": true}
{"type": "AT", "atuserids": ["chengbo05"]}
{"type": "AT", "atagentids": [17212]}
{"type": "LINK", "href": "https://example.com", "label": "示例"}
{"type": "IMAGE", "content": "<base64>"}
```

群聊 reply payload 示例：

```json
{
  "message": {
    "reply": {
      "messageid": "1866420577904309248",
      "preview": "被引用消息预览",
      "imid": "1744775667"
    }
  }
}
```

群聊坑点必须固化：

- `MD + reply` API 可能成功但会丢 `replyData`，禁止选择。
- `message.reply.imid` 是被引用消息发送者 imid。该值通常来自原消息 webhook `fromid` / `FromId`。可省略且仍保留 `replyData`，但客户端引用卡片 `Reply <name>:` 前缀可能不准确；不能用当前机器人 imid 兜底，除非被引用消息本身就是当前机器人发送的。
- `MD + LINK` 不可用，links 必须走 `TEXT/IMAGE`。
- `IMAGE` 包内不能放 `MD`。
- `MD + AT` 必须在 `MD.content` 里补占位：`@uuapName`、`@agentId`、`@all`。
- `@all` 和具体对象需要全部原生 AT 时，`TEXT/IMAGE` 下拆成多个 `AT` item；`MD` 下只保留 @all 原生，具体对象按正文文本展示。
- `LINK.href` 必填，缺失直接报错。
- `mention_agent_ids` 非数字且无法解析直接报错。
- 当前机器人自身 agentId 跳过，并 warning `self_mention_skipped`。

## 8. ServerAPI 私聊接口

```python
async def send_private_message_intent(
    self,
    user_id: str,
    *,
    message: str | None = None,
    format: str = "auto",
    links: Any = None,
    image_paths: Any = None,
    image_bytes: Any = None,
    reply_to: list[dict[str, str]] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> SentResult
```

私聊接口不接收：

```python
at_all
mention_user_ids
mention_agent_ids
```

参数：

- `user_id`: uuapName，例如 `"chengbo05"`。
- `message`: 正文，可含 `MEDIA:/abs/path.png`。
- `format`: `auto/text/markdown`。
- `links`: 同 tool。
- `image_paths`: 图片路径数组。
- `image_bytes`: 内部调用专用，bytes 或 bytes 数组；不暴露给 tool/prompt。
- `reply_to`: 标准 reply item 数组，私聊保留多条。

私聊路由规则：

- 无 reply/link/image，`auto/markdown`：走 `msgtype="md"`。
- `format=text`：走 `msgtype="text"`。
- 有 reply 且无 links/image：走 `text`，避免 `md + reply[]` 丢引用。
- 有 links：走 `richtext`，支持 link-only、text+link、多 link、reply、多 reply。
- 有 image：走 `image`。
- `links + image`：拆多条发送，reply 只挂第一条，warning `message_split`。
- reply-only：走 `text.content="" + reply[]`。

私聊 payload 必须严格遵守：

- `msgtype` 小写：`text/md/richtext/image`。
- 内容对象 key 与 `msgtype` 同名且小写。
- 不使用群聊 `message.header/body`。
- `reply` 是顶层数组，不放入内容对象。

richtext 示例：

```json
{
  "msgtype": "richtext",
  "richtext": {
    "content": [
      {"type": "text", "text": "请看："},
      {"type": "a", "href": "https://example.com", "label": "示例"}
    ]
  }
}
```

reply 示例：

```json
{
  "reply": [
    {
      "content": "被引用消息预览",
      "uid": "1744775667",
      "msgid": "1866420577904309248"
    }
  ]
}
```

私聊坑点必须固化：

- `md + reply[]` API 成功但客户端丢引用，禁止选择。
- `reply[].uid` 是被引用私聊消息发送者 imid。该值通常来自原消息 webhook `FromId/fromid`，不是 `"0"`，也不是 `chengbo05` 这类账号名；缺失或错误会导致引用卡片 `Reply <name>:` 前缀不准确。
- richtext item `type` 使用小写 `text/a`。
- link-only richtext 合法。
- 私聊 reply 是数组，已验证 5 条以内展示成功，不限制成 2。
- `msgid2` 不作为必要字段，不暴露给大模型。

## 9. ServerAPI 接口边界

保留的新高层接口：

```python
send_group_message_intent(...)
send_private_message_intent(...)
```

保留的结构化接口仅用于 intent builder、可用组合测试脚本和明确结构化 payload 发送；调用方必须传标准 `reply_to` 数组：

```python
send_group_structured(..., reply_to: list[dict[str, str]] | None = None)
send_private_structured(..., reply_to: list[dict[str, str]] | None = None)
```

删除旧高层接口，避免继续触发已验证不可靠的旧格式路径：

```python
api.send_group_message(...)
api.send_private_message(...)
send_to_group(...)
send_to_dm(...)
send_image_to_group(...)
send_image_to_dm(...)
```

tool、Bot、adapter、standalone 等高层发送入口均只调用 `InfoflowSendService`；只有 service 调用 intent 接口，只有结构化接口负责把标准 `reply_to` 转成如流最终请求字段：

- 群聊：`message.reply` object。
- 私聊：`reply[]` array。

群聊 `send_group_structured()` 仍要做协议族语义校验，不能只校验大小写：

- `msgtype="MD"`：只允许 `AT/MD` body item；必须有 `MD`；最多一个 `AT`；不能带 `reply_to`。
- `msgtype="TEXT"`：只允许 `TEXT/AT/LINK` body item。
- `msgtype="IMAGE"`：只允许 `TEXT/AT/LINK/IMAGE` body item；必须有 `IMAGE`；文本不能用 `MD`。
- 需要验证后端明确失败文本的 exact-wire 负例时，用 `probe_contract_edges.py` 的 raw group post，不通过 `send_group_structured()`。

私聊 `send_private_structured()` 也要做语义校验：

- `markdown` 不能与 `reply_to` 同传；底层 API 会返回成功但客户端丢引用展示。
- `richtext_content` item type 只允许小写 `text/a`；链接 item 必须有 `href` 和 `label`。
- 不允许完全空发送；至少要有一个内容模式，或带 `reply_to` 形成 reply-only 请求。
- `text=""` 仅允许与 `reply_to` 组合形成 reply-only；空 `markdown` 始终拒绝。

## 10. 返回结构

新增：

```python
@dataclass
class SentMessageReceipt:
    message_id: str
    msgseqid: str = ""
    kind: str = "text"      # text | markdown | richtext | image | mixed
    preview: str = ""
```

扩展：

```python
@dataclass
class SentResult:
    success: bool
    message_id: str = ""
    msgseqid: str = ""
    continuation_message_ids: tuple[str, ...] = ()
    continuation_msgseqids: tuple[str, ...] = ()
    sent_messages: tuple[SentMessageReceipt, ...] = ()
    warnings: tuple[dict[str, str], ...] = ()
    error_code: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str = ""
```

要求：

- 新字段必须有默认值，避免影响旧调用。
- `sent_messages` 按真实发送顺序排列。
- 多包发送时，`message_id/msgseqid` 可保留最后一条，`continuation_*` 保留前序 ID 以兼容旧逻辑。
- 第 N 条失败时，`success=False`，`sent_messages` 包含已成功发出的前 N-1 条，tool 返回 `partial_failure`，提醒不要自动重发。

## 11. Warning 与错误

warning：

- `private_mentions_ignored`
- `group_reply_truncated`
- `message_split`
- `deduplicated`
- `self_mention_skipped`

不 warning：

- `auto` 自动选择底层格式。
- 为保留 reply/link/image 自动不用 Markdown。
- 自动补 MD AT 占位。
- service 用 `message_id` 查不到本地预览。

错误：

- `target` 为空或非法。
- `bot:<agentId>` 作为私聊 target。
- `richtext_links` 出现。
- ServerAPI 判断没有任何可发送内容，返回 `error_code="empty_message"`，tool 原样透出为 `reason="empty_message"`。
- `format` 非 `auto/text/markdown`。
- link 缺 `href`。
- 图片路径无法读取、MEDIA 语法损坏、图片不合法。
- group id 非数字。
- `mention_agent_ids` 非数字且无法解析。
- `reply_to` item 非 dict，或缺少 `message_id`。
- 如流发送失败。

## 12. 实现结构建议

不要把所有逻辑堆进两个 intent 方法。建议拆成：

- `InfoflowSendService._normalize_reply_to`
- `InfoflowSendService._lookup_reply_preview`
- `_normalize_intent_format`
- `_parse_message_segments`
- `_normalize_links`
- `_validate_serverapi_reply_to`
- `_normalize_group_mentions`
- `_build_group_packets`
- `_build_private_packets`
- `_send_packets_collect_receipts`
- `_sent_result_with_receipts`

这样接口特性变化时只改对应 helper。

## 13. 测试覆盖

必须覆盖：

- tool schema 不含 `richtext_links`，出现时报错。
- tool 群/私聊 target 分发。
- service 把字符串、对象、数组 `reply_to` 统一转成标准数组。
- service 对外部 preview 优先使用。
- service 对本地可查 message_id 生成 100 字 preview，超长追加 `...`。
- service 对本地可查 message_id 从 raw JSON、participants 表或 recall 补齐被引用消息发送者 imid，作为内部 `sender_imid` 传给 ServerAPI。
- service 生成 preview 时去掉 `@xxx (agent_id:...)` / `@xxx (user_id:...)` 内部 @ 元数据。
- service 对本地查不到 message_id 不报错，传 `{"message_id": ...}`。
- tool 调用 service，不直接调用 ServerAPI intent。
- adapter/Bot 调用 service，不直接调用 ServerAPI intent。
- standalone 调用 service，不直接调用 ServerAPI intent。
- serverapi 对缺 preview 的群 reply 省略 `message.reply.preview`。
- serverapi 对缺 preview 的私聊 reply 省略 `reply[].content`。
- serverapi 把 `sender_imid` 映射到群聊 `message.reply.imid` 和私聊 `reply[].uid`。
- serverapi 不使用当前机器人 imid 兜底群聊 reply 身份字段；只有被引用消息本身由当前机器人发送时，该 imid 才应来自 service 的 `sender_imid`。
- 私聊结构化 @ warning 且不传 serverapi。
- 群普通文本默认 MD。
- 群 reply 强制 TEXT，不能 MD。
- 群 links 走 LINK/TEXT。
- 群 image 包内文字是 TEXT。
- 群 MD AT 自动补占位。
- 群 `@all + 具体对象` 在 MD 下只保留 @all 原生、具体对象按文本展示；TEXT/IMAGE 下拆 AT 可保留全部原生 AT。
- 群多 reply 只第一条并 warning。
- 私聊普通 auto 走 md。
- 私聊 reply 走 text。
- 私聊 links/link-only/multi-link 走 richtext。
- 私聊 richtext + 多 reply。
- 私聊 links + image 拆包和 partial failure。
- 群聊大小写严格校验。
- 群聊 structured 协议族语义校验。
- 私聊大小写严格校验。
- `SentResult.sent_messages` 在多包和失败时准确。

## 14. 与上一版方案的差异核对

已保留的内容：

- tool/service/serverapi 分层边界。
- `richtext_links` 删除且报错。
- 私聊接口不接收结构化 @。
- 图片加载通过注入 image loader，避免绕过 adapter 安全策略。
- 拆包下沉到 serverapi，并通过 `sent_messages` 回传。
- 群聊大小写、私聊大小写、reply、AT、LINK、IMAGE 的全部接口坑点。
- 旧 `send_to_group/send_to_dm/send_image_*` 高层接口删除，避免旧 MD+reply 路径继续被上层误用。

本次新增或调整：

- `reply_to` 的 serverapi 入参收敛为标准 dict 数组。
- 新增 `InfoflowSendService`，作为 tool/Bot/standalone 与 ServerAPI 之间的应用发送层。
- service 负责把外部字符串、对象、数组转换为标准数组。
- service 负责外部自定义 preview、本地 100 字 preview 补全、被引用消息发送者 imid 补全。
- serverapi 不再接收任意形态 `reply_to`，只校验标准数组。
- serverapi 负责判断空发送意图并返回 `error_code="empty_message"`；tool 只透出该 reason。
- serverapi 对缺 preview/content 的 reply 按已验证契约省略对应字段；私聊缺 `sender_imid` 时也省略 `reply[].uid`，不写 `"0"`。
- 明确列出 `reply_to` 每种情况的处理矩阵。

## 15. 风险评估

方案结构合理，职责边界清晰。对原有功能影响可控：adapter、Bot、standalone 的发送入口保持不变，但内部统一改走 `InfoflowSendService`；`SentResult` 新字段有默认值；tool 的 breaking change 仅针对尚未对外发布的 `richtext_links`。

主要风险：

- reply preview 归一化逻辑混淆：外部显式 `preview` 只清理 NUL/空白和内部 @ 元数据，不按自动预览长度截断；只有 service 自动从本地消息库补 preview 时固定取前 100 个字符并追加 `...`。
- reply 身份字段混淆：群聊 `message.reply.imid` 和私聊 `reply[].uid` 都应传被引用消息发送者 imid。该值在 webhook 原始字段中通常叫 `fromid` / `FromId`；不能用当前机器人 imid、用户账号名或 `"0"` 作为正常路径兜底。
- service 用 `message_id` 查不到本地预览时误报错。必须允许继续发送。
- serverapi 缺 preview/content 时应省略字段，不能再填无意义默认文案。必须测试覆盖。
- 图片加载下沉后绕过 adapter 安全读取。必须通过注入 `image_loader` 解决。
- 拆包下沉后 tool 记录不准。必须通过 `sent_messages` 解决。
- 如流大小写和字段位置实现遗漏。必须通过接口文档约束和测试矩阵覆盖。
