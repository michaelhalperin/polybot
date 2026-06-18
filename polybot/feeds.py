"""Live exchange data: spot price and realized volatility.

This is the *independent* information the bot needs to beat a stale Polymarket
order book. We read public, no-auth endpoints:
  * Binance  (primary)  — spot ticker + hourly candles for volatility
  * Coinbase (fallback) — spot only, if Binance is unreachable/region-blocked

Both spot and volatility are cached so a full market scan only hits the network
once per asset.
"""
from __future__ import annotations

import math
import time
from typing import Dict, Optional, Tuple

from .api.http import get_json
from .log import get_logger

# Polymarket question text -> Binance symbol. Extend as needed.
SYMBOLS = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "ether": "ETHUSDT", "eth": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
    "ripple": "XRPUSDT", "xrp": "XRPUSDT",
    "cardano": "ADAUSDT", "ada": "ADAUSDT",
    "litecoin": "LTCUSDT", "ltc": "LTCUSDT",
    "binance coin": "BNBUSDT", "bnb": "BNBUSDT",
}

# Conservative fallback annualized vol if candle data is unavailable.
DEFAULT_VOL = {
    "BTCUSDT": 0.55, "ETHUSDT": 0.70, "SOLUSDT": 0.95, "DOGEUSDT": 1.10,
    "XRPUSDT": 0.90, "ADAUSDT": 0.95, "LTCUSDT": 0.80, "BNBUSDT": 0.70,
}

SPOT_TTL = 20.0      # seconds
VOL_TTL = 3600.0     # seconds


class CryptoFeed:
    def __init__(self):
        self.log = get_logger()
        self._spot: Dict[str, Tuple[float, float]] = {}   # symbol -> (ts, price)
        self._vol: Dict[str, Tuple[float, float]] = {}     # symbol -> (ts, sigma)

    @staticmethod
    def symbol_for(text: str) -> Optional[str]:
        t = text.lower()
        # check multi-word keys first, then single tokens
        for key, sym in SYMBOLS.items():
            if key in t:
                return sym
        return None

    # ------------------------------------------------------------------
    def spot(self, symbol: str) -> Optional[float]:
        now = time.time()
        cached = self._spot.get(symbol)
        if cached and now - cached[0] < SPOT_TTL:
            return cached[1]
        price = self._fetch_spot(symbol)
        if price is not None:
            self._spot[symbol] = (now, price)
        return price

    def _fetch_spot(self, symbol: str) -> Optional[float]:
        base = symbol.replace("USDT", "")
        # Coinbase first — works from US cloud hosts (Render). Binance often
        # returns 451 (geo-blocked) there, so we try it only as a fallback.
        # retries=2 rides out Coinbase's occasional dropped connections.
        d = get_json(f"https://api.coinbase.com/v2/prices/{base}-USD/spot", retries=2)
        try:
            return float(d["data"]["amount"])
        except (TypeError, ValueError, KeyError):
            pass
        d = get_json("https://api.binance.com/api/v3/ticker/price",
                     params={"symbol": symbol}, retries=1)
        try:
            return float(d["price"])
        except (TypeError, ValueError, KeyError):
            self.log.warning("No spot price for %s (Coinbase + Binance failed)", symbol)
            return None

    # ------------------------------------------------------------------
    def volatility(self, symbol: str) -> float:
        """Annualized realized volatility from recent hourly candles."""
        now = time.time()
        cached = self._vol.get(symbol)
        if cached and now - cached[0] < VOL_TTL:
            return cached[1]
        sigma = self._fetch_vol(symbol)
        self._vol[symbol] = (now, sigma)
        return sigma

    def _fetch_vol(self, symbol: str) -> float:
        fallback = DEFAULT_VOL.get(symbol, 0.80)
        closes = self._coinbase_closes(symbol) or self._binance_closes(symbol)
        if not closes or len(closes) < 24:
            self.log.warning("No candle data for %s; using default vol %.0f%%",
                             symbol, fallback * 100)
            return fallback
        rets = [math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 10:
            return fallback
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        annual = math.sqrt(var) * math.sqrt(24 * 365)
        return max(0.05, min(3.0, annual))   # sanity clamp

    def _coinbase_closes(self, symbol: str) -> Optional[list]:
        base = symbol.replace("USDT", "")
        rows = get_json(f"https://api.exchange.coinbase.com/products/{base}-USD/candles",
                        params={"granularity": 3600}, retries=2)
        if not isinstance(rows, list) or len(rows) < 24:
            return None
        try:
            # candle = [time, low, high, open, close, volume], newest first
            return [float(r[4]) for r in sorted(rows, key=lambda r: r[0])]
        except (TypeError, ValueError, IndexError):
            return None

    def _binance_closes(self, symbol: str) -> Optional[list]:
        data = get_json("https://api.binance.com/api/v3/klines",
                        params={"symbol": symbol, "interval": "1h", "limit": 336},
                        retries=1)
        if not isinstance(data, list) or len(data) < 24:
            return None
        try:
            return [float(row[4]) for row in data]
        except (TypeError, ValueError, IndexError):
            return None
