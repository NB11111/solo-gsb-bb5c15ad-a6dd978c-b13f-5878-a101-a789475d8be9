from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class FragmentIn(BaseModel):
    seq: int = Field(..., ge=1, description="片段序号，从 1 开始连续编号")
    revision: int = Field(..., ge=0, description="修订号，同一序号只接受更大的值")
    text: str = Field(..., description="字幕文本")


class GapDecisionIn(BaseModel):
    start_seq: int = Field(..., ge=1, description="缺口起点序号，必须等于当前水位线下一位")
    end_seq: int = Field(..., ge=1, description="缺口终点序号（含），不得小于起点")

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "GapDecisionIn":
        if self.end_seq < self.start_seq:
            raise ValueError("end_seq 不得小于 start_seq")
        return self


class FinalizedItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="修订号（仅字幕条目存在）")
    text: Optional[str] = Field(None, description="字幕文本（仅字幕条目存在）")
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")


class FinalizedEffectiveItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕（已合并最新更正）；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="原始定稿修订号（仅字幕条目存在）")
    text: Optional[str] = Field(None, description="生效文本：original 视图为定稿原文，effective 视图为最新更正文本")
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")
    correction_version: Optional[int] = Field(
        None,
        description=(
            "该序号的当前更正版本：字幕条目恒返回，0=未更正、>0=已合并的更正版本；"
            "缺口条目为 null。仅 effective 视图返回该字段"
        ),
    )


class SubmitResponse(BaseModel):
    channel: str
    seq: int
    applied: bool = Field(..., description="本次提交是否生效（被接受并写入缓冲区）")
    reason: Optional[str] = Field(
        None,
        description="未生效原因：stale_revision=修订号不够新；already_finalized=序号已定稿",
    )
    watermark: int = Field(..., description="提交处理完成后的最新水位线")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次提交触发新定稿的片段（缺口补齐时可能一次释放多条）"
    )


class SkippedGap(BaseModel):
    start_seq: int
    end_seq: int


class GapDecisionResponse(BaseModel):
    channel: str
    applied: bool = Field(..., description="裁决是否生效；false 时不产生任何写入")
    idempotent: bool = Field(..., description="是否为重复提交已完整裁决区间的幂等返回")
    reason: Optional[str] = Field(
        None,
        description=(
            "未生效原因：start_not_at_watermark=起点不是水位线下一位；"
            "buffered_fragments=区间内存在已缓冲片段；"
            "already_finalized=区间已完全定稿；gap_overlap=与既有裁决部分重叠"
        ),
    )
    watermark: int = Field(..., description="裁决处理完成后的最新水位线")
    skipped: Optional[SkippedGap] = Field(None, description="本次（或原裁决）标记永久丢失的区间")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次（或原裁决时）随裁决新定稿的连续片段"
    )


class FinalizedResponse(BaseModel):
    channel: str
    watermark: int
    items: List[FinalizedItem] = Field(
        ..., description="按序号升序混合返回字幕（kind=subtitle）与缺口标记（kind=gap）"
    )


class FinalizedEffectiveResponse(BaseModel):
    channel: str
    watermark: int
    view: Literal["effective"] = Field("effective", description="视图：effective=合并最新更正文本")
    correction_version: int = Field(
        ..., description="本页范围内出现过的最大更正版本（0 表示本页均无更正）"
    )
    items: List[FinalizedEffectiveItem] = Field(
        ..., description="按序号升序混合返回生效字幕（已合并最新更正）与缺口标记"
    )


# ---------- 追加式更正层 ----------


class CorrectionIn(BaseModel):
    expected_version: int = Field(
        ...,
        ge=0,
        description="客户端认为的当前更正版本：无更正时必须为 0，之后必须等于该序号最新更正版本",
    )
    text: str = Field(..., min_length=1, description="更正后的字幕文本")
    reason: str = Field(..., min_length=1, description="更正原因（随审计记录永久保留）")

    @field_validator("text", "reason")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class CorrectionResponse(BaseModel):
    channel: str
    seq: int = Field(..., description="被更正的定稿序号")
    correction_version: int = Field(..., description="本次追加后该序号的更正版本（首个更正为 1）")
    text: str = Field(..., description="本次更正文本（原始定稿不受影响，仍可经 view=original 查询）")
    reason: str
    created_at: str = Field(..., description="更正落库时间（UTC ISO8601）")


class CorrectionAuditItem(BaseModel):
    channel: str
    seq: int
    version: int = Field(..., description="更正版本，从 1 起递增")
    text: str = Field(..., description="该版本的更正文本")
    reason: str
    created_at: str


class CorrectionAuditResponse(BaseModel):
    channel: str
    seq: int
    current_version: int = Field(..., description="该序号当前更正版本（0 表示尚无更正）")
    items: List[CorrectionAuditItem] = Field(
        ..., description="该序号的全部更正记录，按 version 升序"
    )


class ErrorResponse(BaseModel):
    error: str = Field(..., description="错误码")
    message: str


# ---------- 发布快照 ----------


class SnapshotCreateIn(BaseModel):
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description=(
            "幂等键（频道范围内唯一）：同一 channel 使用相同 key 重试，"
            "始终返回同一快照，不会重复冻结"
        ),
    )

    @field_validator("idempotency_key")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class SnapshotResponse(BaseModel):
    snapshot_id: str = Field(..., description="快照 ID（UUID），用于分页拉取与更正历史查询")
    channel: str
    watermark: int = Field(..., description="创建时刻锁定的频道水位线")
    idempotency_key: str
    idempotent: bool = Field(
        ...,
        description="false=本次新建快照；true=相同 idempotency_key 重试，返回既有快照",
    )
    created_at: str = Field(..., description="快照创建时间（UTC ISO8601），即冻结时刻")


class SnapshotItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="原始修订号（仅字幕条目存在）")
    text: Optional[str] = Field(
        None,
        description="快照冻结的原始定稿文本（仅字幕条目存在）",
    )
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")


class SnapshotEffectiveItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕（快照时刻生效文本）；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="原始修订号（仅字幕条目存在）")
    text: Optional[str] = Field(
        None,
        description="快照时刻冻结的最新更正文本（仅字幕条目存在）",
    )
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")
    correction_version: Optional[int] = Field(
        None,
        description=(
            "该序号在快照时刻的最新更正版本：字幕条目恒返回，0=创建时未更正；"
            "缺口条目为 null"
        ),
    )


class SnapshotItemsResponse(BaseModel):
    snapshot_id: str
    channel: str
    watermark: int = Field(..., description="快照锁定的水位线，也是条目总数")
    view: Literal["original"] = Field("original")
    items: List[SnapshotItem] = Field(
        ..., description="本页快照条目（kind=subtitle / gap），按 seq 升序"
    )


class SnapshotItemsEffectiveResponse(BaseModel):
    snapshot_id: str
    channel: str
    watermark: int
    view: Literal["effective"] = Field("effective")
    correction_version: int = Field(
        ..., description="本页范围内出现过的最大快照更正版本（0 表示本页均未更正）"
    )
    items: List[SnapshotEffectiveItem] = Field(
        ..., description="本页快照条目：字幕返回快照时刻冻结的生效文本，按 seq 升序"
    )


class SnapshotCorrectionItem(BaseModel):
    seq: int
    version: int = Field(..., description="更正版本，从 1 起递增")
    text: str = Field(..., description="该版本的更正文本（快照时刻冻结）")
    reason: str
    created_at: str


class SnapshotCorrectionsResponse(BaseModel):
    snapshot_id: str
    channel: str
    seq: int
    current_version: int = Field(
        ..., description="该序号在快照时刻的最新更正版本（0 表示快照时尚无更正）"
    )
    items: List[SnapshotCorrectionItem] = Field(
        ..., description="快照冻结的该序号全部更正记录，按 version 升序"
    )


# ---------- 命名消费者拉取游标与租约 ----------


class ClaimResponse(BaseModel):
    channel: str
    consumer: str
    cursor: int = Field(
        ..., description="领取时该消费者的已确认游标（本批序号均大于该值）"
    )
    watermark: int = Field(..., description="领取时频道水位线，cursor<watermark 表示尚有落后")
    lease_token: Optional[str] = Field(
        None,
        description=(
            "本次批次的租约令牌（确认时必须原样回传）；"
            "游标之后暂无可领取序号时为 null，且不签发租约"
        ),
    )
    expires_at: Optional[str] = Field(
        None, description="租约到期时间（UTC ISO8601）；无批次时为 null"
    )
    start_seq: Optional[int] = Field(None, description="本批次首个序号（=领取时 cursor+1）")
    end_seq: Optional[int] = Field(None, description="本批次末个序号")
    limit: int = Field(..., description="本次领取允许返回的最大条目数")
    items: List[FinalizedItem] = Field(
        default_factory=list,
        description="按序号升序连续返回的字幕与缺口标记，游标之后无新定稿时为空",
    )


class ConfirmIn(BaseModel):
    lease_token: str = Field(..., min_length=1, description="领取接口签发的租约令牌")
    expected_cursor: int = Field(
        ...,
        ge=0,
        description=(
            "客户端认为的当前已确认游标：必须与服务端一致，"
            "确认成功后游标推进 expected_cursor+1；可在租约有效期内"
            "逐条确认直到批次末位"
        ),
    )

    @field_validator("lease_token")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class ConfirmResponse(BaseModel):
    channel: str
    consumer: str
    cursor: int = Field(..., description="确认后的新已确认游标")
    watermark: int = Field(..., description="确认时频道水位线")
    lease_token: str = Field(..., description="本次确认消费的租约令牌")
    fully_confirmed: bool = Field(
        ..., description="true=游标已推进到批次末位，租约完整消费；false=部分确认，租约有效期内可继续确认剩余序号"
    )
    confirmed_at: Optional[str] = Field(
        None,
        description="完整确认时间（UTC ISO8601）；部分确认（游标未到批次末位）时为 null",
    )


class LeaseInfo(BaseModel):
    lease_token: str = Field(..., description="租约令牌")
    status: Literal["active", "expired", "confirmed"] = Field(
        ...,
        description="active=未过期且未完整确认（含仅部分确认）；expired=过期未完整确认（可重新领取剩余批次）；confirmed=已完整确认",
    )
    start_seq: int
    end_seq: int
    expires_at: str = Field(..., description="租约到期时间（UTC ISO8601）")
    confirmed_cursor: Optional[int] = Field(None, description="该租约确认后推进到的游标（未确认为 null）")
    confirmed_at: Optional[str] = Field(None, description="确认时间（UTC ISO8601），未确认为 null")
    created_at: str = Field(..., description="租约签发时间（UTC ISO8601）")


class ConsumerStatusResponse(BaseModel):
    channel: str
    consumer: str
    cursor: int = Field(..., description="已确认游标（最后确认到的序号，0 表示尚未确认任何序号）")
    watermark: int = Field(..., description="频道当前水位线")
    lag: int = Field(..., description="落后量 = watermark - cursor（不为负）")
    created_at: str = Field(..., description="消费者首次领取注册时间（UTC ISO8601）")
    latest_lease: Optional[LeaseInfo] = Field(
        None, description="最近一次领取的租约及其状态；从未领取时为 null"
    )
