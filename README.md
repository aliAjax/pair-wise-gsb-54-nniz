# 跨分局抢修协同台

纯Python标准库实现的跨海光缆抢修跨分局协同原型，使用SQLite持久化，HTTP接口由`http.server`提供。

围绕抢修值班"按角色放权"的场景，支持：

- **建单写明管辖分局**：故障单登记在创建者所属分局名下（上级代建须指定管辖分局）。
- **双向可见**：仅管辖分局与**当前在效受托分局**可见；上级调度（`admin`）可见全部。退回、到期、撤回后受托方立即失去可见性。
- **按阶段委托与生效期**：抢修经理可把单个阶段或全流程委托给兄弟分局，设置生效起止时间与事由凭据。
- **三种即时失效**：受托方退回、到达生效截止（读/写时自动翻转）、上级调度撤回。
- **单分局写入**：委托生效期内仅受托分局可写，且只能做委托阶段内动作；管辖分局写已委托阶段会收到 409，其中带受托方与冲突动作；其他分局直接 403。
- **冲突凭据与重试**：所有写操作走 `BEGIN IMMEDIATE` 行锁 + 乐观版本号。换手失败不改原记录，另写 `write_conflict` 审计（含尝试动作、唯一写入分局、先成动作、所持版本/最新版本），客户端可按最新版本重试。
- **详情页**：展示管辖分局、当前受托（阶段/生效期/状态）、委托履历、交接时间线与冲突记录。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误（携带 details）、ISO时间工具和基础校验。
- `src/rules.py`：状态转换、阶段映射、分局可见性与写入授权、委托校验。
- `src/repository.py`：SQLite建表/迁移、写锁事务、委托表、到期翻转、审计。
- `src/service.py`：用例编排、委托生命周期、冲突凭据、详情组装；支持注入时钟。
- `src/http_api.py`：HTTP路由与统一错误响应（409 携带冲突细节）。
- `src/audit.py`：事件时间线。
- `static/index.html`：跨分局协同台演示页面。
- `tests/`：完整流程、规则计算、失败场景与跨分局协同测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 身份头

除`/health`和`/`外，请求需提供：

- `X-User-Id`：用户ID
- `X-Role`：`noc_operator` / `repair_manager` / `vessel_master` / `cable_engineer` / `admin`
- `X-Org`：分局代号（如 `JIA`、`YI`）；非上级角色必填，`admin` 可跨分局并撤回委托

## 主要接口

- `GET /health`：健康检查。
- `GET /`：协同台演示页面。
- `GET /api/records`：仅返回本分局（管辖或当前受托）可见的工单，可带`state`、`limit`。
- `GET /api/records/{id}`：工单详情，含 `owner_org`、`trustee_org`、`writer_org`、`current_delegation`、`delegations`、`handovers`、`conflicts`。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stages`：可委托阶段列表。
- `GET /api/stats`：可见范围内的状态统计。
- `POST /api/records`：登记故障，`{"reference":"...","data":{...}}`，管辖分局取 `X-Org`（admin 可在 `data.owner_org` 指定）。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/delegations/grant`：发起委托：
  `{"expected_version":1,"data":{"target_org":"YI","stage":"approve","valid_from":"2026-10-07T08:00:00Z","valid_until":"2026-10-08T08:00:00Z","reason":"就近支援"}}`
  （`stage` 可选 `approve/mobilize/survey/splice/test/restore/all`；`valid_from` 省略即立即生效）
- `POST /api/records/{id}/delegations/return`：受托分局退回，`{"expected_version":n,"data":{"reason":"..."}}`。
- `POST /api/records/{id}/delegations/withdraw`：上级撤回，同上。

冲突响应示例（HTTP 409）：

```json
{
  "error": "conflict",
  "message": "版本冲突，请刷新后重试",
  "details": {
    "attempted_action": "approve",
    "writer_org": "YI",
    "winning_action": "approve",
    "winning_actor": "yi-rm-1",
    "expected_version": 2,
    "current_version": 3
  }
}
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：完整抢修流程、规则计算、重复引用、角色与分局权限、可见性隔离、按阶段委托、生效期/退回/撤回/到期失效、并发单写入、冲突凭据与按新版本重试。
