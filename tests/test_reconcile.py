from autr.domain.reconcile import reconcile


def test_reconcile_noop():
    decision = reconcile(1.0, 1.0)
    assert decision.action == "noop"


def test_reconcile_open_missing_local():
    decision = reconcile(0.0, 0.5)
    assert decision.action == "open_missing_local"


def test_reconcile_close_orphan_local():
    decision = reconcile(0.5, 0.0)
    assert decision.action == "close_orphan_local"
