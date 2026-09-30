# 考古地层关系核验服务

基于 **Python (FastAPI) + PostgreSQL** 的考古地层关系核验服务。登记遗址地层与带唯一证据编号的
“同一期 / 早于”断言，自动推导地层间关系，支持批次原子核验、冲突证据路径定位、关系查询、不可变定版快照，
以及按正式修订追踪关系结论与支撑证据路径的变化。

## 语义规则

- **同一期（same_period）**：对称且可传递，构成等价类。
- **早于（earlier_than）**：沿“同一期 + 早于”混合路径推导。
  例如 `L1 ~ L2`（同一期）、`L2 < L3`（早于），则 `L1 早于 L3`。
- **硬约束**（对整批应用后的最终关系核验）：
  1. **组内早于禁止**——同一期等价类内部不得存在早于关系；
  2. **早于闭环禁止**——等价类之间的早于关系不得成环。
- **批次语义**：每批新增/撤回须携带当前修订号；按整批应用后的最终关系核验；
  发生矛盾时**整批拒绝**并返回冲突证据路径，原状态不变；全部通过则在同一事务内落库并**原子递增修订号**。
- **路径选取**：多条证据路径时，取证据数最少者；并列时按证据编号序列字典序最小者。
- **定版（快照）**：保存当前修订号下全部关系结论的不可变快照；后续撤回不影响旧版结论。
- **推演（沙盘）**：创建时固定当前正式修订号、已登记地层与生效断言；推演内沿用正式批次的
  新增 / 撤回 / 整批核验语义（携带推演自身修订号），正式状态始终不变。提交（并入）时把推演
  **相对基线的净变更**应用于最新正式状态并重新核验：基线目标已撤回 / 编号已被正式占用 /
  关系冲突（附证据路径）时返回具体原因且双方不变；通过则作为一次正式批次原子提交并关闭推演；
  净变更为空时只关闭推演，不递增正式修订号。
- **变化追踪**：每次成功正式批次 / 有净变更的推演并入产生正式修订 N 时，在同一事务内记录
  修订 N-1 → N 间关系结论或最优证据路径发生变化的地层对（同一对按编号固定顺序只记一次，
  变为未定时路径为空）；失败批次、推演内编辑、无净变更并入不产生记录；
  `GET /changes` 以稳定游标分页读取，同一轮分页固定正式修订上界。

## 快速开始

```bash
docker compose up --build
```

- API 地址：**http://localhost:8000**
- 交互式接口文档（Swagger UI）：**http://localhost:8000/docs**
- PostgreSQL：`localhost:5432`（容器内服务名 `db`，数据持久化于命名卷 `pgdata`）

首次启动时应用会自动等待数据库就绪并完成表结构初始化，无需手工迁移。

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://strat:strat@db:5432/stratigraphy`（compose 内） | 应用连接串 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `strat` / `strat` / `stratigraphy` | 数据库账号（compose） |
| `APP_PORT` | `8000` | API 对外端口 |
| `DB_PORT` | `5432` | 数据库对外端口 |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://strat:strat@localhost:5432/stratigraphy
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/strata` | 登记地层 `{stratum_id, site?}` |
| GET | `/strata` | 地层列表 |
| POST | `/batches` | 提交一批断言新增/撤回（整批核验，成功原子递增修订号） |
| GET | `/revision` | 当前修订号 |
| GET | `/assertions?status=active` | 断言列表 |
| GET | `/relationship?a=L1&b=L3` | 查询两地层当前关系及支撑证据路径 |
| GET | `/changes?limit=&cursor=` | 正式修订间关系结论及证据路径变化（稳定游标分页） |
| POST | `/snapshots` | 定版：保存不可变关系快照 `{label?}` |
| GET | `/snapshots` / `/snapshots/{version}` | 版本列表 / 版本详情 |
| GET | `/snapshots/{version}/relationship?a=L1&b=L3` | 查询历史版本结论 |
| POST | `/projections` | 创建推演（固定当前修订号、地层与生效断言） `{label?}` |
| GET | `/projections` / `/projections/{id}` | 推演列表 / 推演详情（含净变更） |
| POST | `/projections/{id}/batches` | 推演内提交批次（携带推演修订号，语义同正式批次） |
| GET | `/projections/{id}/relationship?a=L1&b=L3` | 查询推演视角下的关系与证据路径 |
| POST | `/projections/{id}/merge` | 并入：净变更应用于最新正式状态并重新核验 |
| DELETE | `/projections/{id}` | 放弃推演（仅 open 可放弃） |

### 批次请求格式

```json
{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-009"}
  ]
}
```

- `relation`：`same_period`（同一期）/ `earlier_than`（`stratum_a` 早于 `stratum_b`）。
- 成功响应：`{"revision": 1, "applied": 3}`。
- 冲突响应（HTTP 409，原状态不变）：

```json
{
  "error": {
    "code": "CONFLICT",
    "message": "批次与现有断言矛盾，整批拒绝，原状态不变",
    "details": {
      "conflicts": [
        {"type": "earlier_cycle", "message": "早于关系形成闭环: ...", "evidence_path": ["EV-002", "EV-003", "EV-004"]}
      ]
    }
  }
}
```

### 关系查询响应

```json
{
  "stratum_a": "L1",
  "stratum_b": "L3",
  "relation": "earlier_than",
  "relation_label": "早于",
  "evidence_path": ["EV-001", "EV-002"],
  "path_detail": [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active"},
    {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than", "status": "active"}
  ],
  "revision": 1
}
```

`relation` 取值：`same_period`（同一期）/ `earlier_than`（早于）/ `later_than`（晚于）/ `undetermined`（未定）。

### 错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `INVALID_STRATUM` | 引用了未登记的地层 |
| 400 | `INVALID_CURSOR` | 分页游标无效或已损坏 |
| 409 | `STALE_REVISION` | 批次携带的修订号过期（响应含当前修订号） |
| 409 | `DUPLICATE_EVIDENCE` | 证据编号重复（含已撤回的编号） |
| 409 | `ASSERTION_NOT_FOUND` | 撤回不存在或已撤回的断言 |
| 409 | `CONFLICT` | 整批核验发现组内早于 / 早于闭环（附冲突证据路径） |
| 409 | `STRATUM_EXISTS` | 地层重复登记 |
| 404 | `SNAPSHOT_NOT_FOUND` | 版本不存在 |
| 404 | `PROJECTION_NOT_FOUND` | 推演不存在 |
| 409 | `PROJECTION_CLOSED` | 推演已关闭（merged/discarded），不可再操作 |
| 409 | `MERGE_TARGET_RETRACTED` | 并入失败：撤回目标证据已在最新正式状态被撤回 |
| 409 | `MERGE_EVIDENCE_TAKEN` | 并入失败：推演新增证据编号已被正式历史占用 |
| 409 | `MERGE_CONFLICT` | 并入核验发现关系冲突（附冲突证据路径），双方不变 |

## 推演（证据修订沙盘）

考古人员可在**不改动正式断言**的前提下推演证据修订：

1. **创建即固定基线**：`base_revision`（正式修订号）、`base_strata`（已登记地层）、
   `base_assertions`（生效断言快照）。推演创建之后正式侧新增的地层 / 断言对该推演不可见。
2. **推演内批次语义与正式批次一致**：
   - 请求携带的是**推演修订号**（自 0 起，每次成功批次 +1），过期返回 `STALE_REVISION`；
   - 按整批应用后的推演最终关系核验，组内早于 / 早于闭环整批拒绝，附冲突证据路径；
   - **正式历史已用证据编号不可复用**（含已撤回，查正式 `assertions`），
     **推演新增编号在本推演内不可复用**（含在推演内已撤回）；
   - 可撤回推演自己新增的断言，也可撤回基线断言；撤回同样只置状态、不可重复。
3. **推演查询**：`GET /projections/{id}/relationship` 返回推演视角下的关系与证据路径，
   `path_detail[].scope` 标注每步证据来源：`base`（基线）/ `projection`（推演新增）。
4. **并入（merge）**：把推演相对基线的**净变更**（推演内仍生效的新增 + 被撤回的基线断言；
   “新增后又撤回”不产生净变更）应用到**最新**正式状态并重新核验：
   - 基线撤回目标已在正式状态撤回 → `MERGE_TARGET_RETRACTED`；
   - 新增编号已被正式占用 → `MERGE_EVIDENCE_TAKEN`；
   - 净变更应用后出现组内早于 / 早于闭环 → `MERGE_CONFLICT`（附证据路径）；
   - 任一失败：**正式状态与推演均不变**。
   - 核验通过：作为一次正式批次原子提交（写 `assertions`、`revisions` 审计，
     批次记录带来源 `source=projection_merge`），正式修订号 +1 并关闭推演；
   - 净变更为空：只关闭推演，**不递增正式修订号**。
5. 并入与放弃都会关闭推演；关闭后其冻结视图仍可查询，但不能再提交批次或并入。

### 推演请求示例

创建并查询：

```bash
curl -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "二期证据修订推演"}'
# -> {"id": 1, "base_revision": 1, "projection_revision": 0, "status": "open", ...}

curl "$BASE/projections/1/relationship?a=L1&b=L3"
```

推演内批次（`expected_revision` 为推演修订号）：

```bash
curl -X POST "$BASE/projections/1/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-002"},
    {"op": "assert", "evidence_id": "EV-P01", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"}
  ]}'
# -> {"id": 1, "projection_revision": 1, "applied": 2}（正式状态不变）
```

并入：

```bash
curl -X POST "$BASE/projections/1/merge"
# 成功 -> {"id": 1, "merged": true, "applied": 2, "revision": 2,
#          "revision_incremented": true, "net_changes": {"added": [...], "retracted": [...], "count": 2}}
# 冲突 -> HTTP 409 MERGE_CONFLICT，details.conflicts[].evidence_path 给出证据路径
```

完整流程（含正式状态不受影响、编号复用拒绝、三类并入失败、空净变更只关闭）见
`examples/projection_demo.sh`：

```bash
bash examples/projection_demo.sh
```

## 修订变化追踪（GET /changes）

考古人员可按正式修订追踪关系结论及支撑证据的变化。每次**成功的正式断言批次**与
**有净变更的推演并入**产生正式修订 `N` 时，服务在**同一事务**内计算并物化修订
`N-1 → N` 之间发生变化的地层对：

- 同一地层对按编号固定顺序（字典序，`stratum_a < stratum_b`）只记录一次；
- **关系不变但最优支撑路径改变**同样记录；
- 变为 `undetermined`（未定）时该侧 `evidence_path` 为 `null`、`path_detail` 为空；
- 旧侧证据按**当时状态**解释：后来撤回的证据在旧侧 `path_detail` 中仍显示 `active`；
- 失败批次、推演内编辑、无净变更并入不递增正式修订号，**不产生变化记录**；
- 历史修订（本功能上线前已存在）在首次查询时按断言的 `added_revision / retracted_revision`
  自动重建当时状态并回填，同样可追溯（历史地层登记的修订号无法考证，迁移时记 0，视为自始已登记；
  新登记地层记录登记时的正式修订号）。

### 分页与并发语义

- 首轮请求不带 `cursor`：服务固定**当前正式修订号**为本轮上界（响应含 `max_revision`）；
- 后续页携带上一页返回的 `next_cursor`：同一轮分页沿用同一上界，并发提交产生的新修订
  不影响本轮结果（不漏项、不重复）；`next_cursor` 为 `null` 表示本轮读完，
  再次以无游标请求即开启新一轮（含此后新修订）；
- 变化记录与产生它的批次在**同一事务**提交，任何时刻都不会读到半批结果；
- 游标为不透明字符串（内含上界与位置），请勿自行构造；非法游标返回 400 `INVALID_CURSOR`。

### 响应示例

```bash
curl "$BASE/changes?limit=2"
# 翻页
curl "$BASE/changes?limit=2&cursor=<上一页返回的 next_cursor>"
```

```json
{
  "max_revision": 5,
  "changes": [
    {
      "revision": 5,
      "stratum_a": "C1",
      "stratum_b": "C3",
      "old": {
        "relation": "earlier_than",
        "relation_label": "早于",
        "evidence_path": ["EV-C01"],
        "path_detail": [
          {"evidence_id": "EV-C01", "stratum_a": "C1", "stratum_b": "C3",
           "relation": "earlier_than", "status": "active"}
        ]
      },
      "new": {
        "relation": "earlier_than",
        "relation_label": "早于",
        "evidence_path": ["EV-C02", "EV-C03"],
        "path_detail": [
          {"evidence_id": "EV-C02", "stratum_a": "C1", "stratum_b": "C2",
           "relation": "earlier_than", "status": "active"},
          {"evidence_id": "EV-C03", "stratum_a": "C2", "stratum_b": "C3",
           "relation": "earlier_than", "status": "active"}
        ]
      }
    }
  ],
  "next_cursor": "eyJtYXhfcmV2aXNpb24iOjUsImxhc3QiOls1LCJDMSIsIkMzIl19"
}
```

上例即“关系不变但最优支撑路径改变”：修订 5 撤回了直接证据 EV-C01，C1 与 C3 仍为早于，
最优路径改走 EV-C02 + EV-C03；旧侧 EV-C01 虽已撤回，仍按当时状态显示 `active`。

端到端演示（新增结论、路径变化、变为未定、失败批次/空净变更并入无记录、稳定游标分页）：

```bash
bash examples/changes_demo.sh
```

## 调用示例

端到端 curl 演示（覆盖登记、批次、推导查询、冲突拒绝、撤回、定版与旧版不可变）：

```bash
bash examples/demo.sh
```

推演（沙盘）端到端演示（创建固定基线、推演批次、关系查询、并入成功与三类并入失败、
净变更为空只关闭、放弃推演）：

```bash
bash examples/projection_demo.sh
```

修订变化追踪端到端演示（按正式修订追踪结论与证据路径变化、稳定游标分页）：

```bash
bash examples/changes_demo.sh
```

Python 客户端示例：

```bash
pip install requests
python3 examples/client_demo.py
```

## 项目结构

```
app/
  main.py         # FastAPI 路由与错误处理
  service.py      # 正式批次核验、关系查询、定版快照
  projections.py  # 推演（沙盘）：创建固定基线、推演批次、关系查询、并入核验
  changes.py      # 修订变化追踪：变化计算与物化、历史回填、稳定游标分页
  derivation.py   # 推导引擎：并查集等价类、DAG 校验、最优证据路径
  db.py           # 连接池与模式初始化（启动时自动建表）
  models.py       # 请求模型
examples/
  demo.sh           # curl 端到端示例（批次/查询/冲突/撤回/定版）
  projection_demo.sh # 推演端到端示例（沙盘批次、并入成功与三类失败、空净变更）
  changes_demo.sh    # 修订变化追踪示例（结论/路径变化、稳定游标分页）
  client_demo.py    # Python 客户端示例
Dockerfile
docker-compose.yml
requirements.txt
```
