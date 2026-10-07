# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。内置跨分局协同台：建单写明管辖分局，抢修经理可按阶段委托兄弟分局并设生效期，受托方退回、到期或上级撤回后立即失效。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、冲突检查和委托校验。
- `src/repository.py`：SQLite建表、事务、委托生命周期和查询。
- `src/service.py`：用例编排、角色与分局权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应（409携带冲突详情）。
- `src/audit.py`：事件时间线。
- `static/index.html`：跨分局协同台演示页面。
- `tests/`：完整流程、规则计算、失败场景和跨分局协同测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表并迁移既有库（补充`jurisdiction_org`列）。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：跨分局协同台演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数；非上级角色仅返回本分局管辖及当前受托的记录。
- `GET /api/records/{id}`：记录详情，附带`jurisdiction_org`（管辖分局）、`delegation`（当前受托）与`delegations`（交接记录）。
- `GET /api/records/{id}/audit`：审计时间线，含委托/退回/撤回/到期事件。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`；`data.jurisdiction_org`为管辖分局，缺省取创建人`X-Org`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`（分局标识，跨分局协同的关键凭据）。

## 跨分局协同规则

- **管辖与可见**：建单写明管辖分局；仅管辖分局、当前生效受托分局和上级（`admin`）可见可办，其余分局一律拒绝。
- **阶段委托**：`delegate`动作（抢修经理或上级，且须为管辖分局）将指定阶段（`approve/mobilize/survey/splice/test/restore`子集）委托给兄弟分局，须设`valid_until`生效期；同一记录同时只允许一个生效委托。
- **立即失效**：受托分局`return`退回、到期（系统自动扫描并记审计）或上级`revoke`撤回后，受托方立即失去可见与办理权限。
- **并发写入**：业务办理以记录版本号做乐观并发，并在事务内复核受托快照；冲突方收到409及`details`（当前版本、当前受托方、冲突动作），原记录保留，可据当前版本重试。
- **换手重试**：委托/退回/撤回不消耗记录版本号，换手失败后可按原版本直接重试。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、跨分局可见性、委托生命周期和并发冲突详情。

