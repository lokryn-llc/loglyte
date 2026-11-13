"""Two-level fractional factorial designs, and the feature costs they estimate.

Each run of a design switches several features on at once (+1 on, -1 off). The columns
are balanced and orthogonal, so a feature's cost is simply the mean of the runs where it
was on minus the mean of the runs where it was off; every run contributes to every
estimate.

The designs are *resolution IV*: no feature's estimate is confounded with another single
feature or with any pair of features. They are built as the fold-over of a saturated
two-level design: ``2**base`` runs that hold up to ``2**base - 1`` feature columns,
followed by the same runs with every sign flipped, plus one column marking the fold.
That supports up to ``2**base`` features in ``2**(base + 1)`` runs (16 features in 32).
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass

#: An effect counts as real when it is larger than this many standard errors.
Z_95 = 1.96


@dataclass(frozen=True)
class Design:
    factors: tuple[str, ...]
    #: One tuple of +1/-1 levels per run, in ``factors`` order.
    rows: tuple[tuple[int, ...], ...]

    def pattern(self, row: tuple[int, ...]) -> str:
        """A run's levels as '+'/'-' in factor order; unique per run, stable across runs."""
        return "".join("+" if level > 0 else "-" for level in row)


@dataclass(frozen=True)
class Effect:
    factor: str
    #: Change in the response when the feature is switched on.
    cost: float
    #: Half-width of the 95% interval around ``cost``.
    interval: float

    @property
    def distinguishable(self) -> bool:
        return abs(self.cost) > self.interval


@dataclass(frozen=True)
class Analysis:
    #: Predicted response with every feature off.
    baseline: float
    effects: tuple[Effect, ...]
    #: Share of the variation between runs that the sum of individual costs explains.
    r_squared: float
    #: Residual spread over measurement noise. Near 1: features add up. Well above 2:
    #: features interact, or the machine drifted during the run.
    lack_of_fit: float | None


def fold_over_design(factors: tuple[str, ...]) -> Design:
    if not factors:
        raise ValueError("a design needs at least one factor")
    base = 0
    while 2**base < len(factors):
        base += 1
    # Columns of the saturated design: the product of every non-empty subset of the
    # base columns, single base columns first.
    words = [
        subset
        for size in range(1, base + 1)
        for subset in itertools.combinations(range(base), size)
    ]
    half = [
        tuple(math.prod(levels[j] for j in word) for word in words)
        for levels in itertools.product((-1, 1), repeat=base)
    ]
    # The fold column comes first, so small designs use it and the base columns.
    rows = [(1, *row) for row in half] + [(-1, *(-level for level in row)) for row in half]
    return Design(factors, tuple(row[: len(factors)] for row in rows))


def analyse(design: Design, responses: list[float], noise_sd: float) -> Analysis:
    """Estimate each feature's cost from one response per run.

    ``noise_sd`` is the measurement noise of a single response. The interval uses the
    larger of that and the spread the additive model leaves unexplained, so neither
    within-run noise nor slow drift across the run can make an estimate look precise.
    """
    runs, k = len(design.rows), len(design.factors)
    if len(responses) != runs:
        raise ValueError(f"expected {runs} responses, got {len(responses)}")
    mean = statistics.fmean(responses)
    costs = [
        2 * statistics.fmean(row[j] * y for row, y in zip(design.rows, responses, strict=True))
        for j in range(k)
    ]
    fitted = [
        mean + sum(c / 2 * level for c, level in zip(costs, row, strict=True))
        for row in design.rows
    ]
    residual_ss = sum((y - f) ** 2 for y, f in zip(responses, fitted, strict=True))
    total_ss = sum((y - mean) ** 2 for y in responses)

    residual_df = runs - k - 1
    residual_sd = math.sqrt(residual_ss / residual_df) if residual_df > 0 else None
    sd = max(noise_sd, residual_sd or 0.0)
    standard_error = 2 * sd / math.sqrt(runs)
    return Analysis(
        baseline=mean - sum(c / 2 for c in costs),
        effects=tuple(
            Effect(name, cost, Z_95 * standard_error)
            for name, cost in zip(design.factors, costs, strict=True)
        ),
        r_squared=1 - residual_ss / total_ss if total_ss else 1.0,
        lack_of_fit=residual_sd / noise_sd if residual_sd is not None and noise_sd else None,
    )
