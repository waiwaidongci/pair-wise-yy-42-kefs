# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 伤员后送排队

后送业务拆成三处代码：

- `src/triage.py`：分诊判定（危重 critical > 重伤 serious > 轻伤 minor）、重复上报沿用首次分诊、复查只许调高、发车前排序、座位/床位缺口计算。
- `src/evacuation.py`：后送台账（SQLite，复用`Repository`连接）。伤员按现场编号`case_ref`登记，编号重复的上报记为`accepted=0`且不改分诊；批次确认即冻结（成员不可打散、车辆不可另挂批次）；发车后批次、分诊、床位占用全部只读。
- `src/service.py` + `src/http_api.py`：请求入口、角色校验和审计留痕。

规则要点：

- 发车前队列按 危重→重伤→轻伤 排序，同等级按登记先后，危重伤员不会被留在队尾。
- 车辆座位不足时返回`seat_gap`/`seat_gap_message`，未上车伤员保留在原队列等待下一批。
- 发车时接收点床位不能整体收下整批时返回409并说明缺口（需要/空余/缺几张），批次与床位均不变动。
- 复查调高等级后，等待队列立即重排；已确认未发车批次只重排发车轮次、成员不打散；已发车记录不动（拒绝改写）。
- 伤员经批次成员唯一约束保证只能被一个接收点收下，不会重复接收。

接口（登记/复查用 field_commander 等现场角色，车辆/接收点/批次/发车用 logistics 或 incident_commander）：

- `POST /api/med/casualties`，提交`case_ref`、`triage`
- `POST /api/med/casualties/{id}/recheck`，提交调高后的`triage`
- `GET /api/med/casualties/{id}`、`GET /api/med/casualties/{id}/reports`
- `GET /api/med/waiting`
- `POST /api/med/vehicles`（`plate`、`seats`）、`GET /api/med/vehicles`
- `POST /api/med/receivers`（`name`、`total_beds`）、`GET /api/med/receivers`
- `POST /api/med/batches`，提交`vehicle_id`或临时`seats`
- `POST /api/med/batches/{id}/dispatch`，提交`receiver_id`
- `GET /api/med/batches`、`GET /api/med/batches/{id}`

## 测试

```bash
python3 -m unittest discover -s tests -v
```
