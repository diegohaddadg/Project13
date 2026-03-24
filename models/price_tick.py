from dataclasses import dataclass, field
import time


@dataclass
class PriceTick:
    """Standardized market data object for all price feeds.

    Latency semantics:
    - Binance: timestamp is the exchange trade event time (field "T").
      age_ms() measures exchange-to-local latency (includes network + processing).
    - Coinbase: ticker messages do not include a trade-event timestamp.
      timestamp is set to local receive time, so age_ms() only measures
      processing delay, NOT true exchange-to-local latency.
      This is a known limitation until Coinbase match channel is used.
    """

    timestamp: float          # Exchange event time (Binance) or local receive time (Coinbase)
    price: float
    source: str               # "binance" | "coinbase" | "polymarket"
    local_timestamp: float = field(default_factory=time.time)  # When we received the tick
    is_stale: bool = field(default=False)

    def age_ms(self) -> float:
        """Milliseconds between exchange event and local receipt.

        For Binance: true exchange-to-local latency.
        For Coinbase: ~0ms (both timestamps are local). See class docstring.
        """
        return (self.local_timestamp - self.timestamp) * 1000

    def staleness_ms(self) -> float:
        """Milliseconds since this tick was received locally. Always accurate."""
        return (time.time() - self.local_timestamp) * 1000

    def __repr__(self) -> str:
        stale_tag = " [STALE]" if self.is_stale else ""
        return (
            f"PriceTick({self.source} ${self.price:,.2f} "
            f"age={self.age_ms():.0f}ms{stale_tag})"
        )
