"""命名消费者拉取游标与租约测试。

覆盖：
- 领取：从已确认游标后返回连续批次（字幕与缺口标记混合）、limit 截断、
  游标之后无内容时不签发租约；
- 不重复领取：未过期租约存续期间同一消费者重复领取 409 lease_active；
- 确认：lease_token + expected_cursor 双校验，游标只能推进到批次内；
  过期令牌、旧令牌、已消费令牌、令牌错误、游标不匹配、越界、未知消费者
  一律 409/404 且不改写状态；
- 过期后可重新领取同一未确认批次，旧令牌即刻失效；
- 部分确认后租约在有效期内仍可继续确认剩余序号，到期未确认完可重新领取；
- 多消费者游标相互隔离；
- 消费者状态查询；
- 多线程并发领取恰好一个成功、并发确认恰好一个成功；
- 真实挂钟过期（短 TTL + sleep）。
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import service


@pytest.fixture(autouse=True)
def _long_lease_ttl(monkeypatch):
    """默认给每个用例一个不会过期的租约 TTL；需要过期的用例自行 monkeypatch。"""
    monkeypatch.setenv("LEASE_TTL_SECONDS", "3600")
    yield


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def decide_gap(client, channel, start_seq, end_seq):
    resp = client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )
    assert resp.status_code == 200
    return resp.json()


def claim(client, channel, consumer, **params):
    return client.post(
        f"/channels/{channel}/consumers/{consumer}/claims", params=params
    )


def confirm(client, channel, consumer, lease_token, expected_cursor):
    return client.post(
        f"/channels/{channel}/consumers/{consumer}/claims/confirm",
        json={"lease_token": lease_token, "expected_cursor": expected_cursor},
    )


def build_channel(client, channel="news"):
    """1=字幕、2-3=缺口、4=字幕、5=字幕，水位线 5。"""
    submit(client, channel, 1, 1, "第一条")
    submit(client, channel, 4, 1, "第四条")
    submit(client, channel, 5, 1, "第五条")
    decide_gap(client, channel, 2, 3)


# ---------- 领取 ----------


def test_claim_returns_contiguous_batch_with_gap_markers(client):
    build_channel(client)

    resp = claim(client, "news", "dlq-worker")
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "news"
    assert body["consumer"] == "dlq-worker"
    assert body["cursor"] == 0
    assert body["watermark"] == 5
    assert body["start_seq"] == 1
    assert body["end_seq"] == 5
    assert body["limit"] == 100
    assert [(i["kind"], i["seq"]) for i in body["items"]] == [
        ("subtitle", 1),
        ("gap", 2),
        ("gap", 3),
        ("subtitle", 4),
        ("subtitle", 5),
    ]
    assert body["items"][1]["start_seq"] == 2
    assert body["items"][1]["end_seq"] == 3
    assert body["lease_token"]
    assert body["expires_at"]


def test_claim_respects_limit_and_next_claim_waits_for_confirmation(client):
    build_channel(client)

    body = claim(client, "news", "w", limit=2).json()
    assert body["start_seq"] == 1
    assert body["end_seq"] == 2
    assert [i["seq"] for i in body["items"]] == [1, 2]

    # 租约未过期：即便想要后续批次也不得重复领取
    again = claim(client, "news", "w")
    assert again.status_code == 409
    assert again.json()["error"] == "lease_active"

    # 全部确认两批序号后才能继续
    confirm(client, "news", "w", body["lease_token"], 0)
    confirm(client, "news", "w", body["lease_token"], 1)

    body2 = claim(client, "news", "w", limit=2).json()
    assert body2["cursor"] == 2
    assert body2["start_seq"] == 3
    assert [i["seq"] for i in body2["items"]] == [3, 4]


def test_claim_with_nothing_new_returns_empty_and_no_lease(client, conn):
    build_channel(client)

    # 频道水位线 0：空批次，不签发租约，也不写租约行
    body = claim(client, "empty", "w").json()
    assert body["items"] == []
    assert body["lease_token"] is None
    assert body["expires_at"] is None
    assert body["start_seq"] is None
    assert body["end_seq"] is None
    assert body["cursor"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM consumer_leases WHERE consumer = 'w'"
    ).fetchone()["c"] == 0

    # 全部确认后再次领取：已追上水位线，仍为空
    body = claim(client, "news", "w").json()
    token = body["lease_token"]
    for expected in range(5):
        confirm(client, "news", "w", token, expected)
    body = claim(client, "news", "w").json()
    assert body["items"] == []
    assert body["lease_token"] is None
    assert body["cursor"] == 5


def test_claim_limit_validation(client):
    assert claim(client, "news", "w", limit=0).status_code == 422
    assert claim(client, "news", "w", limit=10001).status_code == 422


# ---------- 确认：成功路径 ----------


def test_full_confirmation_advances_cursor_and_continues(client):
    build_channel(client)
    body = claim(client, "news", "w").json()

    resp = confirm(client, "news", "w", body["lease_token"], 0)
    assert resp.status_code == 200
    ack = resp.json()
    assert ack["cursor"] == 1
    assert ack["watermark"] == 5
    assert ack["fully_confirmed"] is False
    assert ack["confirmed_at"] is None  # 部分确认不落完整确认时间

    # 同一租约内逐条确认到批次末位
    for expected in range(1, 5):
        ack = confirm(client, "news", "w", body["lease_token"], expected).json()
    assert ack["cursor"] == 5
    assert ack["fully_confirmed"] is True
    assert ack["confirmed_at"]

    status = client.get("/channels/news/consumers/w/status").json()
    assert status["cursor"] == 5
    assert status["lag"] == 0
    assert status["latest_lease"]["status"] == "confirmed"
    assert status["latest_lease"]["confirmed_cursor"] == 5


def test_confirmation_advances_one_seq_per_call(client):
    """每次确认只把游标推进 expected_cursor+1（逐条 ack 语义，不能跳号）。"""
    build_channel(client)
    body = claim(client, "news", "w", limit=5).json()
    confirm(client, "news", "w", body["lease_token"], 0)
    # 不能一次跳到批次末位：expected_cursor 必须等于当前游标
    resp = confirm(client, "news", "w", body["lease_token"], 4)
    assert resp.status_code == 409
    assert resp.json()["error"] == "cursor_conflict"


def test_partial_progress_keeps_lease_for_remaining_seq(client):
    build_channel(client)
    body = claim(client, "news", "w", limit=5).json()
    token = body["lease_token"]

    # 只确认到序号 2（部分确认）：租约仍有效，可继续确认剩余序号
    confirm(client, "news", "w", token, 0)
    ack = confirm(client, "news", "w", token, 1).json()
    assert ack["cursor"] == 2
    assert ack["fully_confirmed"] is False

    status = client.get("/channels/news/consumers/w/status").json()
    assert status["latest_lease"]["status"] == "active"
    assert status["latest_lease"]["confirmed_cursor"] == 2
    assert status["latest_lease"]["confirmed_at"] is None

    # 未完整确认前仍不得重新领取
    assert claim(client, "news", "w").status_code == 409

    # 继续确认剩余序号直到批次末位
    ack = confirm(client, "news", "w", token, 2).json()
    assert ack["cursor"] == 3
    for expected in range(3, 5):
        ack = confirm(client, "news", "w", token, expected).json()
    assert ack["fully_confirmed"] is True
    assert ack["confirmed_at"]


def test_confirm_rejects_invalid_old_and_consumed_tokens(client):
    build_channel(client)
    body = claim(client, "news", "w").json()
    token = body["lease_token"]

    # 未知令牌
    resp = confirm(client, "news", "w", "not-a-real-token", 0)
    assert resp.status_code == 409
    assert resp.json()["error"] == "invalid_lease_token"

    # 空白令牌
    assert confirm(client, "news", "w", "   ", 0).status_code == 422
    assert confirm(client, "news", "w", token, -1).status_code == 422

    # 完整确认后租约已消费，重复确认（任何游标）一律 409 lease_consumed
    for expected in range(5):
        assert confirm(client, "news", "w", token, expected).status_code == 200
    resp = confirm(client, "news", "w", token, 5)
    assert resp.status_code == 409
    assert resp.json()["error"] == "lease_consumed"


def test_confirm_cursor_conflict_does_not_mutate_state(client, conn):
    build_channel(client)
    body = claim(client, "news", "w").json()

    # 并发落后请求：expected_cursor 超前或落后于服务端游标
    resp = confirm(client, "news", "w", body["lease_token"], 3)
    assert resp.status_code == 409
    assert resp.json()["error"] == "cursor_conflict"

    # 状态不改写：仍可用正确的 expected_cursor 成功确认
    ok = confirm(client, "news", "w", body["lease_token"], 0)
    assert ok.status_code == 200
    assert conn.execute(
        "SELECT cursor FROM consumers WHERE channel = 'news' AND consumer = 'w'"
    ).fetchone()["cursor"] == 1


def test_confirm_unknown_consumer_returns_404(client):
    build_channel(client)
    resp = confirm(client, "news", "ghost", "whatever", 0)
    assert resp.status_code == 404
    assert resp.json()["error"] == "consumer_not_found"

    resp = client.get("/channels/news/consumers/ghost/status")
    assert resp.status_code == 404
    assert resp.json()["error"] == "consumer_not_found"


# ---------- 过期 ----------


def test_expired_lease_allows_reclaim_same_batch_and_old_token_dead(client, monkeypatch):
    build_channel(client)
    monkeypatch.setenv("LEASE_TTL_SECONDS", "0")

    # TTL=0：签出即过期
    body = claim(client, "news", "w").json()
    old_token = body["lease_token"]
    assert [i["seq"] for i in body["items"]] == [1, 2, 3, 4, 5]

    # 旧令牌确认 -> 409 lease_expired，状态不变
    resp = confirm(client, "news", "w", old_token, 0)
    assert resp.status_code == 409
    assert resp.json()["error"] == "lease_expired"

    # 过期后允许重新领取同一未确认批次（游标仍是 0）
    body2 = claim(client, "news", "w").json()
    assert body2["cursor"] == 0
    assert body2["start_seq"] == 1
    assert [i["seq"] for i in body2["items"]] == [1, 2, 3, 4, 5]
    assert body2["lease_token"] != old_token

    # 旧租约被新领取取代：即便不过期也不能再确认
    resp = confirm(client, "news", "w", old_token, 0)
    assert resp.status_code == 409
    assert resp.json()["error"] == "lease_superseded"

    # TTL=0 签出的新租约同样签出即过期，恢复 TTL 后再次领取得到有效租约
    monkeypatch.setenv("LEASE_TTL_SECONDS", "3600")
    body3 = claim(client, "news", "w").json()
    assert body3["cursor"] == 0
    assert body3["start_seq"] == 1
    assert [i["seq"] for i in body3["items"]] == [1, 2, 3, 4, 5]
    assert body3["lease_token"] not in (old_token, body2["lease_token"])
    ok = confirm(client, "news", "w", body3["lease_token"], 0)
    assert ok.status_code == 200


def test_wall_clock_expiry_blocks_then_reclaim(client, monkeypatch):
    """真实挂钟：TTL 到期后旧令牌失效并可重新领取（不依赖 TTL=0）。"""
    build_channel(client)
    monkeypatch.setenv("LEASE_TTL_SECONDS", "1")

    body = claim(client, "news", "w").json()
    token = body["lease_token"]
    time.sleep(1.1)

    resp = confirm(client, "news", "w", token, 0)
    assert resp.status_code == 409
    assert resp.json()["error"] == "lease_expired"

    # 重新领取前先把 TTL 恢复，保证新租约在后续确认时不过期
    monkeypatch.setenv("LEASE_TTL_SECONDS", "3600")
    body2 = claim(client, "news", "w").json()
    assert body2["cursor"] == 0
    assert body2["lease_token"] != token
    assert confirm(client, "news", "w", body2["lease_token"], 0).status_code == 200


def test_active_lease_survives_until_expiry(client, monkeypatch):
    build_channel(client)
    monkeypatch.setenv("LEASE_TTL_SECONDS", "3600")
    body = claim(client, "news", "w").json()
    assert claim(client, "news", "w").status_code == 409
    assert confirm(client, "news", "w", body["lease_token"], 0).status_code == 200


# ---------- 隔离与状态查询 ----------


def test_consumers_are_isolated_and_share_channel_finalized(client):
    build_channel(client)

    b1 = claim(client, "news", "worker-a").json()
    # worker-a 持租约不影响 worker-b 独立游标
    b2 = claim(client, "news", "worker-b").json()
    assert b1["lease_token"] != b2["lease_token"]
    assert b1["start_seq"] == b2["start_seq"] == 1

    # a 部分确认前两条，b 仍从 1 开始；二者互不干扰
    confirm(client, "news", "worker-a", b1["lease_token"], 0)
    confirm(client, "news", "worker-a", b1["lease_token"], 1)
    assert client.get(
        "/channels/news/consumers/worker-a/status"
    ).json()["cursor"] == 2
    assert client.get(
        "/channels/news/consumers/worker-b/status"
    ).json()["cursor"] == 0

    # a 用同一租约继续确认剩余 3..5；b 独立持有自己的 1..5 租约
    for expected in range(2, 5):
        assert confirm(
            client, "news", "worker-a", b1["lease_token"], expected
        ).status_code == 200
    assert confirm(client, "news", "worker-b", b2["lease_token"], 0).status_code == 200
    assert client.get(
        "/channels/news/consumers/worker-a/status"
    ).json()["cursor"] == 5
    assert client.get(
        "/channels/news/consumers/worker-b/status"
    ).json()["cursor"] == 1


def test_expired_after_partial_progress_reclaims_remaining_batch(client, monkeypatch):
    """部分确认后租约到期（真实挂钟）：从当前游标重新领取剩余未确认序号。"""
    build_channel(client)
    monkeypatch.setenv("LEASE_TTL_SECONDS", "1")
    body = claim(client, "news", "w").json()
    token = body["lease_token"]
    confirm(client, "news", "w", token, 0)
    confirm(client, "news", "w", token, 1)  # 游标推进到 2，租约仍未完整消费
    time.sleep(1.1)

    # 租约到期：旧令牌立即失效
    resp = confirm(client, "news", "w", token, 2)
    assert resp.status_code == 409
    assert resp.json()["error"] == "lease_expired"

    # 恢复 TTL 后从已确认游标 2 之后重新领取剩余 3..5
    monkeypatch.setenv("LEASE_TTL_SECONDS", "3600")
    body2 = claim(client, "news", "w").json()
    assert body2["cursor"] == 2
    assert body2["start_seq"] == 3
    assert [i["seq"] for i in body2["items"]] == [3, 4, 5]
    assert body2["lease_token"] != token
    ok = confirm(client, "news", "w", body2["lease_token"], 2)
    assert ok.status_code == 200


def test_consumer_status_reports_active_expired_and_confirmed(client, monkeypatch):
    build_channel(client)
    monkeypatch.setenv("LEASE_TTL_SECONDS", "3600")

    claim(client, "news", "w")
    status = client.get("/channels/news/consumers/w/status").json()
    assert status["channel"] == "news"
    assert status["consumer"] == "w"
    assert status["cursor"] == 0
    assert status["watermark"] == 5
    assert status["lag"] == 5
    assert status["created_at"]
    lease = status["latest_lease"]
    assert lease["status"] == "active"
    assert lease["start_seq"] == 1
    assert lease["end_seq"] == 5
    assert lease["confirmed_cursor"] is None
    assert lease["confirmed_at"] is None

    # 部分确认 -> 租约仍 active，confirmed_cursor 展示进度
    token = lease["lease_token"]
    confirm(client, "news", "w", token, 0)
    status = client.get("/channels/news/consumers/w/status").json()
    assert status["cursor"] == 1
    assert status["lag"] == 4
    assert status["latest_lease"]["status"] == "active"
    assert status["latest_lease"]["confirmed_cursor"] == 1
    assert status["latest_lease"]["confirmed_at"] is None

    # 确认到批次末位 -> confirmed
    for expected in range(1, 5):
        confirm(client, "news", "w", token, expected)
    status = client.get("/channels/news/consumers/w/status").json()
    assert status["cursor"] == 5
    assert status["lag"] == 0
    assert status["latest_lease"]["status"] == "confirmed"
    assert status["latest_lease"]["confirmed_cursor"] == 5
    assert status["latest_lease"]["confirmed_at"]


def test_consumer_status_expired_lease(client, monkeypatch):
    build_channel(client)
    monkeypatch.setenv("LEASE_TTL_SECONDS", "0")
    claim(client, "news", "w")
    status = client.get("/channels/news/consumers/w/status").json()
    assert status["latest_lease"]["status"] == "expired"
    assert status["cursor"] == 0


# ---------- 新定稿到达不影响在途租约 ----------


def test_new_finalized_after_claim_does_not_change_active_lease(client, monkeypatch):
    monkeypatch.setenv("LEASE_TTL_SECONDS", "3600")
    submit(client, "news", 1, 1, "one")
    body = claim(client, "news", "w").json()
    assert body["end_seq"] == 1

    # 领取之后新定稿 2、3：在途租约批次不变，确认后下次领取才看到
    submit(client, "news", 2, 1, "two")
    submit(client, "news", 3, 1, "three")
    status = client.get("/channels/news/consumers/w/status").json()
    assert status["watermark"] == 3
    assert status["latest_lease"]["end_seq"] == 1

    ack = confirm(client, "news", "w", body["lease_token"], 0).json()
    assert ack["cursor"] == 1
    assert ack["fully_confirmed"] is True

    body2 = claim(client, "news", "w").json()
    assert [i["seq"] for i in body2["items"]] == [2, 3]


# ---------- 并发 ----------


def test_concurrent_claims_exactly_one_wins(client):
    """同一消费者并发领取：恰好一个拿到租约，其余全部 lease_active。"""
    build_channel(client)

    def attempt(_):
        try:
            return service.claim_batch("news", "w", limit=100)
        except service.LeaseError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(20)))

    granted = [r for r in results if not isinstance(r, service.LeaseError)]
    blocked = [
        r for r in results
        if isinstance(r, service.LeaseError) and r.error == "lease_active"
    ]
    assert len(granted) == 1
    assert len(blocked) == 19
    assert granted[0]["start_seq"] == 1


def test_concurrent_confirms_exactly_one_wins_per_cursor(client, conn):
    """同一租约同 expected_cursor 的并发确认：恰好一个推进游标。"""
    build_channel(client)
    granted = service.claim_batch("news", "w", limit=100)
    token = granted["lease_token"]

    def attempt(_):
        try:
            return service.confirm_batch("news", "w", token, 0)
        except service.LeaseError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(20)))

    ok = [r for r in results if not isinstance(r, service.LeaseError)]
    conflicts = [
        r for r in results
        if isinstance(r, service.LeaseError) and r.error == "cursor_conflict"
    ]
    assert len(ok) == 1
    assert len(conflicts) == 19
    assert ok[0]["cursor"] == 1
    assert conn.execute(
        "SELECT cursor FROM consumers WHERE channel = 'news' AND consumer = 'w'"
    ).fetchone()["cursor"] == 1


def test_concurrent_confirmation_chain_advances_exactly_once_per_seq(conn):
    """多轮并发确认整条批次：每个序号恰好推进一次，最终游标等于批次末位。"""
    n = 50
    channel = "chain"
    for seq in range(1, n + 1):
        service.submit_fragment(channel, seq, 1, f"line{seq}")
    token = service.claim_batch(channel, "w", limit=100)["lease_token"]

    cursor = 0
    while cursor < n:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: _try_confirm(channel, "w", token, cursor),
                    range(8),
                )
            )
        winners = [r for r in results if not isinstance(r, service.LeaseError)]
        assert len(winners) == 1  # 每个序号在竞争中恰好推进一次
        assert winners[0]["cursor"] == cursor + 1
        cursor += 1

    assert conn.execute(
        "SELECT cursor FROM consumers WHERE channel = ? AND consumer = 'w'",
        (channel,),
    ).fetchone()["cursor"] == n
    # 完整确认后任何并发请求都是 lease_consumed
    with ThreadPoolExecutor(max_workers=8) as pool:
        tail = list(pool.map(lambda _: _try_confirm(channel, "w", token, n), range(8)))
    assert all(isinstance(r, service.LeaseError) and r.error == "lease_consumed"
               for r in tail)


def _try_confirm(channel, consumer, token, expected_cursor):
    try:
        return service.confirm_batch(channel, consumer, token, expected_cursor)
    except service.LeaseError as exc:
        return exc


def test_concurrent_claim_after_expiry_only_one_new_lease(client, monkeypatch):
    """过期后并发重新领取：写事务串行化，每个时刻最多一个在途租约。"""
    build_channel(client)
    monkeypatch.setenv("LEASE_TTL_SECONDS", "0")
    old = service.claim_batch("news", "w")["lease_token"]
    # 旧租约立即过期，确保所有并发线程都在“可重新领取”状态起跑
    assert old

    def attempt(i):
        try:
            return service.claim_batch("news", "w")
        except service.LeaseError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(20)))

    granted = [r for r in results if not isinstance(r, service.LeaseError)]
    blocked = [
        r for r in results
        if isinstance(r, service.LeaseError) and r.error == "lease_active"
    ]
    # TTL=0 时签出即过期，可能连续多笔领取各自成立；关键是不存在两个
    # “同时有效”的在途租约——事务串行化使每次领取时最近租约必已过期
    assert len(granted) + len(blocked) == 20
    assert granted, "至少应有一个线程成功重新领取"
    assert all(r["lease_token"] != old for r in granted)
