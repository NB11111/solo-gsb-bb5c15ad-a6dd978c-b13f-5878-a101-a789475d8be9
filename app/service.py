"""核心业务：片段接收、水位线推进、永久缺口裁决、追加式更正与发布快照。

不变式（由单事务保证）：
- finalized 与 gaps 中每个频道的序号恰好是 1..watermark 的连续前缀，
  每个序号要么是定稿字幕，要么是永久缺口标记；无缺口的片段只存在于
  fragments 缓冲区；
- 同一序号只保留最高修订号，定稿后不再接受任何修改；
- 缺口裁决区间起点必须等于当前水位线下一位，且区间内不得存在已缓冲
  片段，校验失败不产生任何写入；
- 重复提交已完整裁决的同一区间幂等返回当前结果，部分重叠一律拒绝。

更正层（corrections）不变式：
- 只允许更正 finalized 中的定稿字幕；缺口序号与未定稿序号一律拒绝且不写入；
- corrections 表只追加：version 在每个 (channel, seq) 内从 1 起单调递增，
  UPDATE/DELETE 被触发器禁止，原始定稿文本永远不被改写；
- expected_version 必须等于当前更正版本（无更正时为 0），否则返回
  version_conflict；“读取当前版本 -> 校验 -> 追加下一版本”在同一个
  BEGIN IMMEDIATE 事务内完成，竞争请求只有一个能成功。

发布快照（snapshots/snapshot_items/snapshot_corrections）不变式：
- “锁定当前 watermark -> 固化 1..watermark 的字幕 / 缺口及各条更正
  历史 -> 写入快照”在单个 BEGIN IMMEDIATE 事务内完成，冻结结果与
  创建时刻的定稿状态严格一致；
- 快照完全追加且不可变：同一频道的同一 idempotency_key 重试不重新
  冻结，始终返回原 snapshot_id；此后新定稿、新更正、新缺口只写入
  原表，任何既有快照内容都不随之改变；
- 不存在的频道也可创建 watermark=0 的空快照（不创建频道行）。

命名消费者拉取游标（consumers/consumer_leases）不变式：
- 每个 (channel, consumer) 持有一个已确认游标 cursor（从 0 起），
  领取在单个 BEGIN IMMEDIATE 事务内“读取游标 -> 判定最近租约状态 ->
  签发新租约并返回 cursor+1 起的连续批次（字幕与缺口标记混合）”，
  未过期且未确认的租约存续期间同一消费者不得重复领取（409
  lease_active），不改写任何状态；
- 确认必须同时通过 lease_token 与 expected_cursor 校验，且推进目标
  必须严格落在本批次 (start_seq, end_seq] 内；旧/已消费/过期令牌、
  游标不匹配、越界、并发落后请求一律 409 且不产生任何写入；
- 租约到期后旧令牌即刻失效（409 lease_expired），允许在同一事务内
  把未确认批次以新租约重新签发给同一消费者（从当前已确认游标之后
  开始）；部分确认后租约在有效期内仍可继续确认剩余序号，到期未确认
  完则可重新领取剩余批次。
"""

import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from .db import read_tx, write_tx


class CorrectionError(Exception):
    """更正 / 审计请求在业务前置条件上被拒绝；HTTP 层映射为 409/404。"""

    def __init__(self, error: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code


class SnapshotError(Exception):
    """快照分页 / 审计请求在业务前置条件上被拒绝；HTTP 层映射为 404/409。"""

    def __init__(self, error: str, message: str, status_code: int = 404):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code


class LeaseError(Exception):
    """消费者领取 / 确认请求在业务前置条件上被拒绝；HTTP 层映射为 409/404。"""

    def __init__(self, error: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code


def submit_fragment(channel: str, seq: int, revision: int, text: str) -> dict:
    """提交一个字幕片段，返回是否生效、最新水位线及本次新定稿的片段。"""
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, 0) "
            "ON CONFLICT(channel) DO NOTHING",
            (channel,),
        )
        watermark = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()["watermark"]

        applied = False
        reason = None
        if seq <= watermark:
            # 已定稿（或已裁决为缺口）的序号不可再修改
            reason = "already_finalized"
        else:
            row = conn.execute(
                "SELECT revision FROM fragments WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO fragments (channel, seq, revision, text) "
                    "VALUES (?, ?, ?, ?)",
                    (channel, seq, revision, text),
                )
                applied = True
            elif revision > row["revision"]:
                conn.execute(
                    "UPDATE fragments SET revision = ?, text = ? "
                    "WHERE channel = ? AND seq = ?",
                    (revision, text, channel, seq),
                )
                applied = True
            else:
                reason = "stale_revision"

        newly = _consume_contiguous(conn, channel, watermark)
        new_watermark = newly[-1]["seq"] if newly else watermark

        return {
            "channel": channel,
            "seq": seq,
            "applied": applied,
            "reason": reason,
            "watermark": new_watermark,
            "finalized": newly,
        }


def _consume_contiguous(conn, channel: str, watermark: int) -> list:
    """从 watermark+1 起消费连续的缓冲片段：移入定稿表并更新水位线。

    遇到第一个缺失（缺口或未到达）即停止，保证不会越过缺口定稿；
    缺口补齐（片段到达或被裁决为永久缺口）的操作会在一次事务里
    释放整段连续片段。
    """
    newly = []
    seq = watermark + 1
    while True:
        row = conn.execute(
            "SELECT revision, text FROM fragments WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if row is None:
            break
        conn.execute(
            "INSERT INTO finalized (channel, seq, revision, text) VALUES (?, ?, ?, ?)",
            (channel, seq, row["revision"], row["text"]),
        )
        conn.execute(
            "DELETE FROM fragments WHERE channel = ? AND seq = ?", (channel, seq)
        )
        newly.append(
            {
                "kind": "subtitle",
                "seq": seq,
                "revision": row["revision"],
                "text": row["text"],
                "start_seq": None,
                "end_seq": None,
            }
        )
        seq += 1
    if newly:
        conn.execute(
            "UPDATE channels SET watermark = ? WHERE channel = ?",
            (newly[-1]["seq"], channel),
        )
    return newly


def decide_gap(channel: str, start_seq: int, end_seq: int) -> dict:
    """裁决 [start_seq, end_seq] 为永久缺口。

    起点必须等于当前水位线下一位，区间内不得存在已缓冲片段。
    成功后在同一事务中记录被跳过序号、推进水位线，并继续释放紧随
    其后的连续缓冲片段。重复提交已完整裁决的同一区间幂等返回；
    与既有裁决部分重叠则拒绝。任何校验失败都不产生写入。
    """
    with write_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        if start_seq <= watermark:
            return _historical_gap_response(conn, channel, start_seq, end_seq, watermark)

        if start_seq != watermark + 1:
            return _rejected(channel, watermark, "start_not_at_watermark")

        buffered = conn.execute(
            "SELECT seq FROM fragments WHERE channel = ? AND seq BETWEEN ? AND ? "
            "ORDER BY seq LIMIT 1",
            (channel, start_seq, end_seq),
        ).fetchone()
        if buffered is not None:
            # 区间内已有缓冲片段，必须等片段定稿而不能裁决丢失
            return _rejected(channel, watermark, "buffered_fragments")

        # 校验通过：以下写入与水位线推进在同一事务内完成
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET watermark = excluded.watermark",
            (channel, end_seq),
        )
        conn.execute(
            "INSERT INTO gap_decisions (channel, start_seq, end_seq, released_until) "
            "VALUES (?, ?, ?, ?)",
            (channel, start_seq, end_seq, end_seq),
        )
        conn.executemany(
            "INSERT INTO gaps (channel, seq, start_seq, end_seq) VALUES (?, ?, ?, ?)",
            [(channel, seq, start_seq, end_seq) for seq in range(start_seq, end_seq + 1)],
        )

        released = _consume_contiguous(conn, channel, end_seq)
        if released:
            conn.execute(
                "UPDATE gap_decisions SET released_until = ? "
                "WHERE channel = ? AND start_seq = ? AND end_seq = ?",
                (released[-1]["seq"], channel, start_seq, end_seq),
            )
        new_watermark = released[-1]["seq"] if released else end_seq

        return {
            "channel": channel,
            "applied": True,
            "idempotent": False,
            "reason": None,
            "watermark": new_watermark,
            "skipped": {"start_seq": start_seq, "end_seq": end_seq},
            "finalized": released,
        }


def _rejected(channel: str, watermark: int, reason: str) -> dict:
    """构造拒绝响应：不执行任何写入。"""
    return {
        "channel": channel,
        "applied": False,
        "idempotent": False,
        "reason": reason,
        "watermark": watermark,
        "skipped": None,
        "finalized": [],
    }


def _historical_gap_response(
    conn, channel: str, start_seq: int, end_seq: int, watermark: int
) -> dict:
    """处理起点位于水位线之前的提交：完全相同的区间幂等返回，否则拒绝。"""
    decision = conn.execute(
        "SELECT end_seq, released_until FROM gap_decisions "
        "WHERE channel = ? AND start_seq = ? AND end_seq = ?",
        (channel, start_seq, end_seq),
    ).fetchone()
    if decision is not None:
        released = _fetch_finalized(
            conn, channel, after=end_seq, upto=decision["released_until"]
        )
        return {
            "channel": channel,
            "applied": True,
            "idempotent": True,
            "reason": None,
            "watermark": watermark,
            "skipped": {"start_seq": start_seq, "end_seq": end_seq},
            "finalized": released,
        }

    overlap = conn.execute(
        "SELECT 1 FROM gap_decisions "
        "WHERE channel = ? AND start_seq <= ? AND end_seq >= ? LIMIT 1",
        (channel, end_seq, start_seq),
    ).fetchone()
    reason = "gap_overlap" if overlap is not None else "already_finalized"
    return _rejected(channel, watermark, reason)


def _fetch_finalized(conn, channel: str, after: int, upto: int) -> list:
    """读取 (after, upto] 区间内随某次裁决/提交新定稿的字幕片段。"""
    rows = conn.execute(
        "SELECT seq, revision, text FROM finalized "
        "WHERE channel = ? AND seq > ? AND seq <= ? ORDER BY seq",
        (channel, after, upto),
    ).fetchall()
    return [
        {
            "kind": "subtitle",
            "seq": row["seq"],
            "revision": row["revision"],
            "text": row["text"],
            "start_seq": None,
            "end_seq": None,
        }
        for row in rows
    ]


def get_finalized(
    channel: str, after: int = 0, limit: int = 1000, view: str = "original"
) -> dict:
    """查询频道定稿结果与当前水位线，按序混合返回字幕与缺口标记。

    after 用于增量拉取（按序号过滤，字幕与缺口标记统一计数）。
    view=original（默认）只返回不可变的原始定稿；view=effective 合并
    每个序号的最新更正文本并附带 correction_version，原始表不被读取改写。
    """
    with read_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        if view == "original":
            return {"channel": channel, "watermark": watermark,
                    "items": _fetch_finalized_items(conn, channel, after, limit)}

        # effective：左连接每个序号的最新更正版本，未更正者 version 为 0
        rows = conn.execute(
            """
            SELECT base.seq AS seq, base.kind AS kind, base.revision AS revision,
                   base.text AS original_text, base.start_seq AS start_seq,
                   base.end_seq AS end_seq,
                   COALESCE(c.version, 0) AS correction_version,
                   COALESCE(c.text, base.text) AS effective_text
            FROM (
                SELECT f.seq AS seq, 'subtitle' AS kind, f.revision AS revision,
                       f.text AS text, NULL AS start_seq, NULL AS end_seq, 0 AS ord
                FROM finalized f
                WHERE f.channel = ? AND f.seq > ?
                UNION ALL
                SELECT g.seq AS seq, 'gap' AS kind, NULL AS revision, NULL AS text,
                       g.start_seq AS start_seq, g.end_seq AS end_seq, 1 AS ord
                FROM gaps g
                WHERE g.channel = ? AND g.seq > ?
            ) base
            LEFT JOIN corrections c
                   ON c.channel = ? AND c.seq = base.seq
                  AND c.version = (
                      SELECT MAX(c2.version) FROM corrections c2
                      WHERE c2.channel = ? AND c2.seq = base.seq
                  )
            ORDER BY base.seq, base.ord
            LIMIT ?
            """,
            (channel, after, channel, after, channel, channel, limit),
        ).fetchall()

        items = []
        page_max_version = 0
        for row in rows:
            if row["kind"] == "gap":
                items.append(
                    {
                        "kind": "gap",
                        "seq": row["seq"],
                        "revision": None,
                        "text": None,
                        "start_seq": row["start_seq"],
                        "end_seq": row["end_seq"],
                        "correction_version": None,
                    }
                )
            else:
                cv = row["correction_version"]
                page_max_version = max(page_max_version, cv)
                items.append(
                    {
                        "kind": "subtitle",
                        "seq": row["seq"],
                        "revision": row["revision"],
                        "text": row["effective_text"],
                        "start_seq": None,
                        "end_seq": None,
                        "correction_version": cv,
                    }
                )
        return {
            "channel": channel,
            "watermark": watermark,
            "view": "effective",
            "correction_version": page_max_version,
            "items": items,
        }


def _fetch_finalized_items(conn, channel: str, after: int, limit: int) -> list:
    """读取按序混合的字幕与缺口标记；effective 仅在 original 视图之外复用。"""
    rows = conn.execute(
        """
        SELECT f.seq AS seq, f.revision AS revision, f.text AS text,
               NULL AS start_seq, NULL AS end_seq, 0 AS ord
        FROM finalized f
        WHERE f.channel = ? AND f.seq > ?
        UNION ALL
        SELECT g.seq AS seq, NULL AS revision, NULL AS text,
               g.start_seq AS start_seq, g.end_seq AS end_seq, 1 AS ord
        FROM gaps g
        WHERE g.channel = ? AND g.seq > ?
        ORDER BY seq, ord LIMIT ?
        """,
        (channel, after, channel, after, limit),
    ).fetchall()
    items = []
    for row in rows:
        if row["start_seq"] is None:
            items.append(
                {
                    "kind": "subtitle",
                    "seq": row["seq"],
                    "revision": row["revision"],
                    "text": row["text"],
                    "start_seq": None,
                    "end_seq": None,
                }
            )
        else:
            items.append(
                {
                    "kind": "gap",
                    "seq": row["seq"],
                    "revision": None,
                    "text": None,
                    "start_seq": row["start_seq"],
                    "end_seq": row["end_seq"],
                }
            )
    return items


def submit_correction(
    channel: str, seq: int, expected_version: int, text: str, reason: str
) -> dict:
    """对已定稿字幕原子追加一条更正。

    仅当 (channel, seq) 存在于 finalized（不是缺口、不是未定稿序号），
    且 expected_version 等于当前更正版本（无更正为 0）时才写入；
    任何前置条件不满足都先于写入返回，corrections 表与 finalized 表
    均保持不变。
    """
    with write_tx() as conn:
        finalized = conn.execute(
            "SELECT 1 FROM finalized WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if finalized is None:
            gap = conn.execute(
                "SELECT 1 FROM gaps WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if gap is not None:
                raise CorrectionError(
                    "seq_is_gap",
                    f"序号 {seq} 已裁决为永久缺口，不能更正",
                )
            raise CorrectionError(
                "not_finalized",
                f"序号 {seq} 尚未定稿，只有已定稿字幕允许更正",
            )

        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM corrections "
            "WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()["v"]
        if expected_version != current:
            raise CorrectionError(
                "version_conflict",
                f"expected_version={expected_version} 与当前更正版本 {current} 不一致",
            )

        next_version = current + 1
        try:
            row = conn.execute(
                "INSERT INTO corrections (channel, seq, version, text, reason) "
                "VALUES (?, ?, ?, ?, ?) "
                "RETURNING created_at",
                (channel, seq, next_version, text, reason),
            ).fetchone()
        except sqlite3.IntegrityError as exc:  # 极端竞争下的最后防线
            raise CorrectionError(
                "version_conflict",
                f"更正版本 {next_version} 已被并发请求占用",
            ) from exc

        return {
            "channel": channel,
            "seq": seq,
            "correction_version": next_version,
            "text": text,
            "reason": reason,
            "created_at": row["created_at"],
        }


def list_corrections(channel: str, seq: int) -> dict:
    """返回某定稿序号的更正审计记录（按 version 升序）。

    缺口或未定稿序号返回 404——这类序号不存在可审计的更正链。
    """
    with read_tx() as conn:
        finalized = conn.execute(
            "SELECT 1 FROM finalized WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if finalized is None:
            raise CorrectionError(
                "not_finalized",
                f"序号 {seq} 不存在已定稿字幕，没有更正审计记录",
                status_code=404,
            )

        rows = conn.execute(
            "SELECT version, text, reason, created_at FROM corrections "
            "WHERE channel = ? AND seq = ? ORDER BY version",
            (channel, seq),
        ).fetchall()
        return {
            "channel": channel,
            "seq": seq,
            "current_version": rows[-1]["version"] if rows else 0,
            "items": [
                {
                    "channel": channel,
                    "seq": seq,
                    "version": row["version"],
                    "text": row["text"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }


# ---------- 发布快照 ----------


def create_snapshot(channel: str, idempotency_key: str) -> dict:
    """为频道创建发布快照：单事务锁定当前 watermark 并冻结 1..watermark。

    同一 (channel, idempotency_key) 重试不重新冻结，返回既有快照
    （idempotent=true）。不存在的频道生成 watermark=0 的空快照，且
    不会在 channels 表中创建频道行。
    """
    with write_tx() as conn:
        existing = conn.execute(
            "SELECT snapshot_id, watermark, created_at FROM snapshots "
            "WHERE channel = ? AND idempotency_key = ?",
            (channel, idempotency_key),
        ).fetchone()
        if existing is not None:
            return _snapshot_meta(
                existing, channel, idempotency_key, idempotent=True
            )

        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        snapshot_id = str(uuid.uuid4())
        try:
            conn.execute(
                "INSERT INTO snapshots "
                "(snapshot_id, channel, watermark, idempotency_key) "
                "VALUES (?, ?, ?, ?)",
                (snapshot_id, channel, watermark, idempotency_key),
            )
        except sqlite3.IntegrityError:
            # 极端并发下同一幂等键竞争：唯一约束是最后防线，转幂等返回
            existing = conn.execute(
                "SELECT snapshot_id, watermark, created_at FROM snapshots "
                "WHERE channel = ? AND idempotency_key = ?",
                (channel, idempotency_key),
            ).fetchone()
            return _snapshot_meta(
                existing, channel, idempotency_key, idempotent=True
            )

        if watermark > 0:
            # 字幕行：同时固化原始文本与创建时刻的生效文本（最新更正）
            conn.execute(
                """
                INSERT INTO snapshot_items (
                    snapshot_id, seq, kind, revision, original_text,
                    effective_text, correction_version, start_seq, end_seq
                )
                SELECT ?, f.seq, 'subtitle', f.revision, f.text,
                       COALESCE(c.text, f.text), COALESCE(c.version, 0),
                       NULL, NULL
                FROM finalized f
                LEFT JOIN corrections c
                       ON c.channel = f.channel AND c.seq = f.seq
                      AND c.version = (
                          SELECT MAX(c2.version) FROM corrections c2
                          WHERE c2.channel = f.channel AND c2.seq = f.seq
                      )
                WHERE f.channel = ? AND f.seq BETWEEN 1 AND ?
                """,
                (snapshot_id, channel, watermark),
            )
            # 缺口行：固化所属裁决区间端点
            conn.execute(
                """
                INSERT INTO snapshot_items (
                    snapshot_id, seq, kind, revision, original_text,
                    effective_text, correction_version, start_seq, end_seq
                )
                SELECT ?, seq, 'gap', NULL, NULL, NULL, NULL, start_seq, end_seq
                FROM gaps
                WHERE channel = ? AND seq BETWEEN 1 AND ?
                """,
                (snapshot_id, channel, watermark),
            )
            # 各条更正版本完整冻结（按版本升序，供快照内审计）
            conn.execute(
                """
                INSERT INTO snapshot_corrections
                    (snapshot_id, seq, version, text, reason, created_at)
                SELECT ?, seq, version, text, reason, created_at
                FROM corrections
                WHERE channel = ? AND seq BETWEEN 1 AND ?
                """,
                (snapshot_id, channel, watermark),
            )

        created_at = conn.execute(
            "SELECT created_at FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["created_at"]
        return {
            "snapshot_id": snapshot_id,
            "channel": channel,
            "watermark": watermark,
            "idempotency_key": idempotency_key,
            "idempotent": False,
            "created_at": created_at,
        }


def _snapshot_meta(row, channel: str, idempotency_key: str, idempotent: bool) -> dict:
    """从 snapshots 行构造创建/重试响应。"""
    return {
        "snapshot_id": row["snapshot_id"],
        "channel": channel,
        "watermark": row["watermark"],
        "idempotency_key": idempotency_key,
        "idempotent": idempotent,
        "created_at": row["created_at"],
    }


def _get_snapshot_meta(conn, snapshot_id: str):
    meta = conn.execute(
        "SELECT channel, watermark FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if meta is None:
        raise SnapshotError(
            "snapshot_not_found",
            f"快照 {snapshot_id} 不存在",
            status_code=404,
        )
    return meta


def get_snapshot(
    snapshot_id: str, after: int = 0, limit: int = 1000, view: str = "original"
) -> dict:
    """按 snapshot_id 分页拉取快照条目（after/limit 按序号）。

    返回的是快照时刻冻结的内容：快照创建后新定稿的字幕、新追加的更正
    或新裁决的缺口都不会影响任何既有快照。
    """
    with read_tx() as conn:
        meta = _get_snapshot_meta(conn, snapshot_id)
        channel, watermark = meta["channel"], meta["watermark"]

        if view == "original":
            rows = conn.execute(
                """
                SELECT seq, kind, revision, original_text, start_seq, end_seq
                FROM snapshot_items
                WHERE snapshot_id = ? AND seq > ?
                ORDER BY seq LIMIT ?
                """,
                (snapshot_id, after, limit),
            ).fetchall()
            return {
                "snapshot_id": snapshot_id,
                "channel": channel,
                "watermark": watermark,
                "view": "original",
                "items": [_snapshot_item_dict(row, effective=False) for row in rows],
            }

        rows = conn.execute(
            """
            SELECT seq, kind, revision, original_text, effective_text,
                   correction_version, start_seq, end_seq
            FROM snapshot_items
            WHERE snapshot_id = ? AND seq > ?
            ORDER BY seq LIMIT ?
            """,
            (snapshot_id, after, limit),
        ).fetchall()
        items = []
        page_max_version = 0
        for row in rows:
            item = _snapshot_item_dict(row, effective=True)
            if item["correction_version"] is not None:
                page_max_version = max(
                    page_max_version, item["correction_version"]
                )
            items.append(item)
        return {
            "snapshot_id": snapshot_id,
            "channel": channel,
            "watermark": watermark,
            "view": "effective",
            "correction_version": page_max_version,
            "items": items,
        }


def _snapshot_item_dict(row, effective: bool) -> dict:
    """将快照条目行转为响应 dict；effective=True 时返回冻结的生效文本。"""
    if row["kind"] == "gap":
        return {
            "kind": "gap",
            "seq": row["seq"],
            "revision": None,
            "text": None,
            "start_seq": row["start_seq"],
            "end_seq": row["end_seq"],
            **(
                {"correction_version": None}
                if effective
                else {}
            ),
        }
    item = {
        "kind": "subtitle",
        "seq": row["seq"],
        "revision": row["revision"],
        "text": row["effective_text"] if effective else row["original_text"],
        "start_seq": None,
        "end_seq": None,
    }
    if effective:
        item["correction_version"] = row["correction_version"]
    return item


def list_snapshot_corrections(snapshot_id: str, seq: int) -> dict:
    """返回快照内某字幕序号冻结的更正历史（按 version 升序）。

    快照不存在 -> 404 snapshot_not_found；序号超出快照锁定水位线 ->
    404 seq_out_of_snapshot；序号在快照中是缺口 -> 409 seq_is_gap。
    """
    with read_tx() as conn:
        meta = _get_snapshot_meta(conn, snapshot_id)
        channel, watermark = meta["channel"], meta["watermark"]

        if seq < 1 or seq > watermark:
            raise SnapshotError(
                "seq_out_of_snapshot",
                f"序号 {seq} 超出快照 {snapshot_id} 的水位线 {watermark}",
                status_code=404,
            )

        item = conn.execute(
            "SELECT kind FROM snapshot_items WHERE snapshot_id = ? AND seq = ?",
            (snapshot_id, seq),
        ).fetchone()
        if item["kind"] == "gap":
            raise SnapshotError(
                "seq_is_gap",
                f"序号 {seq} 在快照 {snapshot_id} 中是永久缺口，没有更正历史",
                status_code=409,
            )

        rows = conn.execute(
            "SELECT version, text, reason, created_at FROM snapshot_corrections "
            "WHERE snapshot_id = ? AND seq = ? ORDER BY version",
            (snapshot_id, seq),
        ).fetchall()
        return {
            "snapshot_id": snapshot_id,
            "channel": channel,
            "seq": seq,
            "current_version": rows[-1]["version"] if rows else 0,
            "items": [
                {
                    "seq": seq,
                    "version": row["version"],
                    "text": row["text"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }


# ---------- 命名消费者拉取游标与租约 ----------


def lease_ttl_seconds() -> float:
    """租约有效期（秒），由环境变量 LEASE_TTL_SECONDS 配置，默认 30 秒。"""
    return float(os.environ.get("LEASE_TTL_SECONDS", "30"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc_str(dt: datetime) -> str:
    """统一 UTC ISO8601（毫秒，Z 结尾），与 SQLite 建表默认时间格式一致。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _lease_status(row, now: datetime) -> str:
    """租约状态：confirmed=已完整确认；expired=过期未确认；active=未过期未完整确认（含部分确认）。

    到期时刻在签发时按当时的 TTL 固化（expires_at 列），之后即使调整
    LEASE_TTL_SECONDS 也不会让在途租约提前/延后到期。
    """
    if row["confirmed_at"] is not None:
        return "confirmed"
    if _parse_utc(row["expires_at"]) <= now:
        return "expired"
    return "active"


def _latest_lease(conn, channel: str, consumer: str):
    """读取某消费者最近一次领取的租约（按自增 rowid，无则 None）。"""
    return conn.execute(
        "SELECT * FROM consumer_leases WHERE channel = ? AND consumer = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (channel, consumer),
    ).fetchone()


def claim_batch(channel: str, consumer: str, limit: int = 100) -> dict:
    """消费者从已确认游标后领取一个连续批次（含缺口标记）并签发租约。

    - “读游标 -> 检查最近租约 -> 取批次 -> 写租约”在单个 BEGIN IMMEDIATE
      事务内完成，并发领取被串行化；
    - 最近租约仍有效（未过期且未完整确认，允许已有部分确认进度）时拒绝
      重复领取（409 lease_active），不产生任何写入；过期租约不修改、不
      删除，只作为旧令牌失效的凭据；
    - 游标之后暂无可领取序号时返回 items=[] 且不签发租约。
    """
    now = _now_utc()
    with write_tx() as conn:
        row = conn.execute(
            "SELECT cursor FROM consumers WHERE channel = ? AND consumer = ?",
            (channel, consumer),
        ).fetchone()
        cursor = row["cursor"] if row else 0

        latest = _latest_lease(conn, channel, consumer)
        if latest is not None and _lease_status(latest, now) == "active":
            raise LeaseError(
                "lease_active",
                f"消费者 {consumer} 在频道 {channel} 上仍持有未过期且未完整"
                f"确认的租约 (token 尾号 ...{latest['lease_token'][-8:]})，"
                "请先确认到批次末位或等待租约到期",
            )

        watermark = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = watermark["watermark"] if watermark else 0

        items = _fetch_finalized_items(conn, channel, after=cursor, limit=limit)

        # 首次领取时惰性注册消费者；游标仍为 0 也保留注册记录供状态查询
        conn.execute(
            "INSERT INTO consumers (channel, consumer, cursor) VALUES (?, ?, 0) "
            "ON CONFLICT(channel, consumer) DO NOTHING",
            (channel, consumer),
        )

        if not items:
            return {
                "channel": channel,
                "consumer": consumer,
                "cursor": cursor,
                "watermark": watermark,
                "lease_token": None,
                "expires_at": None,
                "start_seq": None,
                "end_seq": None,
                "limit": limit,
                "items": [],
            }

        start_seq = cursor + 1
        end_seq = items[-1]["seq"]
        issued_at = now
        expires_at = now + timedelta(seconds=lease_ttl_seconds())
        issued_at_str = _utc_str(issued_at)
        expires_at_str = _utc_str(expires_at)
        lease_token = secrets.token_urlsafe(32)

        conn.execute(
            "INSERT INTO consumer_leases "
            "(channel, consumer, lease_token, start_seq, end_seq, expires_at, "
            " confirmed_cursor, confirmed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                channel, consumer, lease_token, start_seq, end_seq,
                expires_at_str, issued_at_str,
            ),
        )

        return {
            "channel": channel,
            "consumer": consumer,
            "cursor": cursor,
            "watermark": watermark,
            "lease_token": lease_token,
            "expires_at": expires_at_str,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "limit": limit,
            "items": items,
        }


def confirm_batch(
    channel: str, consumer: str, lease_token: str, expected_cursor: int
) -> dict:
    """确认消费者已处理下一个序号，把已确认游标推进到 expected_cursor+1。

    一个租约覆盖一整个批次，可在有效期内逐条确认：每次调用游标 +1，
    直到推进到 end_seq 时租约完整消费（回填 confirmed_at）。所有前置
    校验先于任何写入执行：
    - lease_token 必须是该消费者最近一次领取的令牌（未知/旧令牌 409）；
    - 租约已完整确认 -> 409 lease_consumed；已过期 -> 409 lease_expired；
    - expected_cursor 必须等于服务端当前已确认游标（落后/超前的并发
      请求 409 cursor_conflict）；
    - 推进目标 expected_cursor+1 必须 <= end_seq（越界 409
      cursor_out_of_batch）。
    """
    now = _now_utc()
    with write_tx() as conn:
        crow = conn.execute(
            "SELECT cursor FROM consumers WHERE channel = ? AND consumer = ?",
            (channel, consumer),
        ).fetchone()
        if crow is None:
            raise LeaseError(
                "consumer_not_found",
                f"消费者 {consumer} 在频道 {channel} 上不存在，请先领取批次",
                status_code=404,
            )
        current_cursor = crow["cursor"]

        lease = conn.execute(
            "SELECT * FROM consumer_leases "
            "WHERE channel = ? AND consumer = ? AND lease_token = ?",
            (channel, consumer, lease_token),
        ).fetchone()
        if lease is None:
            raise LeaseError(
                "invalid_lease_token",
                "lease_token 无效或不属于该消费者",
            )

        # 只有最近一次租约允许确认：过期后重新领取会让旧令牌落到这里
        latest = _latest_lease(conn, channel, consumer)
        if latest["lease_token"] != lease_token:
            raise LeaseError(
                "lease_superseded",
                "该 lease_token 已被更新的领取取代（旧租约不可再确认）",
            )

        if _lease_status(lease, now) == "confirmed":
            raise LeaseError(
                "lease_consumed",
                "该租约已完整确认，不能重复确认",
            )

        if _lease_status(lease, now) == "expired":
            raise LeaseError(
                "lease_expired",
                "lease_token 已过期，请重新领取未确认批次",
            )

        if expected_cursor != current_cursor:
            raise LeaseError(
                "cursor_conflict",
                f"expected_cursor={expected_cursor} 与服务端已确认游标 "
                f"{current_cursor} 不一致",
            )

        new_cursor = expected_cursor + 1
        # 游标在领取时等于 start_seq-1 且只增不减，下界天然成立；
        # 只需拒绝推进到批次末位之后
        if new_cursor > lease["end_seq"]:
            raise LeaseError(
                "cursor_out_of_batch",
                f"推进目标 {new_cursor} 超出本批次末位 {lease['end_seq']}",
            )

        fully_confirmed = new_cursor == lease["end_seq"]
        confirmed_at_str = _utc_str(now) if fully_confirmed else None
        # 租约确认信息与消费者游标推进必须在同一事务内原子完成；
        # 部分确认只回填 confirmed_cursor 展示进度，confirmed_at 保持 NULL，
        # 租约仍可在有效期内继续确认剩余序号
        conn.execute(
            "UPDATE consumer_leases SET confirmed_cursor = ?, confirmed_at = ? "
            "WHERE channel = ? AND consumer = ? AND lease_token = ?",
            (new_cursor, confirmed_at_str, channel, consumer, lease_token),
        )
        conn.execute(
            "UPDATE consumers SET cursor = ? WHERE channel = ? AND consumer = ?",
            (new_cursor, channel, consumer),
        )

        watermark_row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = watermark_row["watermark"] if watermark_row else 0

        return {
            "channel": channel,
            "consumer": consumer,
            "cursor": new_cursor,
            "watermark": watermark,
            "lease_token": lease_token,
            "fully_confirmed": fully_confirmed,
            "confirmed_at": confirmed_at_str,
        }


def get_consumer_status(channel: str, consumer: str) -> dict:
    """查询消费者的已确认游标、频道水位线与最近租约状态。"""
    now = _now_utc()
    with read_tx() as conn:
        crow = conn.execute(
            "SELECT cursor, created_at FROM consumers "
            "WHERE channel = ? AND consumer = ?",
            (channel, consumer),
        ).fetchone()
        if crow is None:
            raise LeaseError(
                "consumer_not_found",
                f"消费者 {consumer} 在频道 {channel} 上不存在",
                status_code=404,
            )

        watermark_row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = watermark_row["watermark"] if watermark_row else 0
        cursor = crow["cursor"]

        lease = _latest_lease(conn, channel, consumer)
        lease_info = None
        if lease is not None:
            lease_info = {
                "lease_token": lease["lease_token"],
                "status": _lease_status(lease, now),
                "start_seq": lease["start_seq"],
                "end_seq": lease["end_seq"],
                "expires_at": lease["expires_at"],
                "confirmed_cursor": lease["confirmed_cursor"],
                "confirmed_at": lease["confirmed_at"],
                "created_at": lease["created_at"],
            }

        return {
            "channel": channel,
            "consumer": consumer,
            "cursor": cursor,
            "watermark": watermark,
            "lag": max(watermark - cursor, 0),
            "created_at": crow["created_at"],
            "latest_lease": lease_info,
        }
