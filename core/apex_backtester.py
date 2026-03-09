# apex_backtester.py
"""
APEX PREDATOR NEO – Tick-Level Backtester v666
Requisitos: pip install pandas numpy matplotlib loguru python-dotenv ccxt
"""
import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
import pandas as pd
import numpy as np
from loguru import logger
import matplotlib.pyplot as plt

# ------------------------------------------------------------
# CONFIGURAÇÕES (pode vir de .env depois)
# ------------------------------------------------------------
BINANCE_FEE_MAKER = 0.0002      # 0.02%
BINANCE_FEE_TAKER = 0.0004      # 0.04%
DEFAULT_SLIPPAGE_LIQUID = (0.0002, 0.0008)   # 0.02% a 0.08%
DEFAULT_SLIPPAGE_ILLIQUID = (0.005, 0.02)    # 0.5% a 2%

# ------------------------------------------------------------
# MODELOS DE DADOS
# ------------------------------------------------------------
@dataclass
class Tick:
    timestamp: int          # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread_bps: float = 0   # spread em basis points

@dataclass
class TradeResult:
    timestamp: int
    side: str               # 'buy' ou 'sell'
    entry_price: float
    exit_price: float
    size_usd: float
    leverage: float
    pnl_usd: float
    pnl_pct: float
    exit_reason: str        # 'tp', 'sl', 'manual', 'timeout', 'drawdown_kill'

@dataclass
class BacktestReport:
    total_pnl_usd: float
    total_pnl_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate: float
    num_trades: int
    trades: List[TradeResult]
    equity_curve: List[float]
    timestamps: List[int]

# ------------------------------------------------------------
# RISK ENGINE (ROBIN HOOD)
# ------------------------------------------------------------
class RobinHoodRisk:
    """Circuit breaker real: drawdown >4% mata tudo por 30 min."""
    def __init__(self, initial_capital: float, max_drawdown_pct: float = 4.0):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        self.max_drawdown_pct = max_drawdown_pct
        self.killed = False
        self.kill_time = 0

    def update(self, pnl_usd: float, timestamp_ms: int):
        if self.killed:
            if timestamp_ms < self.kill_time:
                return False   # ainda em cooldown
            else:
                self.killed = False
        self.current_capital += pnl_usd
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital * 100
        if drawdown >= self.max_drawdown_pct:
            self.killed = True
            self.kill_time = timestamp_ms + 30 * 60 * 1000  # 30 minutos em ms
            logger.warning(f"🚨 ROBIN HOOD KILL: drawdown {drawdown:.2f}%")
            return False
        return True

# ------------------------------------------------------------
# BACKTESTER PRINCIPAL
# ------------------------------------------------------------
class ApexBacktester:
    def __init__(self,
                 initial_capital: float = 81.75,
                 max_per_trade: float = 6.0,
                 leverage: float = 1.0,
                 fee_maker: float = BINANCE_FEE_MAKER,
                 fee_taker: float = BINANCE_FEE_TAKER,
                 slippage_range: Tuple[float, float] = DEFAULT_SLIPPAGE_LIQUID,
                 risk_engine: Optional[RobinHoodRisk] = None):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.max_per_trade = max_per_trade
        self.leverage = leverage
        self.fee_maker = fee_maker
        self.fee_taker = fee_taker
        self.slippage_range = slippage_range
        self.risk = risk_engine or RobinHoodRisk(initial_capital)
        self.trades: List[TradeResult] = []
        self.equity_curve: List[float] = [initial_capital]
        self.timestamps: List[int] = []

    def _slippage(self, price: float, side: str) -> float:
        """Aplica slippage aleatório baseado na liquidez do par."""
        slippage_pct = random.uniform(*self.slippage_range)
        if side == 'buy':
            return price * (1 + slippage_pct)
        else:
            return price * (1 - slippage_pct)

    def _apply_fees(self, amount_usd: float, taker: bool = True) -> float:
        fee_rate = self.fee_taker if taker else self.fee_maker
        return amount_usd * fee_rate

    async def run_on_ticks(self, ticks: List[Tick], signal_func: callable):
        """
        Executa backtick tick por tick.
        signal_func deve retornar (side: str, size_usd: float, tp_pct: float, sl_pct: float)
        """
        position = None  # {'side', 'entry', 'size', 'tp', 'sl', 'entry_time'}
        for i, tick in enumerate(ticks):
            # Atualiza equity curve
            self.timestamps.append(tick.timestamp)
            self.equity_curve.append(self.capital)

            # Verifica kill switch
            if not self.risk.update(0, tick.timestamp):
                logger.info("Backtest paused due to drawdown kill.")
                break

            # Se não há posição, pode abrir nova
            if position is None:
                signal = signal_func(tick, self.capital)
                if signal:
                    side, size_usd, tp_pct, sl_pct = signal
                    size_usd = min(size_usd, self.max_per_trade, self.capital)
                    if size_usd <= 0:
                        continue
                    # slippage na entrada
                    entry_price = self._slippage(tick.close, side)
                    fees = self._apply_fees(size_usd, taker=True)
                    self.capital -= fees   # paga taxa na entrada
                    position = {
                        'side': side,
                        'entry': entry_price,
                        'size': size_usd,
                        'tp': entry_price * (1 + tp_pct) if side == 'buy' else entry_price * (1 - tp_pct),
                        'sl': entry_price * (1 - sl_pct) if side == 'buy' else entry_price * (1 + sl_pct),
                        'entry_time': tick.timestamp
                    }
            else:
                # Verifica se atingiu TP ou SL
                exit_reason = None
                exit_price = None
                if position['side'] == 'buy':
                    if tick.high >= position['tp']:
                        exit_reason = 'tp'
                        exit_price = position['tp']
                    elif tick.low <= position['sl']:
                        exit_reason = 'sl'
                        exit_price = position['sl']
                else:  # sell
                    if tick.low <= position['tp']:
                        exit_reason = 'tp'
                        exit_price = position['tp']
                    elif tick.high >= position['sl']:
                        exit_reason = 'sl'
                        exit_price = position['sl']

                if exit_reason:
                    # slippage na saída
                    exit_price = self._slippage(exit_price, 'sell' if position['side'] == 'buy' else 'buy')
                    fees = self._apply_fees(position['size'], taker=True)
                    pnl = (exit_price - position['entry']) / position['entry'] * position['size'] * self.leverage
                    pnl -= fees
                    self.capital += pnl
                    trade = TradeResult(
                        timestamp=position['entry_time'],
                        side=position['side'],
                        entry_price=position['entry'],
                        exit_price=exit_price,
                        size_usd=position['size'],
                        leverage=self.leverage,
                        pnl_usd=pnl,
                        pnl_pct=pnl / position['size'] * 100,
                        exit_reason=exit_reason
                    )
                    self.trades.append(trade)
                    position = None

    def generate_report(self) -> BacktestReport:
        """Calcula métricas finais."""
        if not self.trades:
            return BacktestReport(0,0,0,0,0,0,[],self.equity_curve,self.timestamps)

        df = pd.DataFrame([t.__dict__ for t in self.trades])
        total_pnl = df['pnl_usd'].sum()
        total_pnl_pct = (self.capital - self.initial_capital) / self.initial_capital * 100
        win_rate = (df['pnl_usd'] > 0).mean() * 100
        max_dd = self._calculate_max_drawdown()
        sharpe = self._calculate_sharpe(df['pnl_usd'])
        return BacktestReport(
            total_pnl_usd=total_pnl,
            total_pnl_pct=total_pnl_pct,
            sharpe_ratio=sharpe,
            max_drawdown_pct=max_dd,
            win_rate=win_rate,
            num_trades=len(self.trades),
            trades=self.trades,
            equity_curve=self.equity_curve,
            timestamps=self.timestamps
        )

    def _calculate_max_drawdown(self) -> float:
        """Calcula max drawdown em % a partir da curva de equity."""
        eq = np.array(self.equity_curve)
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / peak * 100
        return float(np.max(dd))

    def _calculate_sharpe(self, returns: pd.Series) -> float:
        """Sharpe ratio anualizado (assume ticks em intervalo constante, ajuste depois)."""
        if len(returns) < 2 or returns.std() == 0:
            return 0.0
        # simplificado: assume 252 dias * 6.5h * 3600s = ~5.9M ticks por ano? Melhor usar períodos reais.
        # Aqui usaremos apenas razão média/desvio
        return (returns.mean() / returns.std()) * np.sqrt(252 * 6.5 * 3600 / (self.timestamps[-1] - self.timestamps[0]) * 1000) if len(self.timestamps) > 1 else 0.0

    def plot_equity_curve(self, save_path: Optional[str] = None):
        plt.figure(figsize=(12,6))
        plt.plot(self.timestamps, self.equity_curve)
        plt.title('Equity Curve')
        plt.xlabel('Timestamp (ms)')
        plt.ylabel('Capital (USD)')
        if save_path:
            plt.savefig(save_path)
        else:
            plt.show()

    def export_report(self, path: str):
        report = self.generate_report()
        with open(path, 'w') as f:
            json.dump({
                'total_pnl_usd': report.total_pnl_usd,
                'total_pnl_pct': report.total_pnl_pct,
                'sharpe_ratio': report.sharpe_ratio,
                'max_drawdown_pct': report.max_drawdown_pct,
                'win_rate': report.win_rate,
                'num_trades': report.num_trades,
                'trades': [t.__dict__ for t in report.trades],
            }, f, indent=2)

# ------------------------------------------------------------
# CARGA DE DADOS HISTÓRICOS E GERADOR SINTÉTICO
# ------------------------------------------------------------
def load_ticks_from_csv(path: str) -> List[Tick]:
    """Carrega ticks de um CSV com colunas: timestamp,open,high,low,close,volume,spread_bps (opcional)"""
    df = pd.read_csv(path)
    ticks = []
    for _, row in df.iterrows():
        ticks.append(Tick(
            timestamp=int(row['timestamp']),
            open=float(row['open']),
            high=float(row['high']),
            low=float(row['low']),
            close=float(row['close']),
            volume=float(row['volume']),
            spread_bps=float(row.get('spread_bps', 0))
        ))
    return ticks

def generate_synthetic_ticks(
    days: int = 30,
    freq_ms: int = 1000,          # 1 tick por segundo
    base_price: float = 65000,
    volatility: float = 0.0002,
    volume_mean: float = 100,
    spread_bps_mean: float = 5,   # 5 bps
    add_sweeps: bool = True
) -> List[Tick]:
    """Gera ticks sintéticos com ruído e opcionalmente sweeps."""
    np.random.seed(42)
    n_ticks = days * 24 * 3600 * 1000 // freq_ms
    timestamps = np.arange(0, n_ticks * freq_ms, freq_ms)
    prices = base_price * np.exp(np.cumsum(np.random.normal(0, volatility, n_ticks)))
    highs = prices * (1 + np.abs(np.random.normal(0, volatility/2, n_ticks)))
    lows = prices * (1 - np.abs(np.random.normal(0, volatility/2, n_ticks)))
    volumes = np.abs(np.random.normal(volume_mean, volume_mean*0.3, n_ticks))
    spreads = np.abs(np.random.normal(spread_bps_mean, spread_bps_mean*0.2, n_ticks))

    if add_sweeps:
        # injeta alguns sweep events (preço rompe rapidamente e volta)
        for _ in range(int(n_ticks * 0.001)):  # 0.1% de ticks com sweep
            idx = np.random.randint(50, n_ticks-50)
            direction = np.random.choice([-1,1])
            prices[idx:idx+20] += direction * base_price * 0.002  # +-0.2%
            highs[idx:idx+20] += direction * base_price * 0.002
            lows[idx:idx+20] += direction * base_price * 0.002
    ticks = []
    for i in range(n_ticks):
        ticks.append(Tick(
            timestamp=int(timestamps[i]),
            open=prices[i],
            high=highs[i],
            low=lows[i],
            close=prices[i],
            volume=volumes[i],
            spread_bps=spreads[i]
        ))
    return ticks

# ------------------------------------------------------------
# EXEMPLO DE USO
# ------------------------------------------------------------
if __name__ == '__main__':
    # Gerar ticks sintéticos
    ticks = generate_synthetic_ticks(days=1, freq_ms=60000)  # 1 min candles
    logger.info(f"Gerados {len(ticks)} ticks sintéticos")

    # Função de sinal fictícia
    def dummy_signal(tick: Tick, capital: float):
        if tick.close % 2 > 1:   # só pra testar
            return ('buy', 5.0, 0.01, 0.005)
        return None

    # Rodar backtest
    bt = ApexBacktester(initial_capital=81.75, max_per_trade=6.0, leverage=1)
    asyncio.run(bt.run_on_ticks(ticks, dummy_signal))
    report = bt.generate_report()
    logger.info(f"PnL: ${report.total_pnl_usd:.2f} | Sharpe: {report.sharpe_ratio:.2f} | Win rate: {report.win_rate:.1f}%")
    bt.plot_equity_curve()
