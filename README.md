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

三处业务代码分开：分诊判定在`src/rules.py`（纯函数），后送台账在`src/repository.py`/`src/service.py`，请求入口在`src/http_api.py`。

规则：伤员按现场编号（`site_ref`）登记，重复上报沿用首次分诊（409并回传首次记录）；发车前队列按危重critical、重伤serious、轻伤minor排序，同等级按登记序号；复查只能调高等级，未发车批次就地重排，已发车记录不动；批次状态为planned→confirmed→departed，已确认批次不能打散；车辆座位或接收点床位不足时409返回`details.gap`缺口明细，原队列与批次保留；同一伤员只能被一个接收点收治一次。

- `POST /api/casualties` 登记分诊（field_commander/logistics）
- `GET /api/casualties`、`GET /api/casualties/waiting` 全部/待后送队列（已按发车顺序排好）
- `POST /api/casualties/{id}/recheck` 复查调高（未发车批次自动重排）
- `POST /api/vehicles`、`POST /api/receivers` 车辆（座位）、接收点（床位）
- `POST /api/batches` 组批（按队列顺序取 min(座位,空床)，响应含剩余缺口）
- `POST /api/batches/{id}/confirm`、`POST /api/batches/{id}/depart` 确认（复核运力）、发车
- `GET /api/batches`、`GET /api/batches/{id}`
- `POST /api/casualties/{id}/admit` 接收点收治（重复收治409）
- `GET /api/admissions`

## 测试

```bash
python3 -m unittest discover -s tests -v
```
