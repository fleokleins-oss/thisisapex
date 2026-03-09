# adversarial_shield.py
"""
APEX PREDATOR NEO – Adversarial Shield v666
Defesas reais: jitter adaptativo, ordens fantasma, rate‑limit evasion, ghost execution, circuit breaker comportamental.
"""
import asyncio
import random
import time
from typing import List, Dict, Optional, Callable, Any
from loguru import logger
import ccxt.pro as ccxt
from dotenv import load_dotenv
import os

load_dotenv()

class AdversarialShield:
    """
    Escudo que envolve chamadas à exchange, aplicando:
    - Jitter adaptativo em delays
    - Rate-limit evasion (parallel requests, backoff exponencial, proxy pool simulado)
    - Ghost execution (ordens IOC com parâmetros stealth)
    - Circuit breaker comportamental (detecta padrões de detecção)
    - Subaccount rotation simulation (alterna entre múltiplas chaves)
    """

    def __init__(self,
                 exchange: ccxt.Exchange,
                 config: Optional[Dict] = None):
        self.exchange = exchange
        self.config = config or {}
        self._request_timestamps: List[float] = []
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_until = 0
        self._jitter_range = self.config.get('jitter_range', (0.6, 1.4))
        self._rate_limit = self.config.get('rate_limit', 1200)  # requests per minute
        self._backoff_base = self.config.get('backoff_base', 1.0)
        self._max_backoff = self.config.get('max_backoff', 60)
        self._proxy_list = self.config.get('proxies', [])  # lista de strings 'http://proxy:port'
        self._api_keys = self.config.get('api_keys', [])   # lista de dicts {'apiKey': ..., 'secret': ...}
        self._current_key_idx = 0

        # Estatísticas para debug
        self.stats = {
            'jitter_applied': 0,
            'backoff_events': 0,
            'proxy_switches': 0,
            'key_rotations': 0,
            'ghost_orders': 0,
            'circuit_breaks': 0,
        }

    # ------------------------------------------------------------------
    # 1. Jitter adaptativo
    # ------------------------------------------------------------------
    async def _jitter_sleep(self, base_seconds: float):
        """Sleep com fator aleatório para disfarçar padrões."""
        factor = random.uniform(*self._jitter_range)
        delay = base_seconds * factor
        self.stats['jitter_applied'] += 1
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # 2. Rate-limit evasion com backoff exponencial e proxy pool
    # ------------------------------------------------------------------
    async def _rate_limiter(self):
        """Gerencia janela deslizante de requests e aplica backoff se necessário."""
        now = time.time()
        window = 60  # 1 minuto
        self._request_timestamps = [ts for ts in self._request_timestamps if now - ts < window]
        if len(self._request_timestamps) >= self._rate_limit:
            # Atingiu limite, espera até o próximo slot
            sleep_time = self._request_timestamps[0] + window - now
            if sleep_time > 0:
                logger.debug(f"Rate limit hit, sleeping {sleep_time:.2f}s")
                await self._jitter_sleep(sleep_time)
        self._request_timestamps.append(now)

    async def _backoff(self, attempt: int):
        """Exponential backoff com jitter."""
        delay = min(self._backoff_base * (2 ** attempt), self._max_backoff)
        delay *= random.uniform(0.8, 1.2)
        self.stats['backoff_events'] += 1
        logger.debug(f"Backoff attempt {attempt}, sleeping {delay:.2f}s")
        await asyncio.sleep(delay)

    def _get_proxy(self) -> Optional[str]:
        """Retorna proxy aleatório se disponível (simula pool)."""
        if self._proxy_list:
            return random.choice(self._proxy_list)
        return None

    def _rotate_api_key(self):
        """Alterna para a próxima chave (simula rotação de subconta)."""
        if len(self._api_keys) > 1:
            self._current_key_idx = (self._current_key_idx + 1) % len(self._api_keys)
            key = self._api_keys[self._current_key_idx]
            self.exchange.apiKey = key['apiKey']
            self.exchange.secret = key['secret']
            self.stats['key_rotations'] += 1
            logger.info(f"Rotated to API key #{self._current_key_idx}")

    # ------------------------------------------------------------------
    # 3. Ghost execution
    # ------------------------------------------------------------------
    async def ghost_market_order(self, symbol: str, side: str, amount: float, quote_qty: Optional[float] = None) -> Optional[Dict]:
        """
        Ordem market com parâmetros que minimizam footprint:
        - newOrderRespType='ACK' (apenas confirmação, sem detalhes de fill)
        - timeInForce='IOC' (immediate-or-cancel) para não ficar no book
        - useQuoteOrderQty para compras com USDT
        """
        params = {
            'newOrderRespType': 'ACK',   # não retorna fills detalhados
            'timeInForce': 'IOC',        # ordem não entra no book
        }
        if quote_qty and side == 'buy':
            params['quoteOrderQty'] = quote_qty
            amount = None
        try:
            order = await self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount,
                params=params
            )
            self.stats['ghost_orders'] += 1
            logger.debug(f"Ghost order placed: {symbol} {side} {amount or quote_qty}")
            return order
        except Exception as e:
            logger.error(f"Ghost order failed: {e}")
            return None

    # ------------------------------------------------------------------
    # 4. Circuit breaker comportamental
    # ------------------------------------------------------------------
    async def _check_behavioral_patterns(self) -> bool:
        """
        Detecta padrões que indicam que a Binance pode estar nos marcando:
        - Muitos erros 418 (I'm a teapot) ou 403
        - Latência anormalmente alta constante
        - Respostas com "banned" ou "locked"
        (simulação simples)
        """
        # Exemplo: se mais de 5 erros consecutivos, abre circuito
        if self._consecutive_failures >= 5:
            self._circuit_open = True
            self._circuit_until = time.time() + 300  # 5 minutos
            self.stats['circuit_breaks'] += 1
            logger.critical("Circuit breaker opened due to consecutive failures")
            return False
        return True

    async def shielded_call(self, coro: Callable, *args, **kwargs) -> Any:
        """
        Executa uma chamada à exchange envolvida por todas as defesas.
        """
        # Circuit breaker
        if self._circuit_open:
            if time.time() < self._circuit_until:
                raise Exception("Circuit breaker is open")
            else:
                self._circuit_open = False
                self._consecutive_failures = 0

        # Rate limiter
        await self._rate_limiter()

        # Jitter antes da chamada
        await self._jitter_sleep(0.1)

        # Opcional: trocar proxy
        proxy = self._get_proxy()
        if proxy:
            self.exchange.proxy = proxy
            self.stats['proxy_switches'] += 1

        # Executa com retry e backoff
        for attempt in range(5):
            try:
                result = await coro(*args, **kwargs)
                self._consecutive_failures = 0
                return result
            except (ccxt.RateLimitExceeded, ccxt.NetworkError) as e:
                logger.warning(f"Rate limit / network error (attempt {attempt}): {e}")
                await self._backoff(attempt)
                if attempt >= 2:
                    self._rotate_api_key()   # tenta outra chave
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                self._consecutive_failures += 1
                await self._check_behavioral_patterns()
                raise

        raise Exception("Max retries exceeded")

# ------------------------------------------------------------------
# INTEGRAÇÃO COM APEX_PREDATOR_NEO.PY
# ------------------------------------------------------------------
"""
Exemplo de uso no apex_predator_neo.py:

from adversarial_shield import AdversarialShield

class ApexPredatorNeo:
    def __init__(self):
        self.exchange = ccxtpro.binance({'apiKey': '...', 'secret': '...'})
        # Configura shield com múltiplas chaves e proxies simulados
        self.shield = AdversarialShield(
            exchange=self.exchange,
            config={
                'api_keys': [{'apiKey': 'key1', 'secret': 'sec1'}, {'apiKey': 'key2', 'secret': 'sec2'}],
                'proxies': ['http://proxy1:8080', 'http://proxy2:8080'],
                'jitter_range': (0.5, 1.5),
                'rate_limit': 1000,
            }
        )

    async def place_ghost_order(self, symbol, side, amount):
        # Usa ghost_market_order diretamente (já aplica defesas internas)
        return await self.shield.ghost_market_order(symbol, side, amount)

    async def fetch_balance_safely(self):
        # Qualquer chamada à exchange pode ser protegida por shielded_call
        return await self.shield.shielded_call(self.exchange.fetch_balance)
"""

# ------------------------------------------------------------------
# TESTE UNITÁRIO (simulação)
# ------------------------------------------------------------------
async def test_shield():
    class DummyExchange:
        async def fetch_balance(self):
            return {'USDT': {'free': 100}}
        async def create_order(self, *args, **kwargs):
            return {'id': '123', 'status': 'closed'}
    ex = DummyExchange()
    shield = AdversarialShield(ex, config={'jitter_range': (0.1,0.2)})
    bal = await shield.shielded_call(ex.fetch_balance)
    print(bal)
    ghost = await shield.ghost_market_order('BTC/USDT', 'buy', 0.001)
    print(ghost)

if __name__ == '__main__':
    asyncio.run(test_shield())
