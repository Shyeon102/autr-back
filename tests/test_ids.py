from autr.domain.ids import client_order_id, signal_id


def test_signal_id_format():
    value = signal_id("regime_trend", "btcusdt")
    assert value.startswith("sig-regime_trend-BTCUSDT-")


def test_client_order_id_format():
    value = client_order_id("mean_reversion", "solusdt", "Buy")
    assert value.startswith("coid-mean_reversion-SOLUSDT-buy-")
