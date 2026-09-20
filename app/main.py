from contextlib import asynccontextmanager
from typing import Literal, Union

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import service
from .db import init_db
from .schemas import (
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
    "支持追加式更正与不可变发布快照",
    version="1.2.0",
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
