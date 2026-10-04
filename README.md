# 跨国数字贸易会谈编排服务

面向数贸会等国际活动的会谈编排服务：在跨境数字贸易合作的共享基础能力之上，
以代表团与机构关联为基础核验参会资格，统一编排席位、保障资源与材料访问权。

## 解决的问题

同一家企业同时出现在部长闭门会、采购洽谈、技术尽调名单，译员、保密会议室与
合规观察员被不同代表团重复预订，临时替换的代表仍能下载敏感材料——本服务保证
**席位、资源与访问权始终以唯一一次有效确认为准**：

- 资格核验：代表团-机构关联有效、代表在岗、知悉级别覆盖议题敏感级别；
- 编排约束：议题敏感级别、跨时区时间窗、译员语言能力、场地容量、回避关系、
  互斥场次与互斥分组；
- 邀请生命周期：邀请（有限期保留）→ 候补（稳定次序）→ 接受 / 转授权 / 退出 /
  改期；保留到期自动回到候补队尾，空位按候补位置递补；
- 多方未确认时只做有时限的软保留，截止后按 `waitlist_rank` 稳定递补；
- 材料权限跟随有效席位与承诺版本即时开放或收回（含知悉级别降级、转授权、退出）；
- 会谈结束、签到与材料访问是只追加事实，新名单不能覆盖；转授权一旦产生签到或
  访问事实即不可撤销；
- 全部状态持久化在 SQLite，进程恢复后沿用原候补位置与到期时间；
- 写接口 `request_id` 幂等，`BEGIN IMMEDIATE` + 进程内事务互斥保证重复确认与
  同时发布只有一个生效安排；
- 角色在 HTTP 接口中只能看到履职所需内容，所有变更进入哈希串联审计链。

## 目录

- src/digital_trade_foundation/
  - service.py / storage.py / audit.py / clock.py：基础登记、SQLite 事务、审计链；
  - scheduling.py：会谈编排核心领域（资格、冲突、候补、授权、材料、事实）；
  - scheduling_api.py：编排层 HTTP 路由；
  - api.py：统一 HTTP 入口（基础路由 + 编排路由）；
  - acceptance.py / scheduling_acceptance.py：基础与编排两套离线验收。
- tests/：存储、基础服务、编排领域（30 例）、HTTP 视角（12 例）、并发（3 例）、
  两套端到端验收。

## 环境

- Linux，Python 3.11+，仅使用标准库与 SQLite。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance
    PYTHONPATH=src python3 -m digital_trade_foundation.scheduling_acceptance

编排验收复现数贸会场景：重复预订保密会议室被拒、互斥场次向被拒代表解释冲突来源、
低知悉级别代表被拒、转授权后材料随席位收回、访问事实阻止撤销、进程恢复后候补递补、
从场次反查参与者/资源/保密依据/历次变更。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080

写入通过 `X-Actor-Id` 标识履职身份，所有写请求携带 `request_id` 实现幂等。
主要接口：

| 方法 & 路径 | 说明 |
| --- | --- |
| POST /delegations, /representatives, /resources, /sessions | 登记代表团、代表、资源、场次 |
| POST /representatives/clearance | 调整知悉级别（立即影响下载判断） |
| POST /resource-blocks, /recusals, /session-exclusions | 资源封闭窗、回避关系、互斥场次 |
| POST /invitations | 发邀请：返回 invited / waitlisted / rejected 及冲突明细 |
| GET  /decisions/{id} | 取回邀请结论，用于向被拒代表解释冲突 |
| GET  /conflicts/preview | 不写入的冲突预检 |
| POST /invitations/respond, /seats/withdraw | 接受/谢绝、退出（触发递补） |
| POST /seats/delegate, /authorizations/revoke | 转授权与撤销（有事实则禁止撤销） |
| POST /sessions/reschedule, /sessions/conclude | 改期（保留重新计时）、结束（冻结事实） |
| POST /maintenance/run-due | 到期保留处理与候补递补（恢复后调用） |
| POST /materials, /materials/access, /check-ins | 材料登记、访问（留事实）、签到 |
| GET  /sessions/{id}/view | 联络组反查：参与者、资源、材料、事实、保密依据、历次变更 |
| GET  /seats/{id}/history | 席位承诺版本历史 |
| GET  /representatives/{id}/schedule | 代表本人视角（仅本人代表团可见） |
| GET  /resources/{id}/schedule | 资源保障方视角（仅本机构资源） |

角色：`admin`、`operator`（主办编排）、`liaison`（代表团联络人，仅本团数据）、
`resource_manager`（资源保障）、`observer`（现场签到/访问）、`reviewer`（合规）、
`auditor`（只读审计）。
