# Live Subtitle Finalizer — 直播字幕定稿服务

基于 FastAPI + SQLite 的直播字幕定稿服务。客户端按频道提交带**序号 / 修订号 / 文本**的字幕片段，服务将乱序到达的片段放入缓冲区，只从序号 1 起按**连续前缀**推进水位线并生成**不可再修改**的定稿字幕。操作员可对确认永久丢失的序号区间发起**缺口裁决**，服务在同一事务中记录缺口并释放紧随其后的连续缓冲片段。定稿之后如需改字，可通过**追加式更正层**提交不可变的更正版本——原始定稿永远保留，下游可在原始视图与生效视图之间选择。需要让下游分批拉取**同一时刻的稳定字幕**时，可对频道**发布快照**：单事务锁定水位线并冻结 1..watermark 的原始 / 生效结果、缺口标记与各条更正版本，此后任何定稿、更正或缺口都不会改变既有快照。**命名消费者**还可通过带过期时间的**租约（lease）**从各自的已确认游标后拉取连续批次（含缺口标记），服务端事务化保证未过期批次不会重复发放、确认必须同时校验 `lease_token` 与 `expected_cursor`，租约到期后未确认批次可重新领取。

## 语义规则

- 片段可乱序到达；同一序号只接受**更高修订号**（更低或相等的修订号被拒绝）。
- 水位线 = 已定稿的最大连续序号（定稿字幕与永久缺口共同构成连续前缀）。遇到缺口即停，**不会越过缺口**定稿。
- 缺口被补齐的提交会在**同一事务**内一次释放整段连续片段。
- 序号 ≤ 当前水位线的提交一律不生效（定稿不可变，已裁决为缺口的序号同样不可再提交）。
- 所有提交 / 裁决在 `BEGIN IMMEDIATE` 事务内串行执行，并发下不会重复定稿或跳过缺口。

### 永久缺口裁决规则

- 区间 `[start_seq, end_seq]`（含两端）的**起点必须等于当前水位线下一位**；
- 区间内**不得存在已缓冲片段**（有缓冲说明片段只是迟到，应等待定稿而不是裁决丢失）；
- 任一校验失败返回 `applied=false` 且**不产生任何写入**；
- 裁决成功：同一事务内记录被跳过序号、推进水位线到区间终点，并继续释放紧随其后的连续缓冲片段；
- 重复提交**已完整裁决的同一区间**幂等返回当前结果（`idempotent=true`）；与既有裁决**部分重叠**则拒绝（`gap_overlap`）。

### 追加式更正规则

- 定稿不可改写；需要修改已定稿字幕时只能**追加**一条更正，原始文本永久保留。
- 仅**已定稿字幕**可被更正；未定稿序号与永久缺口序号一律拒绝（409）且不写入。
- 每个序号的更正版本从 1 起单调递增；提交时必须携带 `expected_version`，无更正时为 `0`，否则须等于当前最新版本。
- 版本不匹配（含并发竞争失败）返回 409 `version_conflict`；校验与追加在同一 `BEGIN IMMEDIATE` 事务内原子完成。
- 定稿查询默认 `view=original`（响应结构不变）；`view=effective` 合并最新更正文本并返回每条字幕的 `correction_version`。
- 更正历史可通过审计接口按版本升序查询。

### 发布快照规则

- 创建快照在**单个 `BEGIN IMMEDIATE` 事务**内完成「锁定频道当前 watermark → 固化 `1..watermark` 的字幕（原始文本 + 创建时刻的生效文本）与缺口标记 → 冻结各字幕序号当时存在的全部更正版本」，冻结结果与创建时刻的定稿状态严格一致。
- 快照**整体不可变**：创建之后新定稿的字幕、新追加的更正或新裁决的缺口只写入原表，任何既有快照的内容（含分页与更正历史）都不随之变化。
- 同一频道携带相同 `idempotency_key` 重试始终返回**同一个 `snapshot_id`**（响应 `idempotent=true`），不重新冻结；幂等键按频道隔离（不同频道可复用同一字符串）。
- 不存在的频道也可创建 `watermark=0` 的空快照（不创建频道行）；之后可分页拉到空列表。
- 快照表只追加，存储层触发器禁止任何 `UPDATE` / `DELETE`。

### 命名消费者拉取游标与租约规则

- 每个 `(channel, consumer)` 拥有独立的**已确认游标** `cursor`（从 0 起）；领取在单个 `BEGIN IMMEDIATE` 事务内完成「读游标 → 检查最近租约 → 返回 `cursor+1` 起的连续批次 → 签发 `lease_token`」，批次中字幕与缺口标记按序号混合返回，`limit`（默认 100，1..10000）截断本批条目数。
- **未过期且未完整确认的租约存续期间，同一消费者不得重复领取**：重复调用返回 409 `lease_active` 且不改写任何状态（不同消费者互不影响，各自持有独立租约与游标）。
- 确认接口必须**同时校验** `lease_token` 与 `expected_cursor`：每次确认把游标推进 `expected_cursor+1`，且推进点必须落在该批次 `[start_seq, end_seq]` 之内；可在租约有效期内逐条确认，直到批次末位（`fully_confirmed=true`）。
- **过期令牌、旧令牌（过期后重新领取产生了新租约）、已完整消费的令牌、越界推进、并发落后请求**（`expected_cursor` 与服务端游标不一致）一律返回 409 且**不改写任何状态**。
- 租约到期后旧令牌立即失效（409 `lease_expired`），允许在同一事务中**重新领取同一未确认批次**（从当前已确认游标之后开始）；确认成功后下一次领取从新游标继续。
- 游标之后暂无可领取序号时返回空 `items` 且不签发租约（`lease_token=null`）；领取之后新到达并定稿的序号不影响在途租约，下一次领取才可见。
- 租约 TTL 由环境变量 `LEASE_TTL_SECONDS` 配置（默认 30 秒），到期时刻在签发时固化。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口（容器内 `uvicorn --host 0.0.0.0 --port 8000`，Compose 映射 `8000:8000`），启动时自动初始化数据库（建表幂等，包括更正层与快照层新表及触发器，旧实例重启自动补建）。

- 交互式 API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- 提交片段：`POST /channels/{channel}/fragments`
- 缺口裁决：`POST /channels/{channel}/gaps`
- 定稿查询（`view=original|effective`）：`GET /channels/{channel}/finalized`
- 追加更正：`POST /channels/{channel}/finalized/{seq}/corrections`
- 更正审计（版本升序）：`GET /channels/{channel}/finalized/{seq}/corrections`
- 创建发布快照：`POST /channels/{channel}/snapshots`
- 分页拉取快照（`view=original|effective`）：`GET /snapshots/{snapshot_id}/items`
- 快照内更正历史（版本升序）：`GET /snapshots/{snapshot_id}/items/{seq}/corrections`
- 消费者领取批次（带过期租约）：`POST /channels/{channel}/consumers/{consumer}/claims`
- 消费者确认批次（校验 lease_token + expected_cursor）：`POST /channels/{channel}/consumers/{consumer}/claims/confirm`
- 查询消费者状态：`GET /channels/{channel}/consumers/{consumer}/status`

## 配置

| 环境变量           | 默认值                 | 说明                                   |
| ------------------ | ---------------------- | -------------------------------------- |
| `DB_PATH`          | `/data/subtitles.db`   | SQLite 数据库文件路径                  |
| `LEASE_TTL_SECONDS`| `30`                   | 消费者拉取租约有效期（秒），到期可重新领取未确认批次 |

Compose 默认将 `/data` 挂载到命名卷 `subtitles-data`，容器重启数据不丢失。端口映射可在 `docker-compose.yml` 的 `ports` 中调整。

## API

### 提交字幕片段

```bash
curl -X POST http://localhost:8000/channels/news/fragments \
  -H 'Content-Type: application/json' \
  -d '{"seq": 1, "revision": 1, "text": "各位观众晚上好"}'
```

响应（`applied` 表示本次提交是否生效，`watermark` 为处理后的最新水位线，`finalized` 为本次新定稿的片段）：

```json
{
  "channel": "news",
  "seq": 1,
  "applied": true,
  "reason": null,
  "watermark": 1,
  "finalized": [{"seq": 1, "revision": 1, "text": "各位观众晚上好"}]
}
```

`applied=false` 时 `reason` 取值：

- `stale_revision` — 修订号不高于已缓冲的同序号片段；
- `already_finalized` — 该序号已定稿（或已裁决为永久缺口），不可再修改。

### 裁决永久缺口

当确认某些序号永久丢失（例如推流中断），操作员可对缺失区间发起裁决：

```bash
curl -X POST http://localhost:8000/channels/news/gaps \
  -H 'Content-Type: application/json' \
  -d '{"start_seq": 3, "end_seq": 4}'
```

裁决成功的响应（`watermark` 为裁决后的新水位线，`skipped` 为本次跳过区间，`finalized` 为随裁决一并释放的连续缓冲片段）：

```json
{
  "channel": "news",
  "applied": true,
  "idempotent": false,
  "reason": null,
  "watermark": 6,
  "skipped": {"start_seq": 3, "end_seq": 4},
  "finalized": [
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

重复提交**完全相同**的已裁决区间时，幂等返回原裁决结果，`idempotent` 为 `true`。`applied=false` 时 `reason` 取值：

- `start_not_at_watermark` — 区间起点不是当前水位线下一位（不能跳过未裁决的序号）；
- `buffered_fragments` — 区间内存在已缓冲片段，应等待其定稿；
- `already_finalized` — 区间已完全定稿且不匹配任何缺口；
- `gap_overlap` — 与既有缺口裁决部分重叠（完全相同的区间才允许幂等提交）。

请求体校验（如 `start_seq < 1`、`end_seq < start_seq`）返回 HTTP 422。

### 查询频道定稿

定稿查询按序号升序**混合返回字幕与缺口标记**，`kind` 字段供下游区分丢失内容。通过 `view` 参数在原始文本与更正后文本之间选择，**默认 `original`，响应结构与旧版完全一致**：

```bash
curl 'http://localhost:8000/channels/news/finalized?after=0&limit=1000&view=original'
```

```json
{
  "channel": "news",
  "watermark": 6,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 2, "revision": 1, "text": "第二条字幕", "start_seq": null, "end_seq": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4},
    {"kind": "gap",      "seq": 4, "revision": null, "text": null, "start_seq": 3, "end_seq": 4},
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

- `kind=subtitle`：定稿字幕，取 `revision` / `text`；
- `kind=gap`：永久缺口标记，`start_seq` / `end_seq` 标明所属裁决区间（同一区间内每个被跳过序号各占一条标记）；
- `after` 用于增量拉取，对字幕与缺口标记统一按序号计数。

`view=effective` 时，每个字幕条目合并其**最新更正文本**，并附带该序号的当前 `correction_version`（`0` = 未更正；缺口条目为 `null`）；顶层 `correction_version` 是本页返回范围内出现过的最大更正版本。原始定稿不受任何影响：

```bash
curl 'http://localhost:8000/channels/news/finalized?view=effective'
```

```json
{
  "channel": "news",
  "watermark": 6,
  "view": "effective",
  "correction_version": 1,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕（已更正）", "start_seq": null, "end_seq": null, "correction_version": 1},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null}
  ]
}
```

非法的 `view` 值返回 HTTP 422。

### 追加更正（不可变更正层）

定稿字幕如有错字 / 翻译问题，可对**单个已定稿序号**追加更正。更正不修改任何旧数据，只向 `corrections` 表追加一个新版本：

```bash
curl -X POST http://localhost:8000/channels/news/finalized/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "text": "各位观众晚上好", "reason": "原稿漏字"}'
```

请求字段：

- `expected_version`（≥0，必填）：客户端认为的该序号**当前更正版本**。没有更正时当前版本为 `0`，首个更正必须提交 `0`；此后必须等于最新版本号；
- `text`（非空字符串）：更正后的字幕文本；
- `reason`（非空字符串）：更正原因，随审计记录永久保留。

成功响应（原子追加下一版本，返回该序号新的更正版本）：

```json
{
  "channel": "news",
  "seq": 1,
  "correction_version": 1,
  "text": "各位观众晚上好",
  "reason": "原稿漏字",
  "created_at": "2026-09-20T12:00:00.000Z"
}
```

规则：

- **仅允许更正已定稿字幕**：未定稿序号（尚未进入连续前缀）或已裁决为永久缺口的序号一律拒绝，返回 HTTP 409 且**不产生任何写入**；
- **乐观版本控制**：`expected_version` 与当前版本不一致时返回 HTTP 409 `version_conflict`，客户端应重新拉取生效视图 / 审计记录后携带新版本重试；
- 「读取当前版本 → 校验 → 追加下一版本」在单个 `BEGIN IMMEDIATE` 事务内完成，并发竞争请求中只有一个成功，其余全部得到版本冲突；
- 更正只追加：每个 `(channel, seq)` 的版本号从 1 起严格递增、无空洞；存储层触发器禁止 `UPDATE` / `DELETE`，**原始定稿文本永远不被改写**，随时可用 `view=original` 取回。

409 响应体（更正接口）：

| `error`             | 含义                                                    |
| ------------------- | ------------------------------------------------------- |
| `not_finalized`     | 序号未定稿（频道不存在 / 序号还在缓冲区或水位线之外）   |
| `seq_is_gap`        | 序号已裁决为永久缺口，缺口不可更正                      |
| `version_conflict`  | `expected_version` 与当前更正版本不一致（竞争失败）     |

```json
{"error": "version_conflict", "message": "expected_version=0 与当前更正版本 1 不一致"}
```

### 查询更正审计

按更正版本**升序**返回某定稿序号的全部更正记录（不可变、可追溯）：

```bash
curl http://localhost:8000/channels/news/finalized/1/corrections
```

```json
{
  "channel": "news",
  "seq": 1,
  "current_version": 2,
  "items": [
    {"channel": "news", "seq": 1, "version": 1, "text": "各位观众晚上好", "reason": "原稿漏字", "created_at": "2026-09-20T12:00:00.000Z"},
    {"channel": "news", "seq": 1, "version": 2, "text": "各位观众朋友们晚上好", "reason": "主持人改口", "created_at": "2026-09-20T12:05:00.000Z"}
  ]
}
```

没有更正的定稿序号返回 `current_version=0` 与空 `items`；缺口 / 未定稿 / 不存在的序号返回 HTTP 404。

### 创建发布快照

对频道当前定稿状态做一次不可变冻结，供下游按 `snapshot_id` 分批拉取同一时刻的稳定字幕：

```bash
curl -X POST http://localhost:8000/channels/news/snapshots \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key": "run-2026-09-20T12:00"}'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 6,
  "idempotency_key": "run-2026-09-20T12:00",
  "idempotent": false,
  "created_at": "2026-09-20T12:00:00.000Z"
}
```

- 请求只需 `idempotency_key`（非空字符串，去除首尾空白后不得为空）；
- `idempotent=false` 表示本次新建；相同频道携带相同 key 重试时不重新冻结，返回原快照且 `idempotent=true`（即使频道水位线已推进，`watermark` 仍是创建时刻锁定的值）；
- 不存在的频道可直接创建快照，得到 `watermark=0` 的空快照。

### 分页拉取快照

按 `snapshot_id` + `after`（序号游标，字幕与缺口标记统一计数）+ `limit`（默认 1000，1..10000）分页，按序号升序混合返回冻结的字幕与缺口标记。默认 `view=original`，响应结构与定稿查询一致（不带 `view` / `correction_version` 字段）：

```bash
curl 'http://localhost:8000/snapshots/b1f3c2a4-..../items?after=0&limit=1000'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 6,
  "view": "original",
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4}
  ]
}
```

`view=effective` 返回**快照创建时刻冻结**的最新更正文本（不是查询时刻的最新更正），并附带 `correction_version`（字幕 0=快照时未更正；缺口为 null），顶层 `correction_version` 为本页最大值：

```bash
curl 'http://localhost:8000/snapshots/b1f3c2a4-..../items?view=effective&after=0&limit=2'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 6,
  "view": "effective",
  "correction_version": 1,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕（快照时的更正）", "start_seq": null, "end_seq": null, "correction_version": 1},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null}
  ]
}
```

快照不存在返回 HTTP 404 `snapshot_not_found`；`after<0`、`limit` 越界、非法 `view` 返回 HTTP 422。

### 查询快照内更正历史

返回某字幕序号在**快照时刻冻结**的全部更正版本（按 `version` 升序），与实时审计接口结构对应：

```bash
curl http://localhost:8000/snapshots/b1f3c2a4-..../items/1/corrections
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "seq": 1,
  "current_version": 1,
  "items": [
    {"seq": 1, "version": 1, "text": "第一条字幕（快照时的更正）", "reason": "原稿漏字", "created_at": "2026-09-20T11:00:00.000Z"}
  ]
}
```

| 状态码 | `error`                | 含义                                                       |
| ------ | ---------------------- | ---------------------------------------------------------- |
| 404    | `snapshot_not_found`   | 快照不存在                                                 |
| 404    | `seq_out_of_snapshot`  | 序号小于 1 或超出快照锁定的 watermark（空快照全部越界）    |
| 409    | `seq_is_gap`           | 该序号在快照中是永久缺口，缺口没有更正历史                 |

### 消费者领取批次（租约式拉取）

下游消费者以名称（`consumer`）在频道上独立维护已确认游标。领取时服务在事务内从该消费者已确认游标之后返回一个按序号升序的**连续批次**（字幕与缺口标记混合，结构与定稿查询一致），并签发一个带过期时间的 `lease_token`：

```bash
curl -X POST 'http://localhost:8000/channels/news/consumers/dlq-worker/claims?limit=100'
```

```json
{
  "channel": "news",
  "consumer": "dlq-worker",
  "cursor": 0,
  "watermark": 6,
  "lease_token": "vL19lX85R-zmdVbHfL0YqRsJzxrE3DqNtQQncri_LI8",
  "expires_at": "2026-09-21T12:00:30.000Z",
  "start_seq": 1,
  "end_seq": 6,
  "limit": 100,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

规则：

- `cursor` 是领取时该消费者的已确认游标，`start_seq = cursor + 1`，`end_seq` 为本批末个序号；`limit`（1..10000，默认 100）截断本批条目数；
- **未过期且未完整确认的租约存续期间，同一消费者重复领取得到 409 `lease_active`**，服务不改写任何状态；不同消费者各自持有独立租约，互不阻塞；
- 游标之后暂无可领取序号（已追上频道水位线）时返回 `"items": []`、`lease_token=null`，且不写租约；
- 领取之后新定稿的序号不改变在途租约，下一次领取才可见。

### 确认批次

处理完一个序号后调用确认接口，游标按 `expected_cursor+1` 推进。必须同时携带领取时签发的 `lease_token` 与服务端当前游标：

```bash
curl -X POST http://localhost:8000/channels/news/consumers/dlq-worker/claims/confirm \
  -H 'Content-Type: application/json' \
  -d '{"lease_token": "vL19lX85R-zmdVbHfL0YqRsJzxrE3DqNtQQncri_LI8", "expected_cursor": 0}'
```

```json
{
  "channel": "news",
  "consumer": "dlq-worker",
  "cursor": 1,
  "watermark": 6,
  "lease_token": "vL19lX85R-zmdVbHfL0YqRsJzxrE3DqNtQQncri_LI8",
  "fully_confirmed": false,
  "confirmed_at": null
}
```

- 每次确认只推进一个序号：继续确认时 `expected_cursor` 依次传 `1, 2, 3, …`；`fully_confirmed=false` / `confirmed_at=null` 表示批次尚未完整确认，租约在有效期内可继续使用；
- 推进到批次 `end_seq` 时 `fully_confirmed=true` 并回填 `confirmed_at`，租约完整消费，下一次领取从新游标继续；
- 「校验令牌与游标 → 回填租约确认信息 → 推进消费者游标」在单个 `BEGIN IMMEDIATE` 事务内原子完成，竞争请求中恰好一个成功。

409 / 404 响应体：

| 状态码 | `error`                 | 含义                                                                 |
| ------ | ----------------------- | -------------------------------------------------------------------- |
| 409    | `lease_active`          | 领取时仍持有未过期且未完整确认的租约，不得重复领取                   |
| 409    | `invalid_lease_token`   | 令牌不存在或不属于该消费者                                           |
| 409    | `lease_superseded`      | 令牌属于旧租约，已被过期后的重新领取取代                             |
| 409    | `lease_expired`         | 租约已到期（重新领取未确认批次后再确认）                             |
| 409    | `lease_consumed`        | 租约已完整确认，不能重复确认                                         |
| 409    | `cursor_conflict`       | `expected_cursor` 与服务端已确认游标不一致（并发落后/超前请求）      |
| 409    | `cursor_out_of_batch`   | 推进目标 `expected_cursor+1` 超出本批次 `end_seq`                    |
| 404    | `consumer_not_found`    | 消费者不存在（从未领取）                                             |

租约到期后的典型恢复流程：确认得到 409 `lease_expired` → 重新调用领取接口拿到新 `lease_token`（批次从未确认游标之后重新开始，已确认过的序号不会重复发放）→ 用新令牌继续确认。

### 查询消费者状态

```bash
curl http://localhost:8000/channels/news/consumers/dlq-worker/status
```

```json
{
  "channel": "news",
  "consumer": "dlq-worker",
  "cursor": 2,
  "watermark": 6,
  "lag": 4,
  "created_at": "2026-09-21T11:58:00.000Z",
  "latest_lease": {
    "lease_token": "vL19lX85R-....",
    "status": "active",
    "start_seq": 3,
    "end_seq": 6,
    "expires_at": "2026-09-21T12:01:30.000Z",
    "confirmed_cursor": null,
    "confirmed_at": null,
    "created_at": "2026-09-21T12:01:00.000Z"
  }
}
```

- `cursor` 为已确认游标，`watermark` 为频道当前水位线，`lag = watermark - cursor` 表示落后量；
- `latest_lease.status`：`active`（未过期且未完整确认，可能已有部分确认进度）、`expired`（到期未完整确认，可重新领取）、`confirmed`（已完整确认）；
- 从未领取过的消费者返回 404 `consumer_not_found`。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                     # 含乱序、修订竞争、缺口裁决、幂等/重叠、更正版本冲突、视图合并、审计、发布快照冻结/幂等/分页/不可变、消费者租约领取/确认/过期/并发等用例
uvicorn app.main:app --reload --port 8000
```

## 实现要点

- **事务化提交 / 裁决 / 更正 / 快照**：`app/service.py` 中「校验 → 写入」在单个 `BEGIN IMMEDIATE` 事务内完成；缺口裁决、更正与快照发布的所有校验先于任何写入执行，失败即只读返回，不产生副作用。`finalized` / `gaps` 表主键 `(channel, seq)`、`corrections` 表主键 `(channel, seq, version)`、`snapshots` 的 `UNIQUE(channel, idempotency_key)` 分别作为防重复定稿 / 裁决 / 版本号重复 / 幂等键重复的最后防线。
- **连续前缀推进**：`_consume_contiguous` 从给定水位线的下一位起逐条消费缓冲区，遇到第一个缺失即停止；片段补齐与缺口裁决都复用该逻辑。
- **缺口存储**：`gap_decisions` 记录每次裁决区间及其当时释放到的序号（用于幂等返回原结果），`gaps` 按被跳过序号逐行落表，定稿查询用 `UNION ALL` 按序合并字幕与缺口标记。
- **追加式更正层**：`corrections(channel, seq, version, text, reason, created_at)` 仅通过 `INSERT` 追加，`(channel, seq)` 外键引用 `finalized`——缺口或未定稿序号在存储层即无法插入；版本号按序号从 1 递增，`expected_version` 乐观并发控制 + `BEGIN IMMEDIATE` 保证竞争下唯一成功；`corrections_no_update` / `corrections_no_delete` 触发器在存储层禁止任何改写与删除，原始定稿保持不可变。生效视图用相关子查询连接每个序号的最新版本（`MAX(version)`）合并文本，审计查询按 `version` 升序返回完整历史。
- **发布快照**：`snapshots(snapshot_id, channel, watermark, idempotency_key, created_at)` 记录冻结时刻，`snapshot_items(snapshot_id, seq, …)` 对 `1..watermark` 每个序号落一行——字幕行同时存 `original_text`、创建时刻经 `MAX(version)` 连接冻结的 `effective_text` 及其 `correction_version`，缺口行存裁决区间端点；`snapshot_corrections(snapshot_id, seq, version, …)` 逐版本复制快照时刻已有的全部更正记录。创建先按 `(channel, idempotency_key)` 查重，命中即幂等返回；全部插入在一个写事务内完成，故冻结视图与锁定水位线严格一致。三张快照表各自配备 `no_update` / `no_delete` 触发器，快照一旦生成在存储层即不可变；分页与快照内审计只读取快照表，后续定稿、更正、缺口对其零影响。不存在的频道不写 `channels` 行，直接生成 `watermark=0` 的空快照。
- **命名消费者租约**：`consumers(channel, consumer, cursor, created_at)` 保存每个消费者的已确认游标；`consumer_leases(channel, consumer, lease_token, start_seq, end_seq, expires_at, confirmed_cursor, confirmed_at, created_at)` 每次领取追加一行，`lease_token` 为 256 位随机令牌，到期时刻在签发时按 `LEASE_TTL_SECONDS` 固化。领取路径在单个 `BEGIN IMMEDIATE` 事务内检查最近租约（按自增 rowid）：未过期且未完整确认即 409 `lease_active`，过期则保留旧行（旧令牌确认时得到 409 `lease_expired` / `lease_superseded`）并签发新租约；批次内容复用定稿查询的字幕/缺口合并逻辑。确认路径所有校验（令牌归属与新旧、`confirmed_at`、到期时间、`expected_cursor == cursor`、`cursor+1 <= end_seq`）全部先于任何写入执行，随后在同一事务内回填租约确认信息并推进消费者游标；部分确认只更新 `confirmed_cursor` 展示进度，`confirmed_at` 仅在推进到批次末位时写入。
- **存储**：`channels`（水位线）、`fragments`（待定稿缓冲）、`finalized`（定稿，不可变）、`gap_decisions` / `gaps`（永久缺口）、`corrections`（追加式更正）、`snapshots` / `snapshot_items` / `snapshot_corrections`（不可变发布快照）、`consumers` / `consumer_leases`（消费者游标与领取租约）十一张表；WAL 模式 + `busy_timeout`。建表语句与触发器均幂等（`CREATE TABLE/TRIGGER IF NOT EXISTS`），已部署实例重启后自动补建新表与触发器，原有数据与接口行为不受影响。
- **容器化**：`docker compose up --build` 一键启动，数据库落在命名卷 `subtitles-data`；服务固定监听 **8000** 端口。
