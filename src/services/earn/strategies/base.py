from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """Pluggable yield source for an earn pool.

    A strategy knows how to deploy pool funds into an external protocol
    (Aave, Compound, Morpho, ...), report the current APY, expose the
    on-chain principal + accrued yield as total_assets, and signal whether
    the underlying protocol is healthy enough to route deposits into.

    Strategies are stateless. They don't persist anything themselves. The
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
        """Unrealized yield in base units (not yet harvested on-chain).

        TODO: goes away alongside harvest() once total_assets becomes the
        single source of truth for pool AUM.
        """

    @abstractmethod
    async def total_assets(self) -> int:
        """Current pool AUM held by this strategy, in base units: principal
        plus accrued yield as reported by the external protocol (e.g. aToken
        balance for Aave). Returns 0 for strategies that don't hold funds
        externally (e.g. manual).
        """

    @abstractmethod
    async def is_healthy(self) -> bool:
        """Cheap liveness probe for the external protocol. False means the
        vault should refuse new deposits into this strategy until it recovers.
        """
