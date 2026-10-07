"""
Edge calibration.

A signal score is not a probability. The calibrator turns a score into an
estimate of the trade's win rate and payoff (in R-multiples, where 1R is the
amount risked between entry and stop) using realised trades from walk-forward
backtests, shrunk toward a conservative prior with a Beta-binomial model so a
handful of lucky trades can't inflate the bot's confidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, Optional

BUCKETS = [(0.0, 0.35), (0.35, 0.50), (0.50, 1.01)]


@dataclass(frozen=True)
class EdgeEstimate:
    p_win: float
    avg_win_r: float
    avg_loss_r: float
    n_trades: int
    source: str  # "prior" | "calibrated"

    @property
    def ev_r(self) -> float:
        """Expected value per trade in R (before costs)."""
        return self.p_win * self.avg_win_r - (1 - self.p_win) * self.avg_loss_r

    @property
    def kelly(self) -> float:
        """Kelly fraction of capital to *risk* (full Kelly; callers scale it down)."""
        if self.avg_loss_r <= 0 or self.avg_win_r <= 0:
            return 0.0
        b = self.avg_win_r / self.avg_loss_r
        return self.p_win - (1 - self.p_win) / b

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(ev_r=round(self.ev_r, 4), kelly=round(self.kelly, 4))
        return d


def _bucket(score: float) -> int:
    s = abs(score)
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= s < hi:
            return i
    return len(BUCKETS) - 1


class Calibrator:
    # Conservative prior: trend/momentum systems with ~2R targets typically win
    # 35–45% of trades; average winners are smaller than the target because
    # trailing stops and time stops close many trades early.
    PRIOR_STRENGTH = 30.0       # pseudo-trades backing the prior win rate
    PAYOFF_PRIOR_STRENGTH = 15.0

    def __init__(self, stats: Optional[Dict] = None):
        self.stats = stats or {}

    @staticmethod
    def prior(score: float) -> EdgeEstimate:
        s = min(1.0, abs(score))
        return EdgeEstimate(p_win=0.38 + 0.12 * s, avg_win_r=1.6, avg_loss_r=1.0, n_trades=0, source="prior")

    @classmethod
    def fit(cls, trades: Iterable[dict]) -> "Calibrator":
        """trades: iterable of {"entry_score": float, "r_multiple": float}."""
        stats: Dict[str, Dict[str, float]] = {}
        for t in trades:
            r = t.get("r_multiple")
            score = t.get("entry_score")
            if r is None or score is None:
                continue
            b = stats.setdefault(str(_bucket(score)), {"n": 0, "wins": 0, "sum_win_r": 0.0, "sum_loss_r": 0.0})
            b["n"] += 1
            if r > 0:
                b["wins"] += 1
                b["sum_win_r"] += r
            else:
                b["sum_loss_r"] += -r
        return cls(stats)

    def estimate(self, score: float) -> EdgeEstimate:
        prior = self.prior(score)
        b = self.stats.get(str(_bucket(score)))
        if not b or b["n"] == 0:
            return prior
        n, wins = b["n"], b["wins"]
        losses = n - wins
        k, kp = self.PRIOR_STRENGTH, self.PAYOFF_PRIOR_STRENGTH
        p = (wins + prior.p_win * k) / (n + k)
        avg_win = (b["sum_win_r"] + prior.avg_win_r * kp) / (wins + kp)
        avg_loss = (b["sum_loss_r"] + prior.avg_loss_r * kp) / (losses + kp)
        return EdgeEstimate(p_win=p, avg_win_r=avg_win, avg_loss_r=avg_loss, n_trades=int(n), source="calibrated")

    def to_dict(self) -> Dict:
        return self.stats
