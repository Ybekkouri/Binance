"""Trade decision output: the full, auditable record of every evaluation.

Every strategy evaluation — including the ones that decide NOT to trade —
produces one of these, and it is written verbatim to the journal.
"""

from dataclasses import dataclass, field, asdict

LONG = "LONG"
SHORT = "SHORT"
NO_TRADE = "NO_TRADE"


@dataclass
class FactorVote:
    name: str
    vote: int        # +1 bullish, -1 bearish, 0 neutral/unavailable
    weight: float
    detail: str


@dataclass
class TradeDecision:
    symbol: str
    direction: str                     # LONG / SHORT / NO_TRADE
    timestamp: str = ""
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    position_size: float = 0.0         # contracts (filled in by sizing)
    leverage: int = 0
    risk_reward: float = 0.0           # blended across TP1/TP2
    confidence: float = 0.0            # 0..1 weighted factor alignment
    market_condition: str = ""         # trending_up / trending_down / ranging / volatile
    reasons: list = field(default_factory=list)        # why enter
    risks: list = field(default_factory=list)          # why it may fail
    invalidation: str = ""             # what proves the idea wrong
    votes: list = field(default_factory=list)          # list[FactorVote]

    def to_dict(self) -> dict:
        return asdict(self)


def blended_rr(entry: float, stop: float, tp1: float, tp2: float,
               tp1_fraction: float) -> float:
    """Risk/reward averaged over the two partial targets."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    reward = tp1_fraction * abs(tp1 - entry) + (1 - tp1_fraction) * abs(tp2 - entry)
    return reward / risk
