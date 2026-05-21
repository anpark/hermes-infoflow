# 如流 Emoji 回复 API

添加/删除指定消息的表情回复（如 👍、🎉 等）。

## 接口概览

| 操作 | 方法 | 路径 |
|------|------|------|
| 添加 | POST | `/api/v1/im/message/emoji/add` |
| 删除 | POST | `/api/v1/im/message/emoji/del` |

> 两个接口请求体格式相同，仅路径不同。添加接口是幂等的（重复添加返回成功）。

## 认证

```
Authorization: Bearer-{app_access_token}
```

Token 获取：`POST /api/v1/auth/app_access_token`

## 请求参数（完整字段说明）

| 字段 | 类型 | 群聊 | 私聊 | 说明 |
|------|------|------|------|------|
| `chatType` | Integer | **2**（必传） | **7**（必传） | 群组=2，服务号=7。⚠️ 私聊不能用 1，虽然 API 返回 200 但用户端不可见 |
| `chatId` | Long | **必传**（群 ID） | **不传** | 群聊传 `groupid`，私聊不传此字段 |
| `fromUid` | String | 可不传 | **必传** | 私聊必须传目标用户的 uuapName（如 `chengbo05`），不传则表情不可见 |
| `baseMsgId` | String | **必传** | **必传** | 被回复的消息 ID |
| `msgId2` | String | 可不传 | 可不传 | 辅助定位，实测可不传 |
| `replyContent` | String | 建议传 | 建议传 | 表情符号编码（如 `d135`），指定具体表情 |
| `replyDesc` | String | 可不传 | 可不传 | 表情描述文本（如 `(qjp)`） |

## 通用响应

**成功：**

```json
{"code": "ok", "data": {"bizCode": 200, "bizMsg": "ok", "bizData": null}}
```

**错误码：**

| 错误码 | 说明 |
|--------|------|
| 100000 | 参数错误 |
| 40001 | 参数无效 |
| 720001 | 机器人无权限 |
| 50010 | 远程 RPC 调用失败 |
| 500000 | 系统内部错误 |

## 群聊（chatType=2）

### 请求格式

```
POST /api/v1/im/message/emoji/{add|del}
Content-Type: application/json; charset=utf-8
```

```json
{
  "chatType": 2,
  "chatId": 4507088,
  "baseMsgId": "1865794273048386548",
  "replyContent": "d135",
  "replyDesc": "(qjp)"
}
```

> `fromUid`、`msgId2`、`replyDesc` 均可省略。`chatId` 必传。

### 添加示例

```bash
curl -X POST "http://apiin.im.baidu.com/api/v1/im/message/emoji/add" \
  -H "Authorization: Bearer-{token}" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"chatType":2,"chatId":4507088,"baseMsgId":"1865794273048386548","replyContent":"d135","replyDesc":"(qjp)"}'
```

### 删除示例

```bash
curl -X POST "http://apiin.im.baidu.com/api/v1/im/message/emoji/del" \
  -H "Authorization: Bearer-{token}" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"chatType":2,"chatId":4507088,"baseMsgId":"1865794273048386548","replyContent":"d135"}'
```

## 私聊（chatType=7）

### 请求格式

```
POST /api/v1/im/message/emoji/{add|del}
Content-Type: application/json; charset=utf-8
```

```json
{
  "fromUid": "chengbo05",
  "chatType": 7,
  "baseMsgId": "1865798223458853292",
  "replyContent": "d135",
  "replyDesc": "(qjp)"
}
```

> ⚠️ 三个关键差异：
> - `chatType` 必须用 **7**（服务号），不能用 1
> - `fromUid` **必传**，值为目标用户的 uuapName
> - **不传 `chatId`**

### 添加示例

```bash
curl -X POST "http://apiin.im.baidu.com/api/v1/im/message/emoji/add" \
  -H "Authorization: Bearer-{token}" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"fromUid":"chengbo05","chatType":7,"baseMsgId":"1865798223458853292","replyContent":"d135","replyDesc":"(qjp)"}'
```

### 删除示例

```bash
curl -X POST "http://apiin.im.baidu.com/api/v1/im/message/emoji/del" \
  -H "Authorization: Bearer-{token}" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"fromUid":"chengbo05","chatType":7,"baseMsgId":"1865798223458853292","replyContent":"d135"}'
```

## Webhook Payload 字段来源

`msgid2` 是 webhook 推送的顶层字段，群消息和私聊都会携带。

**群消息 Webhook payload：**

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
      "messageid": "1865794273048386548",
      "servertime": 1779360077949
    },
    "body": [{"type": "TEXT", "content": "你好啊 chengbo5.1 可以用这条消息测试"}]
  }
}
```

**私聊 Webhook payload：**

```json
{
  "agentId": "6471",
  "Content": "你好 chengbo5.1",
  "FromId": 1744775667,
  "FromUserId": "chengbo05",
  "FromUserName": "chengbo297",
  "MsgId": "1865798223458853292",
  "MsgId2": "300016044"
}
```

**字段映射关系：**

| Emoji API 字段 | 群聊来源 | 私聊来源 |
|---------------|---------|---------|
| `chatType` | 固定 `2` | 固定 `7` |
| `chatId` | 顶层 `groupid` | 不传 |
| `fromUid` | 可不传 | `FromUserId`（必传） |
| `baseMsgId` | `message.header.messageid` | `MsgId` |
| `msgId2` | 顶层 `msgid2` | 顶层 `MsgId2` |

## 表情编码说明

如流表情使用自定义编码体系，格式为字母+数字组合（如 `d135`、`d101`）。`replyDesc` 对应括号包裹的文本描述（如 `(qjp)`、`(收到)`）。具体编码表需参考如流内部文档。

已知表情示例：`d135` = 敲键盘 `(qjp)`，`d101` = 收到 `(收到)`。
