from decimal import Decimal

from pydantic import BaseModel, Field

_USD_SCALE = Decimal(10**8)


def usd_string(value_e8: int) -> str:
    """Render a fixed-point e8 fiat value as a plain decimal string.

    Fixed notation rather than str(Decimal), which switches to scientific
    notation for small values ("1E-8") that a chart client then has to parse
    back out.
    """
    return f"{Decimal(value_e8) / _USD_SCALE:.8f}"


class PortfolioHistoryPoint(BaseModel):
    timestamp: int = Field(..., description="Unix seconds")
    total_usd: str = Field(..., description="available + locked + earn, USD")
    available_usd: str = Field(..., description="Spendable accounting balance, USD")
    locked_usd: str = Field(..., description="Balance held in locks, USD")
    earn_usd: str = Field(..., description="Value of earn positions, USD")


class PortfolioHistoryResponse(BaseModel):
    # Oldest first. Empty when the user has no accounting history and no earn
    # positions — there is no chart to draw, which is a normal answer.
    points: list[PortfolioHistoryPoint]


class EarnHistoryPoint(BaseModel):
    timestamp: int = Field(..., description="Unix seconds")
    value_usd: str = Field(..., description="Value of earn positions, USD")


class EarnHistoryResponse(BaseModel):
    points: list[EarnHistoryPoint]
