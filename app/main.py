from contextlib import asynccontextmanager
from typing import Literal, Union

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import service
from .db import init_db
from .schemas import (
    ClaimResponse,
    ConfirmIn,
    ConfirmResponse,
    ConsumerStatusResponse,
    CorrectionAuditResponse,
    CorrectionIn,
    CorrectionResponse,
    ErrorResponse,
    FinalizedEffectiveResponse,
    FinalizedResponse,
    FragmentIn,
    GapDecisionIn,
    GapDecisionResponse,
    SnapshotCorrectionsResponse,
    SnapshotCreateIn,
    SnapshotItemsEffectiveResponse,
    SnapshotItemsResponse,
    SnapshotResponse,
    SubmitResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动初始化数据库（建表幂等）
    init_db()
    yield


app = FastAPI(
    title="Live Subtitle Finalizer",
    summary="直播字幕定稿服务：乱序片段缓冲、按连续前缀推进水位线并定稿；"
    "支持追加式更正、不可变发布快照与命名消费者租约式拉取游标",
    version="1.3.0",
    lifespan=lifespan,
)


@app.exception_handler(service.CorrectionError)
def correction_error_handler(request, exc: service.CorrectionError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "message": exc.message},
    )


@app.exception_handler(service.SnapshotError)
def snapshot_error_handler(request, exc: service.SnapshotError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "message": exc.message},
    )


@app.exception_handler(service.LeaseError)
def lease_error_handler(request, exc: service.LeaseError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "message": exc.message},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/channels/{channel}/fragments", response_model=SubmitResponse)
def submit_fragment(channel: str, body: FragmentIn) -> dict:
    """提交字幕片段。片段可乱序到达；同一序号只接受更高修订号。"""
    return service.submit_fragment(channel, body.seq, body.revision, body.text)


@app.post("/channels/{channel}/gaps", response_model=GapDecisionResponse)
def decide_gap(channel: str, body: GapDecisionIn) -> dict:
    """裁决永久缺口。

    区间起点必须等于当前水位线下一位，且区间内不得存在已缓冲片段；
    成功后跳过该区间并继续释放紧随其后的连续缓冲片段。重复提交
    已完整裁决的同一区间幂等返回，部分重叠拒绝。
    """
    return service.decide_gap(channel, body.start_seq, body.end_seq)


@app.get(
    "/channels/{channel}/finalized",
    response_model=Union[FinalizedResponse, FinalizedEffectiveResponse],
)
def get_finalized(
    channel: str,
    after: int = Query(0, ge=0, description="只返回序号大于该值的定稿片段与缺口标记"),
    limit: int = Query(1000, ge=1, le=10000),
    view: Literal["original", "effective"] = Query(
        "original",
        description="original=原始定稿（默认，响应结构保持兼容）；"
        "effective=合并最新更正文本并返回 correction_version",
    ),
) -> dict:
    """查询频道已定稿字幕、永久缺口标记与当前水位线（按序号升序混合返回）。"""
    return service.get_finalized(channel, after=after, limit=limit, view=view)


@app.post(
    "/channels/{channel}/finalized/{seq}/corrections",
    response_model=CorrectionResponse,
    status_code=200,
    responses={
        409: {"description": "序号未定稿/为缺口，或 expected_version 与当前版本冲突",
              "model": ErrorResponse},
        422: {"description": "请求体校验失败"},
    },
)
def submit_correction(channel: str, seq: int, body: CorrectionIn) -> dict:
    """对已定稿字幕追加一条不可变更正。

    - 仅允许更正已定稿字幕；缺口序号或未定稿序号一律拒绝（409）且不写入；
    - `expected_version` 必须等于该序号当前更正版本（无更正时为 0）；
    - 成功时在同一 SQLite 事务内原子追加下一版本，返回新的 correction_version；
    - 并发竞争中版本不匹配的请求得到 409 version_conflict；
    - 原始定稿永不改写，只能通过 `view=original` 查询。
    """
    return service.submit_correction(
        channel, seq, body.expected_version, body.text, body.reason
    )


@app.get(
    "/channels/{channel}/finalized/{seq}/corrections",
    response_model=CorrectionAuditResponse,
    responses={404: {"description": "该序号不存在已定稿字幕", "model": ErrorResponse}},
)
def list_corrections(channel: str, seq: int) -> dict:
    """查询某定稿序号的全部更正记录，按更正版本（version）升序返回。"""
    return service.list_corrections(channel, seq)


# ---------- 发布快照 ----------


@app.post(
    "/channels/{channel}/snapshots",
    response_model=SnapshotResponse,
    status_code=200,
    responses={422: {"description": "请求体校验失败（idempotency_key 为空等）"}},
)
def create_snapshot(channel: str, body: SnapshotCreateIn) -> dict:
    """为频道创建发布快照。

    - 单个 `BEGIN IMMEDIATE` 事务内锁定频道当前 watermark，并把
      1..watermark 的字幕（原始 + 快照时刻生效文本）、缺口标记及各条
      更正版本整体固化到快照表；
    - 同一频道携带相同 `idempotency_key` 重试始终返回同一快照
      （`idempotent=true`），不会重复冻结；
    - 不存在的频道也可创建 `watermark=0` 的空快照；
    - 快照创建后，后续新定稿、新更正或新缺口都不会改变既有快照。
    """
    return service.create_snapshot(channel, body.idempotency_key)


@app.get(
    "/snapshots/{snapshot_id}/items",
    response_model=Union[SnapshotItemsResponse, SnapshotItemsEffectiveResponse],
    responses={404: {"description": "快照不存在", "model": ErrorResponse}},
)
def get_snapshot_items(
    snapshot_id: str,
    after: int = Query(0, ge=0, description="只返回序号大于该值的快照条目（字幕与缺口统一计数）"),
    limit: int = Query(1000, ge=1, le=10000),
    view: Literal["original", "effective"] = Query(
        "original",
        description="original=快照冻结的原始定稿（默认）；"
        "effective=快照时刻冻结的最新更正文本并返回 correction_version",
    ),
) -> dict:
    """按 snapshot_id 分页拉取快照条目（按序号升序混合字幕与缺口标记）。"""
    return service.get_snapshot(snapshot_id, after=after, limit=limit, view=view)


@app.get(
    "/snapshots/{snapshot_id}/items/{seq}/corrections",
    response_model=SnapshotCorrectionsResponse,
    responses={
        404: {"description": "快照不存在，或该序号超出快照水位线", "model": ErrorResponse},
        409: {"description": "该序号在快照中为永久缺口", "model": ErrorResponse},
    },
)
def list_snapshot_corrections(snapshot_id: str, seq: int) -> dict:
    """查询快照内某字幕序号在快照时刻冻结的全部更正版本（按 version 升序）。"""
    return service.list_snapshot_corrections(snapshot_id, seq)


# ---------- 命名消费者拉取游标与租约 ----------


@app.post(
    "/channels/{channel}/consumers/{consumer}/claims",
    response_model=ClaimResponse,
    status_code=200,
    responses={
        409: {"description": "该消费者仍持有未过期且未确认的租约", "model": ErrorResponse},
        422: {"description": "limit 越界等请求参数校验失败"},
    },
)
def claim_batch(
    channel: str,
    consumer: str,
    limit: int = Query(
        100, ge=1, le=10000, description="本批最多返回的条目数（字幕与缺口标记统一计数）"
    ),
) -> dict:
    """消费者从已确认游标后领取连续批次（含缺口标记）并获得带过期时间的租约。

    - 整个“读游标 -> 检查最近租约 -> 返回批次 -> 签发 lease_token”过程在
      单个 `BEGIN IMMEDIATE` 事务内完成；
    - 未过期且未确认的租约存续期间，同一消费者重复领取得到 409
      `lease_active`，服务不改写任何状态；
    - 租约到期后允许重新领取同一未确认批次（旧令牌失效）；
    - 游标之后暂无可领取序号时返回空 `items` 且不签发租约（`lease_token=null`）。
    """
    return service.claim_batch(channel, consumer, limit=limit)


@app.post(
    "/channels/{channel}/consumers/{consumer}/claims/confirm",
    response_model=ConfirmResponse,
    status_code=200,
    responses={
        404: {"description": "消费者不存在（从未领取）", "model": ErrorResponse},
        409: {
            "description": (
                "lease_token 无效/过期/已消费/被新领取取代，或 expected_cursor "
                "与服务端游标不一致，或推进目标超出本批次范围；任何失败都不改写状态"
            ),
            "model": ErrorResponse,
        },
        422: {"description": "请求体校验失败（lease_token 为空、expected_cursor<0 等）"},
    },
)
def confirm_batch(channel: str, consumer: str, body: ConfirmIn) -> dict:
    """确认已处理下一个序号，把消费者游标推进到 expected_cursor+1。

    - 必须同时通过 `lease_token` 与 `expected_cursor` 校验，只允许把游标
      推进到该批次 `[start_seq, end_seq]` 内的序号；租约有效期内可逐条
      确认，推进到 end_seq 时租约完整消费；
    - 过期令牌、旧令牌（已被重新领取取代）、已完整消费的令牌、越界
      推进或并发落后的请求（expected_cursor 与服务端不一致）一律返回
      409，且不改写任何状态；
    - 租约到期后允许重新领取同一未确认批次（从当前已确认游标之后开始），
      确认成功后下一次领取从新游标继续。
    """
    return service.confirm_batch(
        channel, consumer, body.lease_token, body.expected_cursor
    )


@app.get(
    "/channels/{channel}/consumers/{consumer}/status",
    response_model=ConsumerStatusResponse,
    responses={404: {"description": "消费者不存在（从未领取）", "model": ErrorResponse}},
)
def get_consumer_status(channel: str, consumer: str) -> dict:
    """查询命名消费者状态：已确认游标、频道水位线、落后量与最近租约状态。"""
    return service.get_consumer_status(channel, consumer)
