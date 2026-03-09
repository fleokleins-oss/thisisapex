import time
from loguru import logger


class RobinHoodRiskEngine:
    """
    Circuit breaker for capital protection.
    - Tracks peak capital (high-water mark)
    - If drawdown > MAX_DRAWDOWN_PCT: kills all execution for LOCKOUT_SECONDS
    - Cannot be overridden — safety is non-negotiable
    """

    MAX_DRAWDOWN_PCT = 4.0
    LOCKOUT_SECONDS = 1800  # 30 minutes

    def __init__(self, initial_capital: float = 22.00):
        self.peak_capital = initial_capital
        self.current_capital = initial_capital
        self.lockout_until = 0

    def update_capital(self, new_capital: float):
        self.current_capital = new_capital
        if new_capital > self.peak_capital:
            self.peak_capital = new_capital

    def can_trade(self) -> bool:
        now = time.time()

        # Check active lockout
        if now < self.lockout_until:
            return False

        # Check drawdown
        if self.peak_capital > 0:
            drawdown_pct = ((self.peak_capital - self.current_capital) / self.peak_capital) * 100
            if drawdown_pct >= self.MAX_DRAWDOWN_PCT:
                self.lockout_until = now + self.LOCKOUT_SECONDS
                logger.critical(
                    f"ROBIN HOOD ACTIVATED | Drawdown: {drawdown_pct:.2f}% | "
                    f"Locked for {self.LOCKOUT_SECONDS}s"
                )
                return False

        return True
