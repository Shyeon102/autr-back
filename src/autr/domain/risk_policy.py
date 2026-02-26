def breached_one_percent(entry_price: float, current_price: float) -> bool:
    if entry_price <= 0:
        return False
    deviation = abs(current_price - entry_price) / entry_price
    return deviation >= 0.01


def stop_loss_triggered(entry_price: float, current_price: float, max_loss_pct: float = 0.05) -> bool:
    if entry_price <= 0:
        return False
    loss_pct = (entry_price - current_price) / entry_price
    return loss_pct >= max_loss_pct


def should_block_trade(daily_loss_pct: float, daily_loss_limit_pct: float = 0.03) -> bool:
    return daily_loss_pct <= -abs(daily_loss_limit_pct)
