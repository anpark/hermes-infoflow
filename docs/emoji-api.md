# 如流 Emoji 回复 API

添加/删除指定消息的表情回复（如 👍、🎉 等）。

## 接口概览

| 操作 | 方法 | 路径 |
|------|------|------|
| 添加 | POST | `/api/v1/im/message/emoji/add` |
| 删除 | POST | `/api/v1/im/message/emoji/del` |

> 两个接口请求体格式完全相同，仅路径不同。添加接口是幂等的（重复添加返回成功）。删除接口不存在时返回 404。

## 认证

通过 App Access Token 认证：

```
Authorization: Bearer-{app_access_token}
```

Token 获取方式：`POST /api/v1/auth/app_access_token`

## 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| fromUid | String | 是 | 发送者用户 ID（机器人自身uid或触发用户uid均可） |
| chatType | Integer | 是 | 会话类型：`2`=群组，`7`=服务号 |
| chatId | Long | 否 | 会话 ID（正整数，机器人单聊时可不填） |
| baseMsgId | String | 是 | 被回复的消息 ID |
| msgId2 | String | 否 | 回复消息 ID（用于定位具体回复） |
| replyContent | String | 否 | 表情符号编码（如 `d135`） |
| replyDesc | String | 否 | 表情描述文本（如 `(qjp)`） |

## Content-Type

```
Content-Type: application/json; charset=utf-8
```

## 成功响应

```json
{"code": "ok", "data": {"bizCode": 200, "bizMsg": "ok", "bizData": null}}
```

## 错误码

| 错误码 | 说明 |
|--------|------|
| 720001 | 机器人无权限 |
| 100000 | 参数错误 |
| 40001 | 参数无效 |
| 50010 | 远程 RPC 调用失败 |
| 500000 | 系统内部错误 |

## msgId2 字段来源

`msgid2` 是 webhook 推送的**原始 payload 顶层字段**，与 `groupid`、`message` 同级，每条群消息都会携带。当前插件 parser 未提取该字段，如需使用可从 `raw_data` 获取。

**Webhook 原始 payload 示例（群消息）：**

```json
{
  "eventtype": "ALL_MESSAGE_FORWARD",
  "agentid": 6471,
  "groupid": 4507088,
  "msgid2": 300014580,
  "fromid": 1744775667,
  "message": {
    "header": {
      "fromuserid": "chengbo05",
      "toid": 4507088,
      "totype": "GROUP",
      "msgtype": "MIXED",
      "messageid": "1865794273048386548",
      "msgseqid": "",
      "servertime": 1779360077949
    },
    "body": [
      {"type": "TEXT", "content": "你好啊 chengbo5.1 可以用这条消息测试"}
    ]
  }
}
```

**字段映射关系：**

| Emoji API 字段 | Webhook Payload 来源 |
|---------------|---------------------|
| `baseMsgId` | `message.header.messageid`（如 `"1865794273048386548"`） |
| `msgId2` | 顶层 `msgid2`（如 `300014580`） |
| `chatId` | 顶层 `groupid`（如 `4507088`） |
| `chatType` | 群聊固定 `2`，私聊 `1`（根据有无 `groupid` 判断） |
| `fromUid` | `message.header.fromuserid`（如 `"chengbo05"`）或顶层 `fromid`（imid） |

## 实际验证示例

**添加表情：**

```bash
curl -X POST "http://apiin.im.baidu.com/api/v1/im/message/emoji/add" \
  -H "Authorization: Bearer-{token}" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "fromUid": "chengbo05",
    "chatType": 2,
    "chatId": 4507088,
    "baseMsgId": "1865794273048386548",
    "msgId2": "300014580",
    "replyContent": "d135",
    "replyDesc": "(qjp)"
  }'
```

**删除表情：**

```bash
curl -X POST "http://apiin.im.baidu.com/api/v1/im/message/emoji/del" \
  -H "Authorization: Bearer-{token}" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "fromUid": "chengbo05",
    "chatType": 2,
    "chatId": 4507088,
    "baseMsgId": "1865794273048386548",
    "msgId2": "300014580",
    "replyContent": "d135",
    "replyDesc": "(qjp)"
  }'
```

## 表情编码说明

如流表情使用自定义编码体系，格式为字母+数字组合（如 `d135`、`d101`）。`replyDesc` 对应括号包裹的文本描述（如 `(qjp)`、`(收到)`）。具体编码表需参考如流内部文档。
