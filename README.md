# 群专属人格（astrbot_plugin_group_persona）

<img src="logo.png" width="128" alt="logo">

给指定群聊绑定一个专属人格，和其他群彻底区分开。

- 优先级最高：盖过配置文件默认人格、对话里的 `/persona`，也盖过 WebUI「自定义规则」里已经设置的人格。
- 不挑隔离模式：不开「隔离对话」时一个群一个会话；开启后每个成员是独立会话，插件会分别接管，效果仍然是「整个群统一人格」。
- 可回退：写入前会记录被覆盖的原值，`/gpersona unbind` 或插件卸载/重载时自动恢复，不留下“幽灵规则”。

## 原理

AstrBot 的人格解析是按会话（UMO）来的，优先级大致是：

```
会话强制人格（session_service_config.persona_id）
  > 对话级人格（/persona、WebUI 会话设置）
    > 所属配置文件的默认人格
```

本插件在 AstrBot 准备调用 LLM、还没构建请求时（`OnWaitingLLMRequestEvent`），把当前群会话的 `session_service_config.persona_id` 写成绑定的目标人格——这正是 AstrBot 自己「自定义规则 → 人格配置」使用的最高优先级通道，所以能压过系统默认值和已有的会话/对话设置。

这样做的另一个好处是：人格的 system prompt、开场白（begin dialogs）、人格限定的工具/技能、`persona` 错误提示等，全都走 AstrBot 原生逻辑，不需要插件自己拼提示词。

写入是按 UMO 的：

- 关闭「隔离对话」：群会话 ID 就是群号，一个群只有一条记录，一次写入全群生效。
- 开启「隔离对话」：会话 ID 变成「成员 + 群」，插件会在每个成员第一次请求时分别写入，最终仍然是全群同一个人格。

## 安装

任选一种：

1. 在 AstrBot WebUI 的插件库中粘贴本仓库地址安装：
   `https://github.com/xZhongjie/astrbot_plugin_group_persona`
2. 把整个目录放到 `data/plugins/astrbot_plugin_group_persona/`，重启 AstrBot。

依赖：无。要求 AstrBot `>= 4.11.0`。

## 使用

在需要设置人格的群里（管理员）：

| 指令 | 说明 |
| --- | --- |
| `/gpersona bind <人格ID>` | 把当前群绑定到指定人格（人格 ID 就是 WebUI「人格」页里的名字） |
| `/gpersona unbind` | 取消绑定，并恢复被覆盖的原人格规则 |
| `/gpersona status` | 查看本群绑定、当前会话 UMO 和已写入的人格 |
| `/gpersona list` | 列出所有已绑定的群 |

> 群聊里指令默认需要唤醒前缀（默认 `/`）或 @ 机器人。

例子：

```
/gpersona bind 客服小助手
```

之后这个群的所有对话（包括新建对话、开启隔离后的每个成员）都会强制使用「客服小助手」，其他群不受影响。

人格 ID 可以用 `/gpersona bind` 不带参数查看列表，或到 WebUI「人格」页面复制。

## 与「自定义规则」的关系

WebUI 的「自定义规则 → 人格配置」和本插件写的是同一个字段，所以：

- 如果某个会话原本有强制人格规则，插件会覆盖它，并在解绑/卸载时还原。
- 如果你在 WebUI 里又改了这个会话的人格，插件在下一次 LLM 请求时会再次覆盖回绑定值——这是“优先级更高”的预期行为。
- 解绑时如果发现当前值已经不是插件写入的值（说明有人手动改过），插件不会覆盖，直接放弃恢复。

## 兼容性

- 适用于 AstrBot 内置的本地 Agent Runner（`local`）。第三方 Agent Runner（Dify、Coze、DashScope 等）不走这条人格解析路径，插件不会生效。
- 只在真的要调用 LLM 时写入，不监听普通群消息，不会把群里的每条消息都当成唤醒。
- 插件只修改 `session_service_config.persona_id` 一个字段，`llm_enabled`、`tts_enabled` 等其他会话配置原样保留。

## 数据与清理

- 绑定和接管记录保存在 `data/plugin_data/astrbot_plugin_group_persona/bindings.json`。
- 正常卸载或被重载时会自动恢复。如果直接删除插件目录导致没有走卸载流程，可能残留强制人格规则，请到 WebUI「自定义规则」里手动清空对应会话的人格。
- 建议卸载前先在各个群执行 `/gpersona unbind`。

## 已知限制

- 人格被删除后，插件会跳过强制（记录一条 warning），不会把会话指到一个不存在的人格。
- 同一会话并发首次写入时可能重复写一次，结果幂等。
- 只处理群聊；私聊不生效。

## License

MIT
