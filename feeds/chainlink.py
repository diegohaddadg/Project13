"""Chainlink oracle price feed — stub for settlement verification."""

from utils.logger import get_logger

log = get_logger("chainlink")


class ChainlinkFeed:
    """Chainlink on-chain oracle for BTC/USD settlement reference.

    TODO (Phase 5):
    - Connect to Chainlink BTC/USD price feed contract
    - Poll latest round data for settlement verification
    - Detect large deviations between spot price and oracle
    - Trigger kill switch on abnormal divergence
    """

    async def start(self) -> None:
        log.info("ChainlinkFeed is a stub — not yet implemented")

    async def stop(self) -> None:
        pass
