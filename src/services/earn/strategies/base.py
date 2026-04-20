from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """Pluggable yield source for an earn pool.

    A strategy knows how to deploy pool funds into an external protocol
    (Aave, Compound, Morpho, ...), report the current APY, and surface the
    unrealized yield so the vault can harvest it on-chain.

    Strategies are stateless — they don't persist anything themselves. The
    vault reads share/asset state from EarnManager and uses the strategy as
    a pure adapter over the external protocol.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, e.g. 'manual', 'aave-v3'."""

    @abstractmethod
    async def get_apy_bps(self) -> int:
        """Current APY in basis points (500 = 5%)."""

    @abstractmethod
    async def deposit_to_earn(self, amount: int) -> None:
        """Move idle pool funds into the external earn protocol."""

    @abstractmethod
    async def withdraw_from_earn(self, amount: int) -> None:
        """Pull funds back from the external earn protocol to the pool."""

    @abstractmethod
    async def pending_yield(self) -> int:
        """Unrealized yield in base units (not yet harvested on-chain)."""
