# 编排跨国合作会谈协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

在此之上，项目内置了面向国际活动（如数贸会）的**会谈编排服务**（`scheduling.py`），覆盖：

- **参会资格核验**：代表团与机构关联、代表密级对议题敏感级别、跨时区可用窗口、回避关系（代表/翻译员/合规观察员对机构）、互斥场次（时间重叠或同一互斥组）；
- **资源编排**：保密会议室容量、翻译语言覆盖、合规观察员指派；发布时占用资源并防止重复预订，多方未确认前只保留有限期限，截止后按候补位次稳定递补并重新保留；
- **邀请生命周期**：邀请、候补、接受、转授权（同代表团内）、退出、改期（场次版本递进、已接受代表重新确认）、截止过期；
- **材料授权**：跟随有效席位与承诺版本（邀请版本 + 场次版本）发放与收回；临时替换的代表立即失去下载权限；已结束的会谈、签到与访问事实不可被新名单覆盖；
- **角色化视图**：主办方联络、代表团联络员、代表本人、翻译员、合规观察员、审计员在 HTTP 接口中只看到履职所需内容；
- **可追溯性**：从任一场次反查参与者、资源、保密依据与历次变更；被拒代表可查询具体冲突原因（密级、回避、互斥、窗口、重复邀请等）。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、会谈编排（scheduling）、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、会谈编排规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance
    PYTHONPATH=src python3 -m digital_trade_foundation.meeting_acceptance

基础验收在临时 SQLite 数据库中登记合作机构、操作者、业务节点和参考资料，核对幂等回执与审计链；会谈验收完整演练部长闭门会的资格核验、候补递补、进程重启恢复、转授权与替换后的权限收回、签到与访问事实保留。成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态、候补位次、到期时间和审计历史继续保留。

### 会谈编排接口

- 登记：`POST /delegations`、`POST /delegates`、`POST /delegate-replacements`、`POST /rooms`、`POST /interpreters`、`POST /recusals`；
- 场次：`POST /sessions`、`POST /sessions/{id}/publish|reschedule|cancel|complete`、`POST /sessions/{id}/interpreters`、`POST /sessions/{id}/observers`；
- 邀请：`POST /sessions/{id}/invitations`、`POST /invitations/{id}/respond|delegate|withdraw`；
- 材料与签到：`POST /materials`、`POST /materials/{id}/access`、`POST /sessions/{id}/checkins`；
- 维护：`POST /maintenance/sweep`（推进截止、递补与场次结束）；
- 视图：`GET /sessions/{id}`（按角色裁剪）、`GET /sessions/{id}/trace`（反查，限主办方/合规/审计）、`GET /sessions/{id}/rejections`、`GET /delegates/{id}/rejections`、`GET /me/invitations`、`GET /me/assignments`。

所有写接口要求请求体携带 `request_id` 做幂等；重复确认或同时发布在 SQLite 事务与唯一约束下只保留一个生效安排。
