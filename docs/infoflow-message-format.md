# Infoflow 底层服务接口契约与实测记录

本文档记录 hermes-infoflow 依赖的如流底层发送接口特性。这里只沉淀服务接口事实：endpoint、payload 结构、字段大小写、必填字段、拼接格式、已验证组合、明确失败组合、以及尚未形成展示契约的边界。后续修改 `infoflow_send_message`、底层 group send 或 private send 逻辑前，应先对照这里的契约；如果新增组合格式，必须先真实发送验证，再更新本文档。

群消息以 webhook 回声验证语义；私聊以 API 返回和收件人侧人工验收共同验证。API 返回成功但没有回声或人工确认时，只能记录为“接口接受”，不能记录为“展示/语义成功”。

## 接口范围

认证：

- Token endpoint：`/api/v1/auth/app_access_token`。
- 请求体使用 `app_key` 和 `md5(app_secret)`，`app_secret` 是小写 hex MD5。
- 后续请求认证头使用 `Authorization: Bearer-<token>`，注意是 `Bearer-` 加连字符，不是标准的 `Bearer <token>` 空格格式。

群消息发送：

- Endpoint：`/api/v1/robot/msg/groupmsgsend`。
- 请求体根结构固定为 `{"message":{"header":{...},"body":[...],"reply":{...可选}}}`。
- 群聊使用大写 `header.msgtype` 和大写 body `type`。
- 群聊服务返回 `messageid` 和 `msgseqid`；webhook 回声用于确认真实语义。

私聊 app 消息发送：

- Endpoint：`/api/v1/app/message/send`。
- 请求体根结构为 `{"touser","toparty","totag","agentid","msgtype",...}`，不使用群聊的 `message.header/body`。
- 私聊使用小写 `msgtype` 和小写同名内容对象，例如 `msgtype="text"` 搭配 `text`。
- 私聊服务返回 `msgkey`；当前 bot 自发私聊没有可匹配 webhook 回声，因此展示语义必须由收件人侧人工确认。

## 调用者速查

本节给外部调用代码一个最小、可执行的协议清单。后文矩阵负责解释每个组合为什么可用或不可用。

通用 HTTP 规则：

- 所有请求体使用 UTF-8 JSON。
- Token 请求只需要 `Content-Type: application/json`。
- 发送消息请求必须带 `Authorization: Bearer-<token>`，这里是连字符 `Bearer-`，不是空格 `Bearer `。
- 当前实现发送消息时还带 `Content-Type: application/json` 和 `LOGID: <uuid>`；`LOGID` 没有做必要性变体测试，外部实现建议保留。
- 成功判定不要只看 HTTP 2xx：发送接口顶层 `code` 必须是 `"ok"`，如果响应内层 `data.errcode` 存在则必须为 `0`。失败时常见错误文本在顶层 `message`/`errmsg` 或内层 `data.errmsg`。
- `messageid`、`msgseqid`、`msgkey` 都可能超过 JavaScript 安全整数范围；调用方应按字符串保存和传递，不要用浮点数或 JS `Number` 承载。

认证最小请求：

```json
{
  "app_key": "<INFOFLOW_APP_KEY>",
  "app_secret": "<lowercase-md5(INFOFLOW_APP_SECRET)>"
}
```

认证成功响应中从 `data.app_access_token` 取 token；`data.expires_in` 可用于缓存过期时间。本文档只覆盖 app access token，不覆盖其它鉴权模式。

群消息最小 header：

```json
{
  "message": {
    "header": {
      "toid": 4507088,
      "totype": "GROUP",
      "msgtype": "TEXT",
      "clientmsgid": 1779963150243,
      "role": "robot"
    },
    "body": [
      {"type": "TEXT", "content": "hello"}
    ]
  }
}
```

群 `message.header` 必填字段：

| 字段 | 要求 |
|---|---|
| `toid` | 数字群 ID。 |
| `totype` | 固定使用 `"GROUP"`，未验证其它大小写。 |
| `msgtype` | 只能使用已验证的大写 `"TEXT"`、`"MD"`、`"IMAGE"`。不要出站使用回声里的 `"MIXED"`。 |
| `clientmsgid` | 客户端生成的消息 ID；当前实现使用递增的毫秒时间形态整数。 |
| `role` | 固定使用 `"robot"`，未验证其它取值。 |

群消息按需求选择协议：

| 需求 | 出站 `msgtype` | body item | 可带 `message.reply` | 关键限制 |
|---|---|---|---|---|
| 纯文本、reply、LINK、原生 AT | `TEXT` | `TEXT`、`AT`、`LINK` | 可以 | Markdown 不渲染；`LINK.href` 必填；@all + 具体对象要拆多个 `AT` item 才能同时原生生效。 |
| Markdown 渲染 | `MD` | 一个 `MD`，可加一个 `AT` item | 不要 | 不能带 `LINK`；不能放进 `IMAGE` packet；带 reply 会丢 `replyData`。 |
| Markdown + 人类/机器人 AT | `MD` | 一个 `AT` item + `MD` | 不要 | `MD.content` 必须有 `@<uuapName>` 或 `@<agentId>` 占位；带 reply 会丢 `replyData`。 |
| Markdown + @all | `MD` | `AT(atall=true)` + `MD` | 不要 | 推荐在正文放 `@all`；同一条 MD 里可写具体 `@user/@agentId`，但具体对象只按普通文本展示，不是原生 AT；带 reply 会丢 `replyData`。 |
| 图片、图文、图片 + reply/AT/LINK | `IMAGE` | `IMAGE`，可加 `TEXT`、`AT`、`LINK` | 可以 | 图片 packet 内文字只能用 `TEXT`，不能用 `MD`。 |
| reply-only 群消息 | `TEXT` | `TEXT(content="")` | 必须 | 未验证省略 body；已验证空 `TEXT` 可承载 reply。 |

Markdown URL 渲染规则：

- HTTP/HTTPS URL 作为链接发送时，原生 links、Markdown 链接 `[name](url)`、纯文本 URL 均已通过群聊和私聊客户端验收。
- Markdown 图片 `![alt](url)` 仅对 `jpg/jpeg/png/gif/webp` 作为可靠契约；`gif` 动图已确认可渲染。
- 不要把视频、音频、PDF、压缩包、Office 或任意非图片 URL 写成 `![alt](url)`。实测 `mov/mp4/webm/pdf/zip/mp3` 会出现标题外内容为空，属于内容丢失风险。
- 不要依赖 HTML 多媒体标签：`<iframe>` 对 `pdf/mp4` 实测为空；`<img>/<video>/<audio>/<object>` 未形成多媒体渲染契约，可能按标签文本展示。发送层应改写为 Markdown 链接，图片仅在安全格式下改写为 Markdown 图片。
- 详细 BOS 文件 URL 与渲染矩阵见 `docs/infoflow-bos.md`。

群 `message.reply` 只能是单个 object，不能是数组。`msgtype="TEXT"` + body `TEXT` 下传 2 条或 3 条 reply 数组均已验证失败为 `请求参数错误`。

私聊最小文本请求：

```json
{
  "touser": "chengbo05",
  "toparty": "",
  "totag": "",
  "agentid": "6471",
  "msgtype": "text",
  "text": {"content": "hello"}
}
```

私聊顶层字段：

| 字段 | 要求 |
|---|---|
| `touser` | 收件人 uuapName；单人私聊时使用。 |
| `toparty`、`totag` | 字符串；单人私聊测试中为空字符串。 |
| `agentid` | 当前 app agent id；本轮请求用字符串形式。 |
| `msgtype` | 小写 `"text"`、`"md"`、`"richtext"`、`"image"`，必须和同名对象 key 匹配。 |
| `reply` | 可选顶层数组；元素使用 `content`、`uid`、`msgid`，不要放进内容对象内部。 |

私聊按需求选择协议：

| 需求 | `msgtype` | 内容对象 | 可带 `reply[]` | 关键限制 |
|---|---|---|---|---|
| 纯文本、文本 reply、reply-only、多个 reply targets | `text` | `text.content` | 可以 | `text.content=""` + `reply[]` 已确认只展示引用；`text + 5 条 reply[]` 已确认可展示。 |
| Markdown 渲染 | `md` | `md.content` | 不要 | `md + reply[]` API 成功但客户端丢 reply。 |
| 链接、双链接、链接 reply、纯链接 | `richtext` | `richtext.content[]` | 可以 | item `type` 使用小写 `text`/`a`；`a` 使用 `href` 和 `label`；link-only + 5 条 reply[] 已确认可展示。 |
| 图片、图片 reply | `image` | `image.content` | 可以 | 本轮展示验收使用 200x200 PNG；其它尺寸/格式不由本文档结论覆盖。 |

私聊 `reply[]` 元素形态：

```json
{
  "content": "quoted preview",
  "uid": "1744775667",
  "msgid": "1866420577904309248"
}
```

`msgid` 使用被回复私聊消息的 `msgkey`。`uid` 使用被引用私聊消息发送者 imid，该值通常来自原消息 webhook `FromId/fromid`；不要传 `"0"` 或 `chengbo05` 这类账号名。`msgid2` 在部分实现中可选透传，但本轮没有把它验证为必要字段。

私聊 `reply[]` 是数组协议。已人工验收 `text + 3 条 reply[]`、`text + 5 条 reply[]`、`link-only richtext + 5 条 reply[]` 均能展示全部引用；本文档只覆盖已验证的 5 条以内，不外推到任意数量。

## 验证方法

测试时间：2026-05-28

群聊沉淀脚本复测：

- `probe_group_links.py` marker `20260528-173520`。
- `probe_group_formats.py` marker `20260528-173522`。
- `probe_group_formats.py` + `probe_group_links.py` marker `20260528-174105`，补测机器人 AT、@all、LINK、IMAGE 的交叉组合。
- `probe_contract_edges.py` marker `20260528-180215`，补测群聊大小写/reply 边界和私聊 P11-P14 API 接受性；P11-P14 展示已由收件人确认通过。
- `probe_group_formats.py` marker `20260528-180319`，补测 @all 与具体用户 AT 的合并/拆分表现。
- `probe_group_formats.py` marker `20260528-181022`，补测 IMAGE packet 中 @all 与具体用户 AT 的合并/拆分表现。
- `probe_group_formats.py` marker `20260528-181209`，补测 @all 与机器人 AT 在 TEXT、MD、IMAGE packet 中的合并/拆分表现。
- `probe_group_formats.py` marker `20260528-211604`，复测 MD 中 `@all + 具体用户`：单 `AT` item 时只保留原生 @all，具体用户按 MD 正文文本展示；拆多个 `AT` item 仍失败。
- `infoflow_reply_count_matrix.py` 临时脚本 marker `20260528-185321`，补测群聊单 reply object 成功、群聊 reply 数组 2/3 条失败，以及私聊 P15-P17 多 reply API 接受性；P15-P17 展示已由收件人确认通过。该临时脚本的可复用能力已沉淀为 `probe_reply_counts.py`。
- `infoflow_group_multi_reply_text_only.py` 临时脚本 marker `20260528-185721`，专项复测 `msgtype=TEXT` + body `TEXT` 下群聊 reply 数组 2/3 条均失败为 `请求参数错误`。该能力已沉淀为 `probe_reply_counts.py`。
- `probe_reply_preview_edges.py` marker `20260528-195317`，补测群聊 reply 省略/空 `preview`、群聊错误 `messageid`，以及私聊 reply 省略/空 `content`、私聊错误 `msgid` 的 API 和展示表现。群聊语义已通过回声确认；私聊 P01-P04 已由收件人确认：P01/P02 引用正常展示，P03/P04 正文正常展示且 reply 区域显示错误态。
- `test_send_intent_matrix.py` 的 G02 直连 `ServerAPI.send_group_message_intent(reply_to=[{"message_id": ...}])` 不带 preview 时，群聊 reply 结构保留，但客户端引用摘要展示为通用文案 `你收到一条消息，点击跳转查看消息原文`。这验证了底层服务可以不传 preview，但应用发送层如需更好的摘要，应在调用 ServerAPI 前补齐 preview。
- 临时 raw 身份字段探测 marker `20260529-095643`：群聊 `G-RID-03-SENDER-FROMID` 只有在 `message.reply.imid` 传被引用消息发送者 imid `1744775667` 时，客户端引用卡片前缀正确显示 `Reply chengbo05:`；该值来自原消息 webhook `fromid`。私聊 `P-RID-04-UID-FROMID` 只有在 `reply[].uid` 传被引用消息发送者 imid `1744775667` 时前缀正确；该值来自原消息 `FromId`。用当前机器人 imid 兜底、传 `"0"`、传 `chengbo05` 或省略身份字段都不能正确显示被引用者。
- 临时 raw 预览长度探测 marker `20260529-095721`、`20260529-095800`：群聊 reply preview 请求可接受至少 12000 字；但回声/客户端展示上限按字符计，100 字以内完整保留，超过 100 字展示为前 100 字 + ASCII `...`。中文和 ASCII 均验证为字符上限，不按字节上限。
- 已标注为预期失败的 case 均按预期失败；非预期失败会让脚本退出非 0。

测试方式：

- 群聊可用格式矩阵通过当前仓库代码中的 `ServerAPI.send_group_structured()` 发送，不依赖已部署 runtime 副本。
- 群聊明确失败或语义失败的 exact-wire 边界通过 `probe_contract_edges.py` 的 raw group post 发送，避免被 `ServerAPI` 本地结构校验提前拦截。
- 测试群：`4507088`。
- 人类 AT 测试用户：`chengbo05`。
- 机器人 AT 测试 agentId：`17212`（非当前机器人）；当前机器人自身 agentId `6471` 单独验证为接口拒绝。
- 全员 AT：已在测试群验证 `atall=true`。
- 回声验证以 `[iflow:raw]` 中 `message.header.messageid` 精确匹配发送返回的 message id。
- 图片补充测试使用 200x200 纯蓝 PNG，准备后 mime 为 `image/png`、二进制大小 427 bytes、base64 长度 572。
- 私聊直接调用当前仓库代码中的 `_api.send_private_payload()` 发送 app message payload。
- 私聊测试对象：`chengbo05`。私聊使用 app message endpoint，不走群消息 body 数组协议；当前 bot 自发私聊未在 gateway/agent 日志里产生可匹配 webhook 回声，因此私聊接口接受性以 API 返回 `errcode=0` 和 `msgkey` 为主，客户端语义以 P01-P17 人工验收为准。
- 群消息判定格式成功不能只看 send API 返回 `success=true`，还要看回声是否保留目标语义：
  - Markdown：回声 body 中有 `type="MD"`，并且 header 兼容字段显示 Markdown。
  - 原生 AT：回声 body 中有 `type="AT"`；header `at` 可作为辅助证据，不作为唯一判断依据。
  - Reply：回声 body 中有 `type="replyData"`。
  - 图片：回声 body 中有 `type="IMAGE"`。
- 私聊没有自动回声，必须区分两个层级：
  - 接口接受：API 返回 `ok=true` 和 `msgkey`。
  - 客户端语义成功：收件人侧确认格式、图片、链接或 reply 展示符合预期。

可复用脚本：

- `python scripts/sim/probe_group_formats.py --group 4507088 --user chengbo05`
- `python scripts/sim/probe_group_links.py --group 4507088 --user chengbo05`
- `python scripts/sim/probe_private_formats.py --user chengbo05`
- `python scripts/sim/probe_contract_edges.py --group 4507088 --user chengbo05`
- `python scripts/sim/probe_reply_counts.py --group 4507088 --include-private --private-user chengbo05`

脚本说明见 `scripts/sim/README.md`。群聊脚本会读取本机 `~/.hermes/logs/gateway.log` 和 `agent.log` 里的 webhook 回声；私聊脚本会在消息正文里写入 Pxx 编号和中文期望，需要收件人按编号人工确认客户端展示。

## 核心结论

群消息：

- 群 endpoint 使用 `message.header + message.body[]` 协议，大写 `msgtype` 和大写 body `type`。
- 群出站大小写严格：`msgtype="text"` 会失败为 `msgtype text not support`；body `type="text"` 或 `type="link"` 会失败为 `type not suport`。
- 回声里常见的 `header.msgtype="MIXED"` 不是合法出站 `msgtype`；出站使用 `MIXED` 会失败为 `msgtype MIXED not support`。
- `message.header.msgtype` 和 `message.body[].type` 必须匹配对应协议族；本轮实测的 MD/TEXT 错配均被接口拒绝。
- `msgtype="MD"` 时，正文必须使用 `type="MD"`；只发 `type="TEXT"` 会失败。
- `msgtype="TEXT"` 时，正文必须使用 `type="TEXT"`；发 `type="MD"` 会失败。
- `msgtype="IMAGE"` 时，图片 packet 内的文字必须使用 `TEXT`，不能使用 `MD`。
- `TEXT + AT` 不需要正文占位；只要有正确的 `AT` body item 即可。
- `TEXT + AT` 可以没有正文；AT-only 群消息已验证会保留原生 AT。
- `MD + 人类/机器人 AT` 必须在 `MD.content` 里放对应 `@` 占位。人类 AT 缺占位或使用展示名占位时，send API 实测仍返回成功，但回声丢失原生 AT；机器人 AT 的成功样例使用 `@<agentId>` 占位。
- `MD + atall=true` 比较特殊：无正文占位时回声实测仍保留原生 `AT`，但正文不显示 `@全体成员`；工具层仍应补占位，保证可见文本和通知语义一致。
- 一个 `AT` item 同时包含 `atuserids` 和 `atagentids` 时可用，服务端回声会拆成多条 `AT` body item；回声里的机器人字段是 `robotid/atrobotids`，不是出站用的 `agentId`。
- `atall=true` 和具体用户/机器人放在同一个 `AT` item 时，服务只保留 @all，具体对象不会成为原生 AT。
- `TEXT`/`IMAGE` packet 中如需同时原生 @all 和原生 @具体用户/机器人，应拆成多个 `AT` item；实测可同时保留 `atall` 与 `atuserids` 或 `atrobotids`。
- `MD` packet 中如同时出现 @all 和具体用户/机器人，可靠契约是只让 @all 原生生效，具体 `@user/@agentId` 留在 `MD.content` 中按普通文本展示；拆成多个 `AT` item 会被服务拒绝为 `msg body wrong, markdown count exceed limit`。
- 群 reply 可靠格式是 `TEXT + reply`；`MD + reply` send API 会成功，但回声不含 `replyData`，即 reply 语义失败。
- 群 `message.reply` 必须是单个 object；即使外层 `msgtype="TEXT"`、body 使用 `TEXT`，`message.reply` 传数组 2 条或 3 条也会被接口拒绝为 `请求参数错误`。
- 群 `message.reply.preview` 不是保留 `replyData` 的必要字段：只传 `messageid` 或 `preview=""` 均已回声确认保留 `replyData`。不传 preview 时回声预览为通用文案 `你收到一条消息，点击跳转查看消息原文`。
- 群 `message.reply.messageid` 会被服务校验；错误 `messageid` 无论是否带 preview，均失败为 `请求参数错误`。
- 群 `message.reply.imid` 用于客户端引用卡片的 `Reply <name>:` 身份显示，字段值应为被引用消息发送者 imid，通常来自原消息 webhook `fromid`。不要用当前机器人 imid 兜底；只有被引用消息本身由当前机器人发送时，当前机器人 imid 才是正确值。
- 群 reply preview 展示上限为 100 字符：请求可传更长，但回声/客户端展示会截为前 100 字 + `...`。发送层自动补 preview 时应按 100 字符生成，避免预期和展示不一致。
- 群 `TEXT + reply + AT` 已验证可同时保留 `replyData` 和原生 AT。
- 群 `IMAGE + reply` 已验证可保留 `replyData` 和 `IMAGE`；图片 packet 内文字仍必须使用 `TEXT`。
- 群 `IMAGE + AT + TEXT + IMAGE` 已验证可同时保留原生 AT 和图片。
- 群 `IMAGE` packet 不强制要求 `TEXT` 正文：`AT + IMAGE`、`LINK + IMAGE`、`reply + IMAGE`、`reply + AT + LINK + IMAGE` 已回声验证可用；纯 `IMAGE` send API 返回成功，但本机未捕获到可匹配 `[iflow:raw]` 回声，不能按回声验明契约处理。
- 群原生链接使用 body item `type="LINK"`，出站必须传 `href`。只传 `label` 会被接口拒绝为 `link pattern wrong`。
- 群 `LINK` 在非图片消息中使用 `msgtype="TEXT"`；`MD + LINK` 会失败为 `md pattern wrong`。
- 群 `LINK` 可与 `TEXT`、多链接、原生 AT、@all、reply 同包共存；纯 `LINK`、`AT + LINK`、`reply + LINK` 不要求额外正文。
- 群 `IMAGE` packet 内也可携带 `LINK`，实测 `reply + AT + TEXT + LINK + IMAGE` 能同时保留 `replyData`、原生 AT、链接和图片。

私聊 App 消息：

- 私聊 app endpoint 使用顶层 payload 协议，不使用群消息 `message.body[]`。
- 私聊 `msgtype` 使用小写枚举，并且必须和同名内容对象匹配：`text/text`、`md/md`、`richtext/richtext`、`image/image`。
- 私聊顶层大小写严格：`msgtype="TEXT"` 会失败为 `msgTypeIsWrong`；`msgtype="text"` 但内容对象写成 `Text` 会失败为 `请求参数错误`。
- 私聊纯文本、Markdown、richtext 链接、200x200 PNG 图片均已通过客户端人工验收。
- `msgtype=text` 却只传 `md` 对象会失败；`msgtype=md` 却只传 `text` 对象会失败。
- 私聊 `text + reply[]` 已通过客户端人工验收。
- 私聊 `md + reply[]` API 会返回成功，Markdown 也会渲染，但客户端不展示 reply 内容，语义失败。
- 私聊 `image + reply[]` 已通过客户端人工验收，200x200 PNG 图片和 reply 都能展示。
- 私聊 `richtext + reply[]` 已通过客户端人工验收，reply 和可点击链接都能展示。
- 私聊 link-only `richtext.content=[{"type":"a", ...}]` 已通过客户端人工验收：无 reply 时整条消息是一条超链；带 `reply[]` 时显示引用结构，引用后的整行文字是可点击超链。
- 私聊 `richtext` 双链接、空文本 `text + reply[]`、两个 `reply[]` target、`text + 3/5 条 reply[]`、`link-only richtext + 5 条 reply[]` 已通过客户端人工验收。本文档只覆盖本轮验证的两个链接和 5 条以内 reply target，不外推到任意数量。
- 私聊 `reply[]` item 省略 `content` 或传 `content=""`，API 接受且客户端引用正常展示。
- 私聊错误 `reply[].msgid` 不会被 API 拒绝；错误 msgid + content、错误 msgid 且无 content 均返回成功。客户端正文正常展示，reply 区域显示错误态。

## 覆盖矩阵

这里的“可用”表示已经通过真实发送验证，并且群聊已经用 webhook 回声确认语义，私聊已经由客户端人工验收确认展示。只有 API 返回成功、但没有回声或人工验收的项目，不归入“可用”。

| 场景 | 外层 `msgtype` | 内容结构 | 已验证可用组合 | 明确不可用或不应依赖 |
|---|---|---|---|---|
| 群 Markdown | `MD` | body `MD`，可选一个 `AT` item | 纯 MD；MD + 人类 AT；MD + 机器人 AT；MD + 人类/机器人合并 AT；MD + @all；MD 中 @all + 具体对象文本。AT 占位规则见“MD 中的 AT 与占位”。 | `MD + reply` 会丢 reply；`MD + LINK` 失败；`MD` header + `TEXT` body 失败；`MD` 不可放进 `IMAGE` packet；MD 中 @all 原生生效时，具体用户/机器人只能按普通文本展示，不能同时形成原生 AT。 |
| 群纯文本 | `TEXT` | body 可含 `TEXT`、一个或多个 `AT`、`LINK`，可带 `message.reply` | 纯 TEXT；TEXT + AT；AT-only；TEXT + reply；reply-only 空 TEXT；TEXT + reply + AT；纯 LINK；AT + LINK；reply + LINK；reply + AT + LINK；@all-only；@all + LINK；reply + @all + LINK；@all 和具体用户/机器人拆分 AT item。 | lowercase `msgtype/body.type` 失败；出站 `MIXED` 失败；inline `@xxx` 只是文本；`LINK` 缺 `href` 失败；@all 与具体用户/机器人合并到同一个 AT item 会丢具体用户/机器人。 |
| 群图片 | `IMAGE` | body `IMAGE`，可选 `TEXT`、`AT`、`LINK`，可带 `message.reply` | TEXT + IMAGE；TEXT + IMAGE + TEXT；reply + TEXT + IMAGE；AT + TEXT + IMAGE；AT + IMAGE；AT + LINK + IMAGE；LINK + IMAGE；reply + IMAGE；reply + AT + IMAGE；reply + AT + LINK + IMAGE；@all + IMAGE；@all + LINK + IMAGE；reply + @all + LINK + IMAGE；@all 和具体用户/机器人拆分 AT item + IMAGE。 | 图片 packet 内文本不能用 `MD`；纯 `IMAGE` API 接受但本地未捕获可匹配 `[iflow:raw]`，暂不作为回声验明的语义契约；@all 与具体用户/机器人合并到同一个 AT item 会丢具体用户/机器人。 |
| 私聊文本 | `text` | 顶层 `text`，可选顶层 `reply[]` | 纯 text；text + reply；空 text + reply-only；text + 两个 reply targets；text + 3/5 条 reply targets。 | `msgtype=TEXT` 失败；`msgtype=text` 但对象 key 为 `Text` 失败。 |
| 私聊 Markdown | `md` | 顶层 `md` | 纯 md。 | `md + reply[]` API 成功但客户端不展示 reply；`msgtype=md` 但只传 `text` 对象失败。 |
| 私聊富文本链接 | `richtext` | 顶层 `richtext.content[]`，item `type` 小写 `text`/`a` | text + link；text + 两个 links；richtext + reply；link-only；link-only + reply；link-only + 两个 reply targets；link-only + 5 条 reply targets。 | 大写 item `type="A"` 仅 API 接受，客户端展示未确认；工具层不要依赖大写 item type。 |
| 私聊图片 | `image` | 顶层 `image`，可选顶层 `reply[]` | 200x200 PNG 图片；200x200 PNG 图片 + reply。 | 其它图片尺寸或格式未覆盖。 |

## 基础文本与 Markdown

| Case | 请求形态 | 发送结果 | 回声结果 | 结论 |
|---|---|---|---|---|
| `md_plain` | `msgtype=MD`, body `MD` | 成功，mid `1866419773746830782` | header `msgtype=""`, `compatible="收到一条Markdown消息"`, body `["MD"]` | Markdown 正确格式。 |
| `text_plain` | `msgtype=TEXT`, body `TEXT` | 成功，mid `1866419774301527487` | header `msgtype="MIXED"`, body `["TEXT"]` | 纯文本正确格式，Markdown 标记按字面量处理。 |
| `md_header_text_body` | `msgtype=MD`, body `TEXT` | 失败 | error `msg body wrong, markdown is empty` | `MD` header 下不能只发 `TEXT` body。 |
| `text_header_md_body` | `msgtype=TEXT`, body `MD` | 失败 | error `type not suport` | `TEXT` header 下不能发 `MD` body。 |
| `lower_header_text_body_upper` | `msgtype=text`, body `TEXT` | 失败 | error `msgtype text not support` | 群出站 `msgtype` 大小写敏感，不能用小写。 |
| `upper_header_lower_text_body` | `msgtype=TEXT`, body `text` | 失败 | error `type not suport` | 群出站 body `type` 大小写敏感，不能用小写。 |
| `mixed_header_text_body` | `msgtype=MIXED`, body `TEXT` | 失败 | error `msgtype MIXED not support` | `MIXED` 是回声 header 常见值，不是合法出站值。 |

请求示例：

```json
{
  "message": {
    "header": {"msgtype": "MD", "totype": "GROUP"},
    "body": [
      {"type": "MD", "content": "**Markdown**\n\n- item"}
    ]
  }
}
```

## TEXT 中的 AT

`TEXT` 场景下，原生 AT 由 `AT` body item 表达，正文中不需要 `@xxx` 占位。

| Case | 请求形态 | 发送结果 | 回声结果 | 结论 |
|---|---|---|---|---|
| `text_at_struct` | `msgtype=TEXT`, body `AT(atuserids=["chengbo05"]) + TEXT` | 成功，mid `1866419775603858880` | header `at.atuserids=["chengbo05"]`, body `["AT", "TEXT"]` | TEXT 原生人类 AT 正确格式。 |
| `text_inline_no_at` | `msgtype=TEXT`, body `TEXT("@chengbo05 ...")` | 成功，mid `1866419776176381377` | body `["TEXT"]`, 无 header `at` | inline `@chengbo05` 只是文本，不会触发原生 AT。 |
| `text_at_wrong_userid_field` | `msgtype=TEXT`, body `AT(userid="chengbo05") + TEXT` | 失败 | error `at pattern wrong` | 发送请求不能使用回声里的 `userid` 字段；必须使用 `atuserids`。 |
| `text_atagent_struct_other` | `msgtype=TEXT`, body `AT(atagentids=[17212]) + TEXT` | 成功，mid `1866419901326024137` | body `["AT", "TEXT"]`, AT echo 为 `robotid=4105001326`, `name="地图不打烊"` | TEXT 原生机器人 AT 正确格式。 |
| `text_atagent_struct_self` | `msgtype=TEXT`, body `AT(atagentids=[6471]) + TEXT` | 失败 | error `被@机器人不能包含自身` | 如流拒绝机器人 @ 自己；工具层应跳过 self agent。 |
| `text_atall_struct` | `msgtype=TEXT`, body `AT(atall=true) + TEXT` | 成功，mid `1866420166564371917` | header `at.atall=true`, body `["AT", "TEXT"]` | TEXT 原生 @all 正确格式。 |
| `text_inline_atall_no_at` | `msgtype=TEXT`, body `TEXT("@all ...")` | 成功，mid `1866420167209246158` | body `["TEXT"]`, 无 header `at` | inline `@all` 只是文本，不会触发原生 @all。 |
| `text_at_only_user` | `msgtype=TEXT`, body `AT(atuserids=["chengbo05"])` | 成功，mid `1866423496649858537` | header `at.atuserids=["chengbo05"]`, body `["AT"]` | AT-only 群消息可用，不要求正文。 |
| `text_at_user_agent_combined` | `msgtype=TEXT`, body 一个 `AT(atuserids=["chengbo05"], atagentids=[17212]) + TEXT` | 成功，mid `1866424669529300504` | header 含 `atuserids=["chengbo05"]` 和 `atrobotids=[4105001326]`, body `["AT","AT","TEXT"]` | 人类和机器人可放同一个出站 `AT` item，回声会拆成两个 `AT` item。 |
| `text_atall_only` | `msgtype=TEXT`, body `AT(atall=true)` | 成功，mid `1866424678218849838` | header `at.atall=true`, body `["AT"]` | @all-only 群消息可用，不要求正文。 |
| `text_atall_user_combined` | `msgtype=TEXT`, body 一个 `AT(atall=true, atuserids=["chengbo05"]) + TEXT` | 成功，mid `1866426068974558787` | header 只有 `atall=true`, body `["AT","TEXT"]` | 语义部分失败：同一个 `AT` item 中 @all 会吞掉具体用户 AT。 |
| `text_atall_user_separate` | `msgtype=TEXT`, body `AT(atall=true) + AT(atuserids=["chengbo05"]) + TEXT` | 成功，mid `1866426069780913732` | header 同时有 `atall=true` 和 `atuserids=["chengbo05"]`, body `["AT","AT","TEXT"]` | TEXT 下 @all 与具体用户必须拆成多个 `AT` item 才能同时保留。 |
| `text_atall_agent_combined` | `msgtype=TEXT`, body 一个 `AT(atall=true, atagentids=[17212]) + TEXT` | 成功，mid `1866426630489103977` | header 只有 `atall=true`, body `["AT","TEXT"]` | 语义部分失败：同一个 `AT` item 中 @all 会吞掉机器人 AT。 |
| `text_atall_agent_separate` | `msgtype=TEXT`, body `AT(atall=true) + AT(atagentids=[17212]) + TEXT` | 成功，mid `1866426631293361770` | header 同时有 `atall=true` 和 `atrobotids=[4105001326]`, body `["AT","AT","TEXT"]` | TEXT 下 @all 与机器人必须拆成多个 `AT` item 才能同时保留。 |

请求字段规则：

- 人类用户：`{"type": "AT", "atuserids": ["<uuapName>"]}`
- 机器人：`{"type": "AT", "atagentids": [<agentId>]}`，不能包含当前机器人自身 agentId。
- 全员：使用 `{"type": "AT", "atall": true}`。
- 人类 + 机器人：可合并为一个 `AT` item，例如 `{"type":"AT","atuserids":["chengbo05"],"atagentids":[17212]}`。
- @all + 具体人/机器人：TEXT/IMAGE packet 中要拆成多个 `AT` item，才能同时保留原生 @all 和原生具体对象。MD packet 中只能保留 @all 原生，具体 `@user/@agentId` 按普通正文展示；拆多个 `AT` item 会失败。
- 回声里的 `userid`、`robotid`、`name` 是如流转换后的入站字段，不是出站请求字段。

TEXT + AT 示例：

```json
{
  "message": {
    "header": {"msgtype": "TEXT", "totype": "GROUP"},
    "body": [
      {"type": "AT", "atuserids": ["chengbo05"]},
      {"type": "TEXT", "content": "正文"}
    ]
  }
}
```

## MD 中的 AT 与占位

`MD + AT` 支持原生 AT。人类和机器人 AT 的 `MD.content` 必须包含和 `AT` item 对应的 `@` 占位；占位用“请求侧 ID”，不是回声中的展示名。人类 AT 缺占位或使用展示名占位时，send API 实测成功但回声丢失原生 AT；机器人 AT 正向样例使用 `@<agentId>` 占位成功。`atall=true` 即使缺少占位也能保留原生 AT，但推荐仍补占位，避免正文里看不到 @all。

| Case | 请求形态 | 发送结果 | 回声结果 | 结论 |
|---|---|---|---|---|
| `md_at_uid_placeholder` | `msgtype=MD`, body `AT(atuserids=["chengbo05"]) + MD("@chengbo05 ...")` | 成功，mid `1866419777064525250` | header `at.atuserids=["chengbo05"]`, body `["AT", "MD"]`, MD 回声中 `@chengbo05` 被替换成 `@成博` | 人类用户 MD AT 正确格式。 |
| `md_at_no_placeholder` | `msgtype=MD`, body `AT(atuserids=["chengbo05"]) + MD("...")` | 发送成功，mid `1866419777650679235` | body 只有 `["MD"]`，无 header `at` | 语义失败：缺少占位会丢失原生 AT。 |
| `md_at_name_placeholder` | `msgtype=MD`, body `AT(atuserids=["chengbo05"]) + MD("@成博 ...")` | 发送成功，mid `1866419778203278788` | body 只有 `["MD"]`，无 header `at` | 语义失败：展示名占位不生效。 |
| `md_at_after_md_uid_placeholder` | `msgtype=MD`, body `MD("@chengbo05 ...") + AT(atuserids=["chengbo05"])` | 成功，mid `1866419778757975493` | body `["AT", "MD"]` | AT item 可在 MD 后面，但推荐请求中放在 MD 前面，便于维护。 |
| `md_atagent_placeholder_other` | `msgtype=MD`, body `AT(atagentids=[17212]) + MD("@17212 ...")` | 成功，mid `1866419901979286986` | body `["AT", "MD"]`, MD 回声中 `@17212` 被替换成 `@地图不打烊` | 机器人 MD AT 正确格式。 |
| `md_atagent_no_placeholder` | `msgtype=MD`, body `AT(atagentids=[17212]) + MD("...")` | 发送成功，mid `1866421416378883543` | body 只有 `["MD"]`，无 header `at` | 语义失败：机器人 AT 缺少 `@<agentId>` 占位也会丢失原生 AT。 |
| `md_atagent_placeholder_self` | `msgtype=MD`, body `AT(atagentids=[6471]) + MD("@6471 ...")` | 失败 | error `被@机器人不能包含自身` | 如流拒绝机器人 @ 自己。 |
| `md_at_user_agent_combined` | `msgtype=MD`, body 一个 `AT(atuserids=["chengbo05"], atagentids=[17212]) + MD("@chengbo05 @17212 ...")` | 成功，mid `1866424671124184604` | header 同时含 `atuserids` 和 `atrobotids`, body `["AT","AT","MD"]` | MD 下人类和机器人可合并在一个出站 `AT` item，前提是 MD 正文里同时有 `@uuapName` 和 `@agentId` 占位。 |
| `md_atall_placeholder_all` | `msgtype=MD`, body `AT(atall=true) + MD("@all ...")` | 成功，mid `1866420167799594447` | header `at.atall=true`, body `["AT", "MD"]`, 回声正文显示 `@全体成员 ...` | MD @all 推荐格式。 |
| `md_atall_placeholder_cn` | `msgtype=MD`, body `AT(atall=true) + MD("@所有人 ...")` | 成功，mid `1866420168405671376` | header `at.atall=true`, body `["AT", "MD"]` | `@所有人` 可作为 @all 占位。 |
| `md_atall_placeholder_caps` | `msgtype=MD`, body `AT(atall=true) + MD("@ALL ...")` | 成功，mid `1866420169003359697` | header `at.atall=true`, body `["AT", "MD"]` | `@ALL` 可作为 @all 占位。 |
| `md_atall_no_placeholder` | `msgtype=MD`, body `AT(atall=true) + MD("...")` | 成功，mid `1866420169625165266` | header `at.atall=true`, body `["AT", "MD"]` | @all 特例：无占位仍保留原生 AT，但正文不显示 @all。 |
| `md_atall_user_combined` | `msgtype=MD`, body 一个 `AT(atall=true, atuserids=["chengbo05"]) + MD("@all @chengbo05 ...")` | 发送成功，mid `1866426070566297157`；复测 mid `1866438197195693717` | header 只有 `atall=true`, body `["AT","MD"]`，正文里 `@chengbo05` 仍只是文本 | MD 可保持 Markdown 和原生 @all；具体用户不会成为原生 AT，只按普通文本展示。 |
| `md_atall_user_separate` | `msgtype=MD`, body `AT(atall=true) + AT(atuserids=["chengbo05"]) + MD("@all @chengbo05 ...")` | 失败 | error `msg body wrong, markdown count exceed limit` | MD 下不能靠拆多个 `AT` item 同时表达 @all 和具体用户。 |
| `md_atall_agent_combined` | `msgtype=MD`, body 一个 `AT(atall=true, atagentids=[17212]) + MD("@all @17212 ...")` | 发送成功，mid `1866426632150048363` | header 只有 `atall=true`, body `["AT","MD"]`，正文里 `@17212` 仍只是文本 | MD 可保持 Markdown 和原生 @all；机器人不会成为原生 AT，只按普通文本展示。 |
| `md_atall_agent_separate` | `msgtype=MD`, body `AT(atall=true) + AT(atagentids=[17212]) + MD("@all @17212 ...")` | 失败 | error `msg body wrong, markdown count exceed limit` | MD 下不能靠拆多个 `AT` item 同时表达 @all 和机器人。 |

占位格式：

- 人类用户占位：`@<uuapName>`，例如 `@chengbo05`。
- 机器人占位：`@<agentId>`，例如 `@17212`。
- 展示名占位，例如 `@成博`，不会触发原生 AT。
- @all 占位：推荐 `@all`；实测 `@所有人`、`@ALL` 也可用。
- 人类/机器人占位必须出现在 `MD.content` 中。只放 `AT` body item 不够。
- @all 是特例：只放 `AT(atall=true)` 也会保留原生 @all，但建议仍在正文里放 `@all`，让用户能看到通知对象。
- MD 下人类 + 机器人可以放同一个 `AT` item。MD 下 @all + 具体用户/机器人时只保留 @all 原生；具体对象按普通文本展示，拆多个 `AT` item 会失败。

推荐 MD + AT 请求示例：

```json
{
  "message": {
    "header": {"msgtype": "MD", "totype": "GROUP"},
    "body": [
      {"type": "AT", "atuserids": ["chengbo05"]},
      {"type": "MD", "content": "@chengbo05 **正文 Markdown**"}
    ]
  }
}
```

机器人示例：

```json
{
  "message": {
    "header": {"msgtype": "MD", "totype": "GROUP"},
    "body": [
      {"type": "AT", "atagentids": [17212]},
      {"type": "MD", "content": "@17212 **正文 Markdown**"}
    ]
  }
}
```

@all 示例：

```json
{
  "message": {
    "header": {"msgtype": "MD", "totype": "GROUP"},
    "body": [
      {"type": "AT", "atall": true},
      {"type": "MD", "content": "@all **正文 Markdown**"}
    ]
  }
}
```

## Reply

群 reply 的可用格式是 `TEXT + reply`。`MD + reply` 在发送接口层返回成功，但回声不包含 `replyData`，因此不能认为 reply 成功。

| Case | 请求形态 | 发送结果 | 回声结果 | 结论 |
|---|---|---|---|---|
| `reply_text` | `msgtype=TEXT`, body `TEXT`, 带 `message.reply` | 成功，mid `1866419779419626950` | body `["replyData", "TEXT", "TEXT"]` | 群 reply 正确格式。 |
| `reply_text_at_user` | `msgtype=TEXT`, body `AT(atuserids=["chengbo05"]) + TEXT`, 带 `message.reply` | 成功，mid `1866421414092987860` | body `["replyData", "TEXT", "AT", "TEXT"]`, header `at.atuserids=["chengbo05"]` | `TEXT + reply + AT` 可同时保留 reply 和原生 AT。 |
| `reply_md` | `msgtype=MD`, body `MD`, 带 `message.reply` | 发送成功，mid `1866419779983760839` | body 只有 `["MD"]`，无 `replyData` | 语义失败：Markdown 渲染成功，但 reply 丢失。 |
| `reply_text_no_imid` | `msgtype=TEXT`, body `TEXT`, `message.reply` 不带 `imid` | 成功，mid `1866423497307315690` | body `["replyData","TEXT","TEXT"]` | `imid` 不是保留 replyData 的必要字段。 |
| `reply_empty_text` | `msgtype=TEXT`, body `TEXT(content="")`, 带 `message.reply` | 成功，mid `1866423497945898475` | body `["replyData","TEXT","TEXT"]`，最后一个 TEXT 为空 | 群 reply-only 可用，使用空 TEXT body 承载 reply。 |
| `replytype_1_text` | `msgtype=TEXT`, body `TEXT`, `message.reply.replytype="1"` | 成功，mid `1866423498603355628` | body `["replyData","TEXT","TEXT"]` | `replytype="1"` 可保留 `replyData`。 |
| `replytype_2_text` | `msgtype=TEXT`, body `TEXT`, `message.reply.replytype="2"` | 成功，mid `1866423499239841261` | body `["replyData","TEXT","TEXT"]` | `replytype="2"` 可保留 `replyData`。 |
| `text_single_reply_object` | `msgtype=TEXT`, body `TEXT`, `message.reply` 为单个 object | 成功，mid `1866429465351020160` | body `["replyData","TEXT","TEXT"]` | 群 TEXT 单 reply object 是正确格式。 |
| `text_reply_array_2` | `msgtype=TEXT`, body `TEXT`, `message.reply` 为 2 个 object 的数组 | 失败 | error `请求参数错误` | 群 `message.reply` 不支持数组，即使是 TEXT packet。 |
| `text_reply_array_3` | `msgtype=TEXT`, body `TEXT`, `message.reply` 为 3 个 object 的数组 | 失败 | error `请求参数错误` | 群 `message.reply` 不支持多 reply。 |
| `reply_no_preview` | `msgtype=TEXT`, body `TEXT`, `message.reply={"messageid": valid_mid}` | 成功，mid `1866432981937282691` | body `["replyData","TEXT","TEXT"]` | 群 preview 可省略；仍保留 `replyData`。 |
| `reply_empty_preview` | `msgtype=TEXT`, body `TEXT`, `message.reply.preview=""` | 成功，mid `1866432982683868804` | body `["replyData","TEXT","TEXT"]` | 群 preview 可为空；仍保留 `replyData`。 |
| `reply_wrong_messageid_with_preview` | 错误 `messageid` + preview | 失败 | error `请求参数错误` | 群服务校验 `messageid`。 |
| `reply_wrong_messageid_no_preview` | 错误 `messageid`，不传 preview | 失败 | error `请求参数错误` | 群服务校验 `messageid`，与 preview 无关。 |
| `G-RID-03-SENDER-FROMID` | `message.reply.imid="1744775667"`，即被引用消息发送者 imid，来源为原消息 `fromid` | 成功，mid `1866486049234083576` | 客户端人工确认引用卡片前缀正确显示 `Reply chengbo05:` | `imid` 身份语义是被引用者 sender imid。 |
| `G-RID-01/02` | 不传 `imid` 或用当前机器人 imid 兜底 | 成功 | 客户端人工确认不能正确显示被引用者 | 不要用当前机器人 imid 兜底，除非被引用消息本身由当前机器人发送。 |
| `reply_preview_100` | `preview` 为 100 个字符 | 成功 | 回声完整保留 100 字符 | 群 reply 预览展示上限至少到 100 字符。 |
| `reply_preview_101+` | `preview` 超过 100 个字符 | 成功，12000 字请求也接受 | 回声展示前 100 字符 + `...` | 群 reply preview 展示上限是 100 字符，按字符计数，不按字节计数。 |

Reply 请求要求：

- `reply` block 位于 `message` 内，与 `header`、`body` 同级。
- `reply` block 必须是单个 object，不能是数组。`msgtype=TEXT` 下传数组 2 条和 3 条均已验证失败。
- `reply.messageid` 是被回复消息 ID；服务会校验该 ID，错误 ID 失败为 `请求参数错误`。
- `reply.preview` 是引用预览文本；可省略或传空字符串，仍保留 `replyData`。省略 preview 时，回声中的预览使用通用文案。
- `reply.preview` 请求可传很长，但客户端/回声展示只保留前 100 字符，超长追加 ASCII `...`；发送层自动生成 preview 时应按 100 字符截断。
- `reply.imid` 可省略；实测不带 `imid` 仍会保留 `replyData`。但该字段影响客户端引用卡片的 `Reply <name>:` 前缀，正确值是被引用消息发送者 imid，通常来自原消息 webhook `fromid`；不要用当前机器人 imid 兜底。
- `replytype` 可省略；实测 `replytype="1"` 和 `replytype="2"` 都会保留 `replyData`。本文档只证明 webhook 语义保留，不区分客户端上“回复/引用”的视觉差异。
- `body` 使用 `TEXT`，`header.msgtype` 使用 `TEXT`。
- 只发送 reply 时，已验证的格式是给一个 `TEXT` item，并将 `content` 置空；没有测试省略 body 的行为。

示例：

```json
{
  "message": {
    "header": {"msgtype": "TEXT", "totype": "GROUP"},
    "body": [
      {"type": "TEXT", "content": "reply body"}
    ],
    "reply": {
      "messageid": "quoted-message-id",
      "preview": "quoted preview",
      "imid": "quoted-sender-imid"
    }
  }
}
```

## 图片

图片 packet 使用 `msgtype="IMAGE"`。图文混排时，图片前后的文字 body item 必须使用 `TEXT`；补充测试中的图片为 200x200 纯蓝 PNG。

| Case | 请求形态 | 发送结果 | 回声结果 | 结论 |
|---|---|---|---|---|
| `image_text_image_text` | `msgtype=IMAGE`, body `TEXT + IMAGE + TEXT` | 成功，mid `1866419828244471240` | body `["TEXT", "IMAGE", "TEXT"]` | 图文混排正确格式。 |
| `image_only` | `msgtype=IMAGE`, body `IMAGE` | 成功，mid `1866424312097005063` | 未捕获可匹配 `[iflow:raw]` 回声 | API 接受纯图片，但展示/回声语义未在本机验明；不要把它归为回声确认契约。 |
| `reply_image_text_image` | `msgtype=IMAGE`, body `TEXT + IMAGE`, 带 `message.reply` | 成功，mid `1866421414883614165` | body `["replyData", "TEXT", "TEXT", "IMAGE"]` | `IMAGE + reply` 可保留 reply 和图片。 |
| `reply_image_only` | `msgtype=IMAGE`, body `IMAGE`, 带 `message.reply` | 成功，mid `1866424314635607564` | body `["replyData","TEXT","IMAGE"]` | 无正文时，`reply + IMAGE` 仍可保留 reply 和图片。 |
| `image_at_text_image` | `msgtype=IMAGE`, body `AT(atuserids=["chengbo05"]) + TEXT + IMAGE` | 成功，mid `1866421415655366102` | header `at.atuserids=["chengbo05"]`, body `["AT", "TEXT", "IMAGE"]` | `IMAGE` packet 可携带原生 AT；文字仍用 `TEXT`。 |
| `at_image_only` | `msgtype=IMAGE`, body `AT(atuserids=["chengbo05"]) + IMAGE` | 成功，mid `1866424312939011593` | header `at.atuserids=["chengbo05"]`, body `["AT","IMAGE"]` | 无正文时，`AT + IMAGE` 仍可保留原生 AT 和图片。 |
| `image_atall_user_combined` | `msgtype=IMAGE`, body 一个 `AT(atall=true, atuserids=["chengbo05"]) + IMAGE` | 成功，mid `1866426520290057814` | header 只有 `atall=true`, body `["AT","IMAGE"]` | 语义部分失败：同一个 `AT` item 中 @all 会吞掉具体用户 AT，IMAGE 场景同样如此。 |
| `image_atall_user_separate` | `msgtype=IMAGE`, body `AT(atall=true) + AT(atuserids=["chengbo05"]) + IMAGE` | 成功，mid `1866426521164570199` | header 同时有 `atall=true` 和 `atuserids=["chengbo05"]`, body `["AT","AT","IMAGE"]` | IMAGE 下 @all 与具体用户必须拆成多个 `AT` item 才能同时保留。 |
| `image_atall_agent_combined` | `msgtype=IMAGE`, body 一个 `AT(atall=true, atagentids=[17212]) + IMAGE` | 成功，mid `1866426639579209332` | header 只有 `atall=true`, body `["AT","IMAGE"]` | 语义部分失败：同一个 `AT` item 中 @all 会吞掉机器人 AT，IMAGE 场景同样如此。 |
| `image_atall_agent_separate` | `msgtype=IMAGE`, body `AT(atall=true) + AT(atagentids=[17212]) + IMAGE` | 成功，mid `1866426640473644661` | header 同时有 `atall=true` 和 `atrobotids=[4105001326]`, body `["AT","AT","IMAGE"]` | IMAGE 下 @all 与机器人必须拆成多个 `AT` item 才能同时保留。 |
| `link_image_only` | `msgtype=IMAGE`, body `LINK + IMAGE` | 成功，mid `1866424309817400834` | body `["LINK","IMAGE"]` | 无正文时，链接和图片可同包。 |
| `at_link_image_only` | `msgtype=IMAGE`, body `AT + LINK + IMAGE` | 成功，mid `1866424672921443872` | header `at.atuserids=["chengbo05"]`, body `["AT","LINK","IMAGE"]` | 无正文时，AT、链接、图片可同包。 |
| `atall_image_only` | `msgtype=IMAGE`, body `AT(atall=true) + IMAGE` | 成功，mid `1866424682572537396` | header `at.atall=true`, body `["AT","IMAGE"]` | 无正文时，@all 和图片可同包。 |
| `atall_link_image_only` | `msgtype=IMAGE`, body `AT(atall=true) + LINK + IMAGE` | 成功，mid `1866424683415592501` | header `at.atall=true`, body `["AT","LINK","IMAGE"]` | 无正文时，@all、链接、图片可同包。 |
| `reply_at_image_only` | `msgtype=IMAGE`, body `AT + IMAGE`, 带 `message.reply` | 成功，mid `1866424677437660716` | body `["replyData","TEXT","AT","IMAGE"]` | 无正文时，reply、AT、图片可同包。 |
| `reply_at_link_image_only` | `msgtype=IMAGE`, body `AT + LINK + IMAGE`, 带 `message.reply` | 成功，mid `1866424312381169160` | body `["replyData","TEXT","AT","LINK","IMAGE"]` | 无正文时，reply、AT、链接、图片可同包。 |
| `reply_atall_link_image_only` | `msgtype=IMAGE`, body `AT(atall=true) + LINK + IMAGE`, 带 `message.reply` | 成功，mid `1866424684260744758` | body `["replyData","TEXT","AT","LINK","IMAGE"]` | 无正文时，reply、@all、链接、图片可同包。 |
| `image_md_image` | `msgtype=IMAGE`, body `MD + IMAGE` | 失败 | error `type not suport` | IMAGE packet 内不能使用 MD 文本。 |

示例：

```json
{
  "message": {
    "header": {"msgtype": "IMAGE", "totype": "GROUP"},
    "body": [
      {"type": "TEXT", "content": "before"},
      {"type": "IMAGE", "content": "<base64 image>"},
      {"type": "TEXT", "content": "after"}
    ]
  }
}
```

## 群链接 LINK

群链接不是私聊 `richtext` 协议，而是群消息 body item：`{"type": "LINK", "href": "https://..."}`。出站请求必须带 `href`；`label` 可选。回声里的 `LINK` item 只暴露 `label`，不回显 `href`：未传自定义 `label` 时，回声 `label` 等于 URL；传自定义 `label` 时，回声 `label` 等于自定义展示文本。

| Case | 请求形态 | 发送结果 | 回声结果 | 结论 |
|---|---|---|---|---|
| `link_href_only` | `msgtype=TEXT`, body `LINK(href)` | 成功，mid `1866422451071737305` | body `["LINK"]`, `LINK.label` 为 URL | 群纯链接正确格式。 |
| `link_label_only` | `msgtype=TEXT`, body `LINK(label)` | 失败 | error `link pattern wrong` | 出站 `LINK` 必须带 `href`。 |
| `link_href_label` | `msgtype=TEXT`, body `LINK(href,label)` | 成功，mid `1866422452484169178` | body `["LINK"]`, `LINK.label="G-LINK 自定义展示文本"` | 自定义 `label` 可作为展示文本。 |
| `text_link_href` | `msgtype=TEXT`, body `TEXT + LINK(href)` | 成功，mid `1866422453262212571` | body `["TEXT","LINK"]` | 正文和链接可同包。 |
| `multi_links_text` | `msgtype=TEXT`, body `TEXT + LINK + LINK` | 成功，mid `1866422529676140000` | body `["TEXT","LINK","LINK"]` | 多个链接可同包。 |
| `at_link_only` | `msgtype=TEXT`, body `AT + LINK` | 成功，mid `1866422632698732005` | body `["AT","LINK"]`, header `at.atuserids=["chengbo05"]` | 无正文时，AT 和链接仍可同包。 |
| `at_text_link_href` | `msgtype=TEXT`, body `AT + TEXT + LINK` | 成功，mid `1866422454049693148` | body `["AT","TEXT","LINK"]`, 原生 AT 保留 | AT、正文、链接可同包。 |
| `reply_link_only` | `msgtype=TEXT`, body `LINK`, 带 `message.reply` | 成功，mid `1866422633622527462` | body `["replyData","TEXT","LINK"]` | 无正文时，reply 和链接仍可同包。 |
| `reply_at_link_only` | `msgtype=TEXT`, body `AT + LINK`, 带 `message.reply` | 成功，mid `1866422634434125287` | body `["replyData","TEXT","AT","LINK"]` | reply、AT、链接可同包且不要求正文。 |
| `reply_at_text_link` | `msgtype=TEXT`, body `AT + TEXT + LINK`, 带 `message.reply` | 成功，mid `1866422530595741153` | body `["replyData","TEXT","AT","TEXT","LINK"]` | reply、AT、正文、链接可同包。 |
| `atall_link_only` | `msgtype=TEXT`, body `AT(atall=true) + LINK` | 成功，mid `1866424679508598321` | header `at.atall=true`, body `["AT","LINK"]` | 无正文时，@all 和链接可同包。 |
| `atall_text_link` | `msgtype=TEXT`, body `AT(atall=true) + TEXT + LINK` | 成功，mid `1866422532339523043` | header `at.atall=true`, body `["AT","TEXT","LINK"]` | @all 和链接可同包。 |
| `reply_atall_link_only` | `msgtype=TEXT`, body `AT(atall=true) + LINK`, 带 `message.reply` | 成功，mid `1866424681722142259` | body `["replyData","TEXT","AT","LINK"]` | 无正文时，reply、@all、链接可同包。 |
| `md_text_link_href` | `msgtype=MD`, body `MD + LINK` | 失败 | error `md pattern wrong` | 群 `LINK` 不能放进 MD packet。 |
| `at_link_image_only` | `msgtype=IMAGE`, body `AT + LINK + IMAGE` | 成功，mid `1866424672921443872` | body `["AT","LINK","IMAGE"]` | 图片 packet 中可无正文携带 AT 和链接。 |
| `link_image_only` | `msgtype=IMAGE`, body `LINK + IMAGE` | 成功，mid `1866424309817400834` | body `["LINK","IMAGE"]` | 图片 packet 中可无正文携带链接。 |
| `atall_link_image_only` | `msgtype=IMAGE`, body `AT(atall=true) + LINK + IMAGE` | 成功，mid `1866424683415592501` | body `["AT","LINK","IMAGE"]` | 图片 packet 中可无正文携带 @all 和链接。 |
| `image_text_link_image` | `msgtype=IMAGE`, body `TEXT + LINK + IMAGE` | 成功，mid `1866422456338734558` | body `["TEXT","LINK","IMAGE"]` | 图片 packet 可携带链接；文字仍用 `TEXT`。 |
| `reply_at_text_link_image` | `msgtype=IMAGE`, body `AT + TEXT + LINK + IMAGE`, 带 `message.reply` | 成功，mid `1866422531454524898` | body `["replyData","TEXT","AT","TEXT","LINK","IMAGE"]` | reply、AT、链接、图片可同包。 |
| `reply_at_link_image_only` | `msgtype=IMAGE`, body `AT + LINK + IMAGE`, 带 `message.reply` | 成功，mid `1866424312381169160` | body `["replyData","TEXT","AT","LINK","IMAGE"]` | 无正文时，reply、AT、链接、图片可同包。 |
| `reply_atall_link_image_only` | `msgtype=IMAGE`, body `AT(atall=true) + LINK + IMAGE`, 带 `message.reply` | 成功，mid `1866424684260744758` | body `["replyData","TEXT","AT","LINK","IMAGE"]` | 无正文时，reply、@all、链接、图片可同包。 |

请求示例：

```json
{
  "message": {
    "header": {"msgtype": "TEXT", "totype": "GROUP"},
    "body": [
      {"type": "TEXT", "content": "正文"},
      {"type": "LINK", "href": "https://example.com", "label": "展示文本"}
    ]
  }
}
```

带图片示例：

```json
{
  "message": {
    "header": {"msgtype": "IMAGE", "totype": "GROUP"},
    "body": [
      {"type": "TEXT", "content": "正文"},
      {"type": "LINK", "href": "https://example.com"},
      {"type": "IMAGE", "content": "<base64 image>"}
    ]
  }
}
```

## 私聊 App 消息

私聊不是群消息的 `message.header + message.body[]` 协议，而是 app message endpoint 的顶层 payload。`msgtype` 使用小写枚举，且必须和同名内容对象匹配。

测试对象：`chengbo05`。图片测试使用一张 200x200 纯蓝 PNG，准备后 mime 为 `image/png`、二进制大小 427 bytes、base64 长度 572。

### 私聊接口接受性矩阵

以下矩阵只说明 app send API 是否接受该 payload。由于私聊没有可匹配 webhook 回声，不能只根据这里判断客户端展示是否符合预期。

| Case | 请求形态 | API 结果 | 自动回声 | 结论 |
|---|---|---|---|---|
| `text_plain` | `msgtype=text`, `text.content` | 成功，msgkey `1866420386995319808` | 无可匹配 webhook 回声 | 私聊纯文本接口接受；P01 已人工验收展示成功。 |
| `md_plain` | `msgtype=md`, `md.content` | 成功，msgkey `1866420387547918336` | 无可匹配 webhook 回声 | 私聊 Markdown 接口接受；P02 已人工验收展示成功。 |
| `richtext_link` | `msgtype=richtext`, `richtext.content[]` 含 text 和 link | 成功，msgkey `1866420388088954880` | 无可匹配 webhook 回声 | 私聊 richtext 接口接受；P03 已人工验收展示成功。 |
| `image_plain` | `msgtype=image`, `image.content=<200x200 blue png base64>` | 成功，msgkey `1866420388623709184` | 无可匹配 webhook 回声 | 私聊图片接口接受；P04 已人工验收 200x200 PNG 展示成功。 |
| `text_with_md_object` | `msgtype=text`, 只传 `md` 对象 | 失败 | error `请求参数错误` | `msgtype=text` 必须传 `text` 对象。 |
| `md_with_text_object` | `msgtype=md`, 只传 `text` 对象 | 失败 | error `请求参数错误` | `msgtype=md` 必须传 `md` 对象。 |
| `upper_msgtype_text` | `msgtype=TEXT`, `text.content` | 失败 | error `msgTypeIsWrong` | 私聊 `msgtype` 大小写敏感，必须小写。 |
| `lower_msgtype_upper_object_key` | `msgtype=text`, 只传 `Text` 对象 | 失败 | error `请求参数错误` | 私聊内容对象 key 大小写敏感，必须是 `text`。 |
| `reply_text` | `msgtype=text`, `text.content`, 顶层 `reply[]` | 成功，msgkey `1866420390263635968` | 无可匹配 webhook 回声 | API 接受私聊 text reply；P05 确认客户端展示 reply。 |
| `reply_md` | `msgtype=md`, `md.content`, 顶层 `reply[]` | 成功，msgkey `1866420390904051712` | 无可匹配 webhook 回声 | API 接受但语义失败：人工验收批次 P06 确认 MD 渲染成功、reply 丢失。 |
| `reply_image` | `msgtype=image`, 200x200 蓝图，顶层 `reply[]` | 成功，msgkey `1866420391467496448` | 无可匹配 webhook 回声 | API 接受私聊 image reply；P07 确认客户端展示 reply 和图片。 |
| `reply_richtext` | `msgtype=richtext`, `richtext.content[]`, 顶层 `reply[]` | 成功，msgkey `1866421756931552256` | 无可匹配 webhook 回声 | API 接受私聊 richtext reply；P08 确认客户端展示 reply 和可点击链接。 |
| `richtext_link_only` | `msgtype=richtext`, `richtext.content[]` 只有 link | 成功，msgkey `1866422691306042368` | 无可匹配 webhook 回声 | API 接受私聊纯链接 richtext；P09 确认整条消息是一条超链。 |
| `reply_richtext_link_only` | `msgtype=richtext`, `richtext.content[]` 只有 link, 顶层 `reply[]` | 成功，msgkey `1866422691558703104` | 无可匹配 webhook 回声 | API 接受私聊纯链接 richtext reply；P10 确认引用结构保留，引用后的整行文字是可点击超链。 |
| `richtext_upper_a_item` | `msgtype=richtext`, `richtext.content[]` 只有 `type="A"` link | 成功，msgkey `1866423513774501888` | 无可匹配 webhook 回声 | API 接受大写 `A`，但客户端展示未人工验收；工具层仍使用规范小写 `a`。 |
| `richtext_multi_links` | `msgtype=richtext`, `richtext.content[]` text + 两个 link | 成功，msgkey `1866426019084709888` | 无可匹配 webhook 回声 | API 接受双链接 richtext；P11 确认说明文本可见，两个链接都可点击。 |
| `reply_empty_text` | `msgtype=text`, `text.content=""`, 顶层 `reply[]` | 成功，msgkey `1866426019602348032` | 无可匹配 webhook 回声 | API 接受私聊 reply-only；P12 确认只展示引用/回复结构，没有额外正文。 |
| `reply_two_targets_text` | `msgtype=text`, `text.content`, 顶层 `reply[]` 两个元素 | 成功，msgkey `1866426020167889920` | 无可匹配 webhook 回声 | API 接受两个 reply targets；P13 确认展示两条引用并展示正文。 |
| `richtext_link_two_reply_targets` | `msgtype=richtext`, link-only, 顶层 `reply[]` 两个元素 | 成功，msgkey `1866426020714198016` | 无可匹配 webhook 回声 | API 接受 link-only richtext + 两个 reply targets；P14 确认展示两条引用，引用后的整行文字为可点击链接。 |
| `text_three_reply_targets` | `msgtype=text`, `text.content`, 顶层 `reply[]` 三个元素 | 成功，msgkey `1866429230981002240` | 无可匹配 webhook 回声 | API 接受 3 个 reply targets；P15 确认展示三条引用并展示正文。 |
| `text_five_reply_targets` | `msgtype=text`, `text.content`, 顶层 `reply[]` 五个元素 | 成功，msgkey `1866429231579689984` | 无可匹配 webhook 回声 | API 接受 5 个 reply targets；P16 确认展示五条引用并展示正文。 |
| `richtext_link_five_reply_targets` | `msgtype=richtext`, link-only, 顶层 `reply[]` 五个元素 | 成功，msgkey `1866429232184766464` | 无可匹配 webhook 回声 | API 接受 link-only richtext + 5 个 reply targets；P17 确认展示五条引用，引用后的整行文字为可点击链接。 |
| `reply_no_content` | `msgtype=text`, `text.content`, 顶层 `reply[]` item 只含 `uid/msgid` | 成功，msgkey `1866432997747965952` | 无可匹配 webhook 回声 | P01 确认引用正常展示，`content` 可省略。 |
| `reply_empty_content` | `msgtype=text`, `text.content`, 顶层 `reply[]` item `content=""` | 成功，msgkey `1866432998379208704` | 无可匹配 webhook 回声 | P02 确认引用正常展示，`content` 可为空。 |
| `reply_wrong_msgid_with_content` | 错误 `reply[].msgid` + content | 成功，msgkey `1866432998957039616` | 无可匹配 webhook 回声 | P03 确认正文正常展示，reply 区域显示错误态。 |
| `reply_wrong_msgid_no_content` | 错误 `reply[].msgid`，省略 content | 成功，msgkey `1866432999532707840` | 无可匹配 webhook 回声 | P04 确认正文正常展示，reply 区域显示错误态。 |

### 私聊客户端展示人工验收批次

发送时间：

- P01-P07：2026-05-28 16:36:08，marker `20260528-163608`。
- P08：2026-05-28 16:54:51，marker `20260528-165451`。
- P09-P10：2026-05-28 17:09:43，marker `20260528-170943`。
- P11-P14：2026-05-28 18:02:15，marker `20260528-180215`；2026-05-28 已由收件人确认客户端展示全部符合预期。
- P15-P17：2026-05-28 18:53:21，marker `20260528-185321`；2026-05-28 已由收件人确认客户端展示全部符合预期。

本批次用于由收件人按编号确认客户端展示。每条可见文本都包含中文说明、期望效果和编号。`image` payload 本身不能携带文字说明，因此图片测试使用一条说明消息加紧随其后的图片消息配对验收。

| 编号 | 请求形态 | msgkey | 期望客户端展示 | 人工验收状态 |
|---|---|---|---|---|
| P01 | `msgtype=text`, `text.content` | `1866420577904309248` | 纯文本展示；`**P01 粗体标记**` 和列表标记不被 Markdown 渲染。 | 通过 |
| P02 | `msgtype=md`, `md.content` | `1866420578486267904` | Markdown 展示；`P02` 加粗文字加粗，列表项显示为列表。 | 通过 |
| P03 | `msgtype=richtext`, `richtext.content[]` text + link | `1866420579019634688` | 中文说明可见，`P03 示例链接` 显示为可点击链接。 | 通过 |
| P04-DESC | `msgtype=text`, `text.content` | `1866420579580952576` | 图片测试说明消息，提示下一条为 P04-IMG。 | 通过 |
| P04-IMG | `msgtype=image`, 200x200 纯蓝 PNG | `1866420580116445184` | 紧跟 P04-DESC 后显示 200x200 纯蓝 PNG，不是 1x1，不是破图。 | 通过 |
| P05 | `msgtype=text`, `text.content`, 顶层 `reply[]` 回复 P01 | `1866420580786814976` | 显示引用/回复 P01 的结构，正文按纯文本展示。 | 通过 |
| P06 | `msgtype=md`, `md.content`, 顶层 `reply[]` 回复 P01 | `1866420581372639232` | 探测 MD 和 reply 是否可同时生效。 | 语义失败：MD 渲染成功，但 reply 内容未展示 |
| P07-DESC | `msgtype=text`, `text.content` | `1866420581937131520` | 图片 reply 测试说明消息，提示下一条为 P07-IMG。 | 通过 |
| P07-IMG | `msgtype=image`, 200x200 纯蓝 PNG, 顶层 `reply[]` 回复 P01 | `1866420582474956800` | 紧跟 P07-DESC 后显示 200x200 纯蓝 PNG，并带有 P01 引用/回复结构。 | 通过 |
| P08-BASE | `msgtype=text`, `text.content` | `1866421756246782976` | richtext reply 测试的被回复基准消息。 | 通过 |
| P08-RICH | `msgtype=richtext`, `richtext.content[]` text + link, 顶层 `reply[]` 回复 P08-BASE | `1866421756931552256` | 显示 P08-BASE 的引用/回复结构，且 `P08 示例链接` 可点击。 | 通过 |
| P09 | `msgtype=richtext`, `richtext.content[]` 只有 link | `1866422691306042368` | 整条消息是一条超链。 | 通过 |
| P10 | `msgtype=richtext`, `richtext.content[]` 只有 link, 顶层 `reply[]` 回复基准消息 | `1866422691558703104` | 显示引用结构，引用后的整行文字都是可点击超链。 | 通过 |
| P11 | `msgtype=richtext`, `richtext.content[]` text + 两个 link | `1866426019084709888` | 说明文本可见，后面两个链接都可点击。 | 通过 |
| P12 | `msgtype=text`, `text.content=""`, 顶层 `reply[]` 回复 BASE | `1866426019602348032` | 只展示引用/回复结构，没有额外正文。 | 通过 |
| P13 | `msgtype=text`, `text.content`, 顶层 `reply[]` 两个元素 | `1866426020167889920` | 客户端展示两条引用，正文为 P13 文本。 | 通过 |
| P14 | `msgtype=richtext`, link-only, 顶层 `reply[]` 两个元素 | `1866426020714198016` | 客户端展示两条引用，引用后的整行文字为可点击链接。 | 通过 |
| P15 | `msgtype=text`, `text.content`, 顶层 `reply[]` 三个元素 | `1866429230981002240` | 客户端展示三条引用，正文为 P15 文本。 | 通过 |
| P16 | `msgtype=text`, `text.content`, 顶层 `reply[]` 五个元素 | `1866429231579689984` | 客户端展示五条引用，正文为 P16 文本。 | 通过 |
| P17 | `msgtype=richtext`, link-only, 顶层 `reply[]` 五个元素 | `1866429232184766464` | 客户端展示五条引用，引用后的整行文字为可点击链接。 | 通过 |

私聊字段规则：

- 文本：`{"msgtype": "text", "text": {"content": "..."}}`
- Markdown：`{"msgtype": "md", "md": {"content": "..."}}`
- 富文本：`{"msgtype": "richtext", "richtext": {"content": [{"type": "text", "text": "..."}, {"type": "a", "href": "...", "label": "..."}]}}`。`content` 可只有 `a` item；实测 link-only richtext 可展示为整条超链。
- 图片：`{"msgtype": "image", "image": {"content": "<base64 image>"}}`
- Reply：顶层 `reply` 是数组，例如 `{"content": "base private ...", "uid": "<quoted-sender-imid>", "msgid": "<quoted-msgkey>"}`。`uid` 使用被引用私聊消息发送者 imid，通常来自原消息 webhook `FromId/fromid`；`msgid` 使用被引用私聊消息的 `msgkey`。实测 `P-RID-04-UID-FROMID` 只有传 sender imid `1744775667` 才能让引用卡片前缀正确显示 `Reply chengbo05:`，传 `"0"`、账号名或省略 `uid` 都不能正确显示被引用者。实测 text/image/richtext 单 reply 请求能在客户端展示 reply；md 请求 API 返回成功且 Markdown 渲染成功，但 reply 内容丢失。因此带 reply 的普通私聊文本内容必须使用 `msgtype="text"`，不能为了 Markdown 渲染改成 `msgtype="md"`；带链接的私聊 reply 可使用 `msgtype="richtext"`。`text` 三条/五条 reply targets、link-only `richtext` 五条 reply targets 已人工确认全部引用可展示。`content` 可省略或为空且引用正常展示；错误 `msgid` 不阻断正文消息，reply 区域显示错误态。
- 私聊不使用群消息的 `body[].type="TEXT"/"MD"/"AT"/"IMAGE"`，也不使用大写 `msgtype`。
- 私聊 `richtext.content[].type` 的规范写法是小写 `text` 和 `a`。接口实测接受 `type="A"`，但未做客户端展示验收；工具层不要依赖非规范大写 item type。

私聊 text 示例：

```json
{
  "touser": "chengbo05",
  "toparty": "",
  "totag": "",
  "agentid": "6471",
  "msgtype": "text",
  "text": {
    "content": "plain text"
  }
}
```

私聊 Markdown 示例：

```json
{
  "touser": "chengbo05",
  "toparty": "",
  "totag": "",
  "agentid": "6471",
  "msgtype": "md",
  "md": {
    "content": "**Markdown**\n\n- item"
  }
}
```

私聊图片示例：

```json
{
  "touser": "chengbo05",
  "toparty": "",
  "totag": "",
  "agentid": "6471",
  "msgtype": "image",
  "image": {
    "content": "<base64 200x200 PNG>"
  }
}
```

## 字段参考

本节按“发送请求字段”列出，不列回声专有字段。后续实现不要把 webhook 回声中的字段名直接拿来构造出站请求。

### 群消息 header

| 字段 | 发送侧要求 | 说明 |
|---|---|---|
| `message.header.toid` | 数字群 ID | 本轮测试使用 `4507088`。 |
| `message.header.totype` | `"GROUP"` | 未测试其它大小写或取值，工具层固定使用大写。 |
| `message.header.msgtype` | `"MD"`、`"TEXT"`、`"IMAGE"` | 大小写敏感；`"text"`、`"MIXED"` 已验证失败。不要使用回声里的 `MIXED` 作为出站值。 |
| `message.header.clientmsgid` | 生成的客户端消息 ID | 使用现有 `_next_clientmsgid()` 生成；本文档不覆盖格式变体。 |
| `message.header.role` | `"robot"` | 本轮未测试其它取值，保持现有实现。 |

### 群消息 body item

| body `type` | 所属外层 `msgtype` | 发送字段 | 规则 |
|---|---|---|---|
| `MD` | `MD` | `content` | Markdown 正文。若同包带人类/机器人 `AT`，`content` 必须包含 `@<uuapName>` 或 `@<agentId>` 占位；@all 推荐放 `@all`。 |
| `TEXT` | `TEXT` 或 `IMAGE` | `content` | 纯文本正文。`TEXT` 下 Markdown 标记不渲染；`IMAGE` packet 内如需文字只能用 `TEXT`。 |
| `AT` | `TEXT`、`MD`、`IMAGE` | `atuserids`、`atagentids`、`atall` | 人类用 `atuserids:["chengbo05"]`；机器人用 `atagentids:[17212]`；全员用 `atall:true`。`atuserids` 和 `atagentids` 可在同一 item 中共存；TEXT/IMAGE 下 `atall` 要和具体用户/机器人拆成多个 item 才能同时原生生效；MD 下若包含 `atall`，只有 @all 原生生效，具体对象按正文文本展示。发送请求不要使用回声里的 `userid`、`robotid`、`name`。 |
| `LINK` | `TEXT` 或 `IMAGE` | `href`，可选 `label` | `href` 必填；只传 `label` 会失败。`MD + LINK` 已验证失败。回声只暴露 `label`，不回显 `href`。 |
| `IMAGE` | `IMAGE` | `content` | `content` 是图片 base64。测试图片为通过生产 media pipeline 准备的 200x200 PNG。 |

`replyData` 是 webhook 回声里的 body item，不是出站 body `type`。发送 reply 必须使用 `message.reply` block，不要在 body 里手工构造 `replyData`。

`AT` item 拼接规则：

- `TEXT`/`IMAGE` packet 可以包含多个 `AT` item；实测 `AT(atall=true) + AT(atuserids=[...]) + TEXT/IMAGE` 与 `AT(atall=true) + AT(atagentids=[...]) + TEXT/IMAGE` 均能同时保留 @all 和具体对象。
- `MD` packet 应只使用一个 `AT` item；人类和机器人可合并在同一个 item 中。若这个 MD item 里包含 `atall=true`，服务只保留 @all 原生语义，具体用户/机器人占位仍留在 MD 正文里按普通文本展示。
- 如果需要 @all 和具体用户/机器人都原生生效，不要使用 MD packet；应改用 TEXT 或 IMAGE packet 并拆成多个 `AT` item。

### 群消息 reply

| 字段 | 发送侧要求 | 说明 |
|---|---|---|
| `message.reply` | object，与 `header`、`body` 同级 | 只在 `TEXT` 或 `IMAGE` packet 中作为可用契约；`MD + reply` 已确认丢失 replyData。 |
| `message.reply.messageid` | 被回复消息 ID | 所有正向 reply 样例均带该字段；工具层不要省略。 |
| `message.reply.preview` | 引用预览文本，可省略或为空 | 省略/空字符串均已回声确认保留 `replyData`；有预览时建议传，便于客户端展示更具体。展示上限为前 100 字符 + `...`。 |
| `message.reply.imid` | 被引用消息发送者 imid；可省略 | 不带 `imid` 已验证仍保留 `replyData`，但引用卡片前缀可能不准确；该值通常来自原消息 webhook `fromid`，不要用当前机器人 imid 兜底。 |
| `message.reply.replytype` | 可省略；`"1"`、`"2"` 均保留 replyData | 本文档只证明回声语义保留，不区分客户端视觉。 |

群 `message.reply` 不支持数组。`msgtype=TEXT` + body `TEXT` 下传 2 条或 3 条 reply 数组均失败为 `请求参数错误`；工具层如果接到多个群 reply 目标，只能选择其中一条发送，不能把数组透传给服务。

### 私聊 payload

| 字段 | 发送侧要求 | 说明 |
|---|---|---|
| `touser` | 收件人 uuapName | 本轮测试使用 `chengbo05`。 |
| `toparty`、`totag` | 字符串，可为空 | 私聊单人测试为空字符串。 |
| `agentid` | 当前 app agent id 字符串 | 使用配置中的 `INFOFLOW_APP_AGENT_ID`。 |
| `msgtype` | 小写 `"text"`、`"md"`、`"richtext"`、`"image"` | 大小写敏感；必须和同名内容对象匹配。 |
| `text.content` | `msgtype="text"` 时使用 | 可与顶层 `reply[]` 同发；空正文 + reply 已人工确认可只展示引用/回复结构。 |
| `md.content` | `msgtype="md"` 时使用 | 不要与 `reply[]` 组合，客户端会丢失 reply。 |
| `richtext.content[]` | `msgtype="richtext"` 时使用 | item 规范类型为小写 `text` 和 `a`；`a` item 使用 `href` 和 `label`。可只有 `a` item；text + 两个 `a` item 已人工确认两个链接都可点击。 |
| `image.content` | `msgtype="image"` 时使用 | 图片 base64；200x200 PNG 已验收。 |
| `reply[]` | 顶层数组 | 元素形态为 `{"content":"...","uid":"<quoted-sender-imid>","msgid":"<msgkey>"}`；`uid` 是被引用消息发送者 imid，通常来自原消息 webhook `FromId/fromid`，用于引用卡片 `Reply <name>:` 身份显示；`content` 可省略或为空；5 条以内 reply targets 已人工确认可展示全部引用；错误 `msgid` API 也返回成功，正文正常展示但 reply 区域显示错误态。`msgid2` 未作为必要字段验证。 |

## 边界与不可推断项

以下内容没有在本轮形成可用格式契约，修改代码时不能靠推断启用：

- 私聊 `md + reply[]` 已确认语义失败；不能以 API 返回成功作为可用依据。
- 群纯 `IMAGE` 已确认 API 接受，但未捕获可匹配本地 webhook 回声；若后续要把纯图片作为严格可观测契约，应补充客户端确认或更可靠的回声捕获。
- 私聊图片展示验收使用 200x200 纯蓝 PNG；其它尺寸或格式的展示效果不由本文档结论覆盖。
- 群 `MD + @all + 具体用户/机器人` 的可用契约是只保留 @all 原生语义，具体对象按普通文本展示；若要求具体对象也原生 @，必须走 TEXT/IMAGE 并拆多个 `AT` item。
- 群多 reply 没有可用契约：`message.reply` 数组 2 条/3 条在 TEXT packet 下均被服务拒绝。
- 私聊 `richtext.content[].type="A"` 只完成了 API 接受性测试，尚未由收件人确认客户端展示；实现应使用已人工验收的规范小写 `a`。
- 群 `header.role`、`header.totype`、`clientmsgid` 格式没有做变体测试；实现应沿用现有发送代码，不要擅自改大小写或省略。
- 本文档未覆盖的组合格式，必须先按“真实发送 + 回声或人工验收”的方式补测，再更新本文档和实现。

## 服务适配注意事项

本节只描述底层服务契约对发送实现的约束。最终发送分层以 `docs/infoflow-send-message-refactor-plan.md` 为准。

- 需要 Markdown 渲染的群文本必须走 `msgtype="MD"` + body `type="MD"`；一旦同包需要 reply、LINK 或 IMAGE，就不能继续依赖 MD packet。
- 群 reply 必须走 `TEXT` 或 `IMAGE` packet；`MD + reply` 会丢 `replyData`。
- 群 reply 只能传单个 `message.reply` object，不能传数组。
- 群 LINK 必须走 body `type="LINK"` 且提供 `href`；不能塞进 MD packet，也不能只传 `label`。
- 群 IMAGE packet 中所有文本都必须是 `TEXT` body item；不能使用 `MD` body item。
- 群 @all 与具体用户/机器人混发时要特别处理：TEXT/IMAGE 可拆多个 `AT` item 让两者都原生生效；MD 可保持 Markdown，但只有 @all 原生生效，具体对象按普通文本展示。
- 私聊 reply 必须使用 app API 顶层 `reply[]`，不是群消息 body 数组；私聊 `md + reply[]` 已确认丢 reply。
- 私聊链接应使用 `msgtype="richtext"` 和小写 item type `a`；已确认单链接、纯链接、双链接、单 reply、两条 reply targets、五条 reply targets 可展示。

回声验证时不要只看 `header.msgtype`：

- Markdown 回声的 `header.msgtype` 经常是空字符串，但 `compatible/offlinenotify` 会显示 Markdown，body type 为 `MD`。
- TEXT 或含 AT/IMAGE 的回声 header 常见为 `MIXED`。
- 机器人 AT 的回声 header 里可能出现 `atrobotids: []`，应以 body 中的 `AT` item 作为主要判断依据。
