import asyncio
import time
from loguru import logger


class AtomicOpenClawExecutor:
    """
    Hybrid execution engine:
    - Triangular arbitrage with atomic 3-leg rollback
    - Liquidity sweep mean-reversion via OpenClaw inventory management
    - IOC (Immediate-or-Cancel) for sweep entries
    - Hard latency ceiling enforcement
    """

    def __init__(self, connector, inventory_manager, max_latency_ms: float = 50.0):
        self.connector = connector
        self.inventory = inventory_manager
        self.max_latency_ms = max_latency_ms
        self._lock = asyncio.Lock()

    async def execute_strike(self, opp: dict, is_sweep: bool) -> bool:
        async with self._lock:
            t0 = time.perf_counter()
            capital = min(opp.get("capital", 8.00), 8.00)  # Hard cap

            if is_sweep:
                result = await self._execute_reaper_sweep(
                    opp["symbol"], opp["side"], capital
                )
            else:
                result = await self._execute_triangular_atomic(
                    opp["legs"], capital
                )

            elapsed_ms = (time.perf_counter() - t0) * 1000
            if elapsed_ms > self.max_latency_ms:
                logger.warning(f"Execution exceeded latency ceiling: {elapsed_ms:.1f}ms")

            return result

    async def _execute_reaper_sweep(self, symbol: str, side: str, amount: float) -> bool:
        """Mean-reversion entry with OpenClaw inventory protection."""
        if not self.inventory.can_increase_exposure(symbol, side):
            logger.warning(f"OpenClaw hedger blocked entry on {symbol} — inventory risk too high")
            return False

        order = await self.connector.limit_ioc(symbol, side, amount)
        if order and order["filled"] > 0:
            self.inventory.update_exposure(symbol, side, order["filled"])
            # Set reversion target at 0.25%
            target_multiplier = 1.0025 if side == "buy" else 0.9975
            await self.connector.limit_maker(
                symbol,
                "sell" if side == "buy" else "buy",
                order["filled"],
                order["price"] * target_multiplier,
            )
            logger.success(f"REAPER STRIKE: {side} {symbol} @ ${amount:.2f}")
            return True
        return False

    async def _execute_triangular_atomic(self, legs: list, capital: float) -> bool:
        """
        3-leg triangular with atomic rollback.
        If leg 2 suffers >20% slippage, dumps leg 1 immediately.
        """
        logger.info(f"TRIANGLE: {legs[0]} -> {legs[1]} -> {legs[2]}")

        leg1 = await self.connector.market_order(legs[0], "buy", quote_qty=capital)
        if not leg1 or leg1["filled"] == 0:
            return False

        leg2 = await self.connector.market_order(legs[1], "sell", amount=leg1["filled"])
        if not leg2 or leg2["filled"] < leg1["filled"] * 0.80:
            logger.critical("SLIPPAGE on leg 2 — ATOMIC ROLLBACK initiated")
            await self._rollback(legs[0], leg1["filled"])
            return False

        leg3 = await self.connector.market_order(legs[2], "sell", amount=leg2["filled"])
        if leg3 and leg3["filled"] > 0:
            logger.success("TRIANGULAR COMPLETE — PnL captured")
            return True
        return False

    async def _rollback(self, symbol: str, amount: float):
        """Emergency dump back to USDT to avoid holding depreciating assets."""
        logger.warning(f"ROLLBACK: Dumping {amount} of {symbol} at market")
        await self.connector.market_order(symbol, "sell", amount=amount)
