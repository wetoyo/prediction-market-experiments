"""The cycle-boundary balance reads: one GET /portfolio/balance shared by
get_shard_balances and the get_balance_dollars right after it, and a 429
retried once (2026-11-08: the second of two back-to-back requests got a 429
at every 15-minute boundary, halting sizing ~15s each time)."""

import pytest
import requests

from order_manager import OrderManager


class _Response:
    status_code = 429


class _Client:
    def __init__(self, fail_first=0):
        self.calls = 0
        self.fail_first = fail_first
        self.balance = 10.0

    def get_balance(self):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise requests.exceptions.HTTPError("429 Too Many Requests", response=_Response())
        return {"balance_dollars": f"{self.balance:.4f}",
                "balance_breakdown": [{"exchange_index": 0, "balance": f"{self.balance:.4f}"}]}

    def place_order(self, **kw):
        return {"order_id": "o1", "fill_count": "0.00"}


@pytest.fixture
def om(monkeypatch):
    om = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
    om.dry_run = False
    om._client = _Client()
    monkeypatch.setattr(OrderManager, "BALANCE_429_RETRY_SECONDS", 0.0)
    return om


def test_boundary_reads_share_one_request(om):
    assert om.get_shard_balances() == {0: 10.0}
    assert om.get_balance_dollars(reuse_shard_fetch=True) == 10.0
    assert om._client.calls == 1


def test_reuse_is_one_shot_and_off_by_default(om):
    om.get_shard_balances()
    om._client.balance = 12.0
    assert om.get_balance_dollars() == 12.0  # not asked to reuse: fresh read
    assert om.get_balance_dollars(reuse_shard_fetch=True) == 12.0  # nothing left to reuse
    assert om._client.calls == 3


def test_order_voids_the_shared_response(om):
    om.get_shard_balances()
    om.buy_favored_side("KXBTC15M-26NOV080015-15", "yes", 1, 0.95)
    om._client.balance = 9.0
    assert om.get_balance_dollars(reuse_shard_fetch=True) == 9.0
    assert om._client.calls == 2


def test_429_is_retried_once(om):
    om._client.fail_first = 1
    assert om.get_balance_dollars() == 10.0
    assert om._client.calls == 2


def test_second_429_raises(om):
    om._client.fail_first = 2
    with pytest.raises(requests.exceptions.HTTPError):
        om.get_balance_dollars()
