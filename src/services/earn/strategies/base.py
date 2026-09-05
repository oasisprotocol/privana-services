from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ApyPoint:
    """One sample of a strategy's APY over time.

    `apy_bps` matches the units of `get_apy_bps`, so a chart's latest point and
    the headline APY are the same number in the same scale.
    """

    timestamp: int  # unix seconds
    apy_bps: int


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

    async def get_apy_history(self, days: Optional[int] = None) -> list[ApyPoint]:
        """APY over time, oldest first. `days` limits the window to the most
        recent N days; None returns everything the source has.

        Not abstract, and empty by default: a strategy that has no historical
        source is a normal state, not a broken one. Callers render what they get,
        so an empty list means "no chart", never a wrong chart.
        """
        return []

    @abstractmethod
    async def deposit_to_earn(self, amount: int) -> None:
        """Move idle pool funds into the external earn protocol."""

    @abstractmethod
    async def withdraw_from_earn(self, amount: int) -> None:
        """Pull funds back from the external earn protocol to the pool."""

    @abstractmethod
    async def total_assets(self) -> int:
        """Current pool AUM held by this strategy, in base units: principal
        plus accrued yield as reported by the external protocol (e.g. aToken
        balance for Aave). Returns 0 for strategies that don't hold funds
        externally (e.g. manual).
        """

    @abstractmethod
    async def idle_assets(self) -> int:
        """Pool funds credited to the pool but not deployed to the external
        protocol: an undeployed deposit, or a reclaim awaiting redeploy.

        Shares are already minted against these, so leaving them out of AUM
        understates the share-math denominator and lets the next deposit mint
        against a false low value."""

    @abstractmethod
    async def is_healthy(self) -> bool:
        """Cheap liveness probe for the external protocol. False means the
        vault should refuse new deposits into this strategy until it recovers.
        """
