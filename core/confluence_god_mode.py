import numpy as np
import ray
from loguru import logger


class ConfluenceGodMode:
    """
    APEX CITADEL — Hybrid Confluence Engine
    Fuses triangular arbitrage signals with OpenClaw microstructure
    analysis and ML-driven sweep prediction.
    
    Filters:
        1. Tire Pressure      — Volatility compression (std dev)
        2. Lead-Lag Gravity    — Trend attraction baseline
        3. Fake Momentum       — Filters false spikes from rival HFTs
        4. OI Temporal Consist — Open Interest tracks price movement
        5. OI Delta/Vol Ratio  — Rejects low-liquidity traps (ratio < 0.5)
        6. Post-Spike Revert   — Confirms mean reversion after exhaustion
        7. OI Acceleration     — Detects whale manipulation via gradient
        8. Liquidity Reaper    — Sweep detection + orderbook imbalance
    """

    MIN_SCORE = 6.0
    SWEEP_THRESHOLD_PCT = 0.0015  # 0.15%

    def __init__(self, redis_client, ml_agent_ref):
        self.redis = redis_client
        self.ml_agent = ml_agent_ref
        self.oi_cache: dict[str, np.ndarray] = {}

    async def calculate_absolute_score(self, symbol: str, lob_data: dict) -> dict:
        """
        Processes the Local Order Book (LOB) through all 8 filters.
        Returns validity, score, sweep status, and imbalance.
        All computation is vectorized via NumPy — zero Python loops.
        """
        try:
            bids = np.array(lob_data["bids"])
            asks = np.array(lob_data["asks"])
            price = (bids[0][0] + asks[0][0]) / 2.0

            # --- Filters 1-3: Base Market Structure ---
            volatility = np.std(lob_data.get("price_history", [price] * 20)[-20:])
            tire_pressure = 1.0 if volatility < 0.0012 else 0.0

            lead_lag = 1.0 if lob_data.get("lead_lag", 0) > 0.65 else 0.0
            fake_momentum = 1.0 if lob_data.get("fake_momentum", 0) < 1.8 else 0.0

            # --- Filters 4-6: Open Interest & Reversion ---
            oi_consistency = 1.0 if lob_data.get("oi_consistency", 0) > 0.75 else 0.0

            oi_delta = lob_data.get("oi_delta", 0)
            volume_ratio = lob_data.get("volume_ratio", 1.0)
            ratio_score = 1.0 if volume_ratio > 0 and (abs(oi_delta) / volume_ratio) > 0.5 else 0.0

            post_spike = 1.0 if lob_data.get("post_spike_reversion", 0) > 0.6 else 0.0

            # --- Filter 7: OI Acceleration (Whale Trap Detector) ---
            oi_accel = self._oi_acceleration_filter(symbol, oi_delta)
            if oi_accel == -1:
                # Whale trap detected — abort immediately
                return {"valid": False, "score": 0, "is_sweep": False, "imbalance": 0}

            # --- Filter 8: Liquidity Reaper (Sweep + Microstructure) ---
            imbalance = (np.sum(bids[:10, 1]) - np.sum(asks[:10, 1])) / (
                np.sum(bids[:10, 1]) + np.sum(asks[:10, 1]) + 1e-9
            )
            aggressive_buy = lob_data.get("agg_buy", 0)
            refill = 1.0 if aggressive_buy > lob_data.get("avg_agg_buy", 1) * 2.5 else 0.0

            sweep_up = bids[0][0] > price * (1 + self.SWEEP_THRESHOLD_PCT)
            sweep_down = asks[0][0] < price * (1 - self.SWEEP_THRESHOLD_PCT)

            reaper_base = 0.0
            if sweep_up and imbalance < -0.35 and refill:
                reaper_base = 0.95
            elif sweep_down and imbalance > 0.35 and refill:
                reaper_base = 0.95

            # --- ML Confidence (Ray distributed inference) ---
            ml_features = {
                "imbalance": float(imbalance),
                "volume_delta": float(oi_delta),
                "refill": float(refill),
                "oi_accel": float(oi_accel),
            }
            ml_confidence = await ray.get(
                self.ml_agent.predict_sweep_prob.remote(ml_features)
            )

            reaper_final = reaper_base * ml_confidence

            # --- Final Score ---
            scores = np.array([
                tire_pressure, lead_lag, fake_momentum,
                oi_consistency, ratio_score, post_spike,
                float(oi_accel), reaper_final
            ])
            final_score = np.sum(scores) + (ml_confidence * 0.5)

            is_valid = final_score >= self.MIN_SCORE
            if is_valid:
                logger.success(
                    f"CONFLUENCE HIT: {symbol} | "
                    f"Score: {final_score:.2f} | ML: {ml_confidence:.2f} | "
                    f"Sweep: {'UP' if sweep_up else 'DOWN' if sweep_down else 'NO'}"
                )

            return {
                "valid": is_valid,
                "score": round(final_score, 3),
                "is_sweep": sweep_up or sweep_down,
                "imbalance": round(float(imbalance), 4),
            }

        except Exception as e:
            logger.error(f"Confluence error on {symbol}: {e}")
            return {"valid": False, "score": 0, "is_sweep": False, "imbalance": 0}

    def _oi_acceleration_filter(self, symbol: str, current_oi_delta: float) -> float:
        """
        Filter 7: Detects artificial OI acceleration (whale manipulation).
        Returns 1.0 if clean, 0.0 if uncertain, -1 if trap detected.
        """
        if symbol not in self.oi_cache:
            self.oi_cache[symbol] = np.array([current_oi_delta] * 3)
            return 0.0

        self.oi_cache[symbol] = np.roll(self.oi_cache[symbol], -1)
        self.oi_cache[symbol][-1] = current_oi_delta

        accel = np.gradient(self.oi_cache[symbol])[-1]
        std = np.std(self.oi_cache[symbol])

        if std > 0 and abs(accel) > 1.8 * std:
            logger.warning(f"OI TRAP detected on {symbol} | accel={accel:.4f}")
            return -1
        return 1.0
