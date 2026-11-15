# live-capture 项目技能包

给 AI 助手/新会话用的操作技能，每个目录一个 SKILL.md（YAML frontmatter + 操作步骤）。

| 技能 | 何时用 |
|---|---|
| [deploy-termux](deploy-termux/SKILL.md) | 把代码改动发布到手机、重启服务、验证 |
| [diagnose-live-events](diagnose-live-events/SKILL.md) | 场次异常：幻影开播、缺下播、分段错乱 |
| [organize-live-data](organize-live-data/SKILL.md) | 整理数据：核场次、删脏段、导出、统计 |
| [bili-capture-protocol](bili-capture-protocol/SKILL.md) | 改采集内核：协议解析、开播判定、重连、补记 |
| [verify-changes](verify-changes/SKILL.md) | 改完代码后的三套测试与验证清单 |
| [build-frontend](build-frontend/SKILL.md) | 改 Vue 前端、构建、部署产物 |

配套背景知识在 `docs/`（architecture / api / data-model / operations / pitfalls）。先读 pitfalls.md 再动手，大部分坑已经踩过一轮。
