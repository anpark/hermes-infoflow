# Infoflow getuserinfo API (私聊 code → uuap)

用于 Session Tracker 页面在 `chatType=1/7`（私聊/服务号）时，将 URL 中的 `code` 解析为对话对象的 `UserId`（uuapName）。

## 请求

```http
POST {INFOFLOW_API_HOST}/api/v1/app/user/getuserinfo
Authorization: Bearer-{app_access_token}
Content-Type: application/json; charset=utf-8
LOGID: {uuid}
```

Body（JSON）：

```json
{
  "agentid": 6471,
  "code": "50374f0d197196b535e0a370f49fc131"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `agentid` | int | 如流应用 agentId，对应环境变量 `INFOFLOW_APP_AGENT_ID` |
| `code` | string | 私聊场景由如流下发的临时 code（Session Tracker URL 的 `code` 参数） |

## 响应示例

```json
{
  "code": "ok",
  "data": {
    "errcode": 0,
    "errmsg": "ok",
    "UserId": "chengbo05"
  }
}
```

| 字段 | 说明 |
|------|------|
| `code` | 顶层状态，期望 `"ok"` |
| `data.errcode` | 业务错误码，0 表示成功 |
| `data.UserId` | 私聊对象 uuapName，用于 Hermes session 的 DM `chat_id` |

## 与 Session Tracker URL 的关系

| chatType | 含义 | chatId | code |
|----------|------|--------|------|
| `2/3/5/6` | 群聊 | 群 ID | 不需要 |
| `1/7` | 私聊（服务号） | 如流侧 chat 标识 | **必填**，经本接口解析为 `UserId` |

示例：

```
GET /webhook/infoflow/sessiontracker?chatType=7&chatId=3950087625&code=2cecba82ba9686cb75596bfbe5637f03
```

插件实现：[`hermes_infoflow/api.py`](../hermes_infoflow/api.py) 中的 `get_user_info_by_code()`。

手工验证：

```bash
export INFOFLOW_API_HOST=http://apiin.im.baidu.com
export INFOFLOW_APP_KEY=...
export INFOFLOW_APP_SECRET=...
export INFOFLOW_APP_AGENT_ID=6471
python scripts/test_getuserinfo.py --code <code>
```
