# Infoflow 插件日志体系文档

> 本文档描述 hermes-infoflow 插件的日志配置、消息追踪标签、关键日志点及排查指南。

---

## 1. 日志配置

### 1.1 Logger 命名

| Logger | 定义位置 | 用途 |
|--------|---------|------|
| `logging.getLogger(__name__)` | 各模块文件顶部 | 模块级日志，用于内部调试 |
| `logging.getLogger("gateway.run")` | `utils.py` `gw_log()` | **Gateway 通道日志**，输出到 `gateway.log` |

### 1.2 输出位置

| 文件 | 内容 | 过滤规则 |
|------|------|---------|
| `~/.hermes/logs/gateway.log` | 所有运行时日志（INFO+） | 只接收 logger name 以 `"gateway"` 开头的记录 |
| `~/.hermes/logs/gateway.error.log` | 错误日志（WARNING+） | 无特殊过滤 |
| `~/.hermes/logs/agent.log` | Agent 侧日志 | 仅 CLI 模式使用 |

### 1.3 关键设计：`gw_log()` 绕过过滤

`gateway.log` 只接收 logger name 以 `"gateway"` 开头的记录。infoflow 插件的模块名是 `hermes_infoflow.adapter` 等不以 `"gateway"` 开头，直接使用 `logger.info()` 不会出现在 `gateway.log` 中。

因此插件通过 `utils.py` 中的 `gw_log()` 函数获取名为 `"gateway.run"` 的 logger：

```python
# utils.py
def gw_log():
    return logging.getLogger("gateway.run")
```

所有需要记录到 `gateway.log` 的日志都通过 `gw_log().info(...)` / `gw_log().warning(...)` 调用。

---

## 2. 消息追踪标签

每条入站消息在整个处理链路中会产生一系列带有 `[iflow:*]` 或 `[infoflow:*]` 前缀的日志。通过 `msgid` 可以串联完整链路。

### 2.1 入站标签

| 标签 | 触发位置 | 记录内容 | 格式 |
|------|---------|---------|------|
| `[iflow:raw]` | `webhook.py` | Webhook 收到的**原始明文 payload** | JSON 全文 |
| `[iflow:event]` | `adapter.py` `_build_message_event()` | Enrichment 后的**标准字段** | `sender_id= sender_name= group= mentioned= text=` |
| `[infoflow-enrich]` | `adapter.py` `_enrich_sender()` | Sender 补全**结果** | `sender= name= agent_id= is_bot= degraded=` |
| `[iflow:decision]` | `adapter.py` | 策略**判定结果** | `action= trigger= reason= sender= text=` |
| `[iflow:dispatch]` | `adapter.py` | Dispatch 路径选择 | `prompt_len=` |
| `[iflow:user_message]` | `adapter.py` `_build_message_event()` | **最终发给 LLM 的完整 user message** | `mid= len= text=\n{全文}` |
| `[iflow:debug]` | `adapter.py` `_build_message_event()` | **完整的 channel_prompt 内容** | `len= FULL=\n{全文}` |

### 2.2 出站标签

| 标签 | 触发位置 | 记录内容 | 格式 |
|------|---------|---------|------|
| `[infoflow:send_payload]` | `api.py` | **发送给如流 API 的完整 JSON payload** | JSON 全文 |
| `[iflow:send]` | `adapter.py` | 发送**结果** | `mid= target= chars= success=` |
| `[iflow:recall]` | `recall.py` | 消息**撤回** | `mid= target= success=` |

### 2.3 其他标签

| 标签 | 触发位置 | 说明 |
|------|---------|------|
| `[infoflow]` | `adapter.py` 各处 | 插件通用信息（连接状态、配置加载等） |
| `[iflow:at_only]` | `adapter.py` | AT-only 消息处理 |

---

## 3. 关键日志点说明

### 3.1 收请求（Webhook 明文）

```
[iflow:raw] mid=1865678874352932868 raw_payload={"msgtype":"text","text":{"content":"hello"},...}
```

- **位置**：`webhook.py` 解密后立即记录
- **用途**：排查消息是否到达、解密是否正确、原始内容是什么
- **注意**：包含完整明文，含用户发送的原始文本

### 3.2 消息事件（Enrichment 后）

```
[iflow:event] mid=1865678874352932868 sender_id=chengbo05 sender_name=chengbo297 ... text=hello
```

- **位置**：`adapter.py` enrichment 完成后
- **用途**：确认 sender 身份补全是否正确（uuapName、是否机器人、agent_id）

### 3.3 策略判定

```
[iflow:decision] mid=1865678874352932868 action=DISPATCH trigger=direct-message reason=dm sender=chengbo297 text=hello
```

- **位置**：`adapter.py` 收到 `PolicyDecision` 后
- **用途**：确认消息是否被分发、触发原因是什么
- **关键判断**：`action=DROP` 表示消息被丢弃，`action=RECORD` 表示仅记录不回复

### 3.4 LLM 输入（User Message + Channel Prompt）

```
[iflow:user_message] mid=1865678874352932868 len=85 text=
[Sender: chengbo05 | human](admin — 完全权限)
[Message]
hello

[iflow:debug] channel_prompt len=155 FULL=
Your name is chengbo5.2 (agentId: 6533).
...
```

- **位置**：`adapter.py` `_build_message_event()` 末尾
- **用途**：确认发送给 LLM 的内容是否正确（prompt 注入、sender 标签、权限标签等）
- **注意**：这两条日志是排查"回复内容不对"的首要检查点

### 3.5 发送结果

```
[infoflow:send_payload] {"touser":"chengbo05","msgtype":"md","md":{"content":"你好！"}}
[iflow:send] mid=1865678874352932868 target=chengbo05 chars=3 success=True
```

- **位置**：`api.py`（payload）+ `adapter.py`（结果）
- **用途**：确认消息是否发送成功、发送内容是什么
- **关键判断**：`success=False` 需要检查网络或 API 凭据

---

## 4. 排查指南

### 4.1 "消息丢失了"

**现象**：用户发了消息但 bot 没回复

**排查步骤**：
1. 搜索 msgid 或关键词：`grep "1865678874352932868" gateway.log`
2. 检查 `[iflow:decision]`：
   - `action=DROP` → 被策略丢弃，检查 trigger_reason
   - `action=RECORD` → 被动记录不回复，符合预期
   - `action=DISPATCH` → 已分发，继续排查下游
3. 检查是否有 `[iflow:raw]`（没有 = 消息未到达 webhook）

### 4.2 "回复内容不对"

**排查步骤**：
1. 检查 `[iflow:user_message]`：确认发给 LLM 的 user message 是否正确
2. 检查 `[iflow:debug]`：确认 channel_prompt 中的安全规则、权限标签是否正确
3. 检查 sender 标签：确认 `[Sender: ...]` 中的权限标签是否正确（admin / restricted）

### 4.3 "群消息没触发"

**排查步骤**：
1. 检查 `[iflow:decision]` 的 `trigger` 字段：
   - `direct-message` → 被误判为私聊（不可能）
   - 无 `[iflow:decision]` → 消息在 Step 4 被去重丢弃
2. 检查 `[iflow:event]`：`mentioned=True` 是否正确（@是否被解析到）
3. 检查群配置：`reply_mode`、`follow_up`、`watch_mentions` 是否配置正确

### 4.4 "发送失败"

**排查步骤**：
1. 检查 `[iflow:send]` 的 `success` 字段
2. 检查 `[infoflow:send_payload]`：payload 格式是否正确
3. 群消息渲染或 reply 异常时，对照 `docs/infoflow-message-format.md` 检查 `header.msgtype` 和 `body[].type`
4. 检查网络连接和 API 凭据（token 是否过期）

### 4.5 端到端排查示例

**场景**：用户 @bot 问了一个问题，但没收到回复

```bash
# 1. 找到消息的所有日志
grep "1865678874352932868" ~/.hermes/logs/gateway.log

# 2. 检查是否到达
grep "\[iflow:raw\].*1865678874352932868" ~/.hermes/logs/gateway.log
# → 有 → 消息到达了

# 3. 检查策略判定
grep "\[iflow:decision\].*1865678874352932868" ~/.hermes/logs/gateway.log
# → action=DISPATCH → 已分发

# 4. 检查发给 LLM 的内容
grep "\[iflow:user_message\].*1865678874352932868" ~/.hermes/logs/gateway.log
# → 确认 user message 格式正确

# 5. 检查 LLM 是否回复
grep "\[iflow:send\].*1865678874352932868" ~/.hermes/logs/gateway.log
# → 无 → LLM 没有回复（可能输出了 NO_REPLY 或 tool loop 异常）
# → 有 success=True → 消息已发送，检查如流端

# 6. 检查发送 payload
grep "\[infoflow:send_payload\]" ~/.hermes/logs/gateway.log | tail -5
# → 确认发送内容格式正确
```
