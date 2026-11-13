"""The feature matrix's statistics: design properties and effect recovery."""

import itertools
import random

import pytest

from benchmarks.design import analyse, fold_over_design


def _column(design, j):
    return [row[j] for row in design.rows]


@pytest.mark.parametrize("k", [1, 2, 5, 8, 11, 16])
def test_design_is_balanced_orthogonal_and_resolution_iv(k):
    design = fold_over_design(tuple(f"f{j}" for j in range(k)))
    columns = [_column(design, j) for j in range(k)]
    assert len(set(design.rows)) == len(design.rows)  # no repeated runs
    for col in columns:
        assert sum(col) == 0  # each feature on in exactly half the runs
    for a, b in itertools.combinations(columns, 2):
        assert sum(x * y for x, y in zip(a, b, strict=True)) == 0  # features are not confounded
    # Resolution IV: no feature is confounded with any pair of the others.
    for j, col in enumerate(columns):
        for a, b in itertools.combinations([c for i, c in enumerate(columns) if i != j], 2):
            assert sum(x * y * z for x, y, z in zip(col, a, b, strict=True)) == 0


def test_design_size():
    assert len(fold_over_design(tuple("abcdefgh")).rows) == 16
    assert len(fold_over_design(tuple("abcdefghijk")).rows) == 32


def test_analysis_recovers_additive_costs():
    design = fold_over_design(("json", "redact", "location", "free"))
    true_costs = {"json": 6_000.0, "redact": 1_200.0, "location": 3_500.0, "free": 0.0}
    rng = random.Random(1)
    responses = [
        4_000
        + sum(true_costs[f] * (level > 0) for f, level in zip(design.factors, row, strict=True))
        + rng.gauss(0, 50)
        for row in design.rows
    ]
    analysis = analyse(design, responses, noise_sd=50)
    by_name = {e.factor: e for e in analysis.effects}
    for name, cost in true_costs.items():
        assert by_name[name].cost == pytest.approx(cost, abs=150)
    assert not by_name["free"].distinguishable
    assert by_name["redact"].distinguishable
    assert analysis.baseline == pytest.approx(4_000, abs=150)
    assert analysis.r_squared > 0.99


def test_interaction_shows_as_lack_of_fit():
    design = fold_over_design(("a", "b", "c"))
    responses = [1_000 + (5_000 if row[0] > 0 and row[1] > 0 else 0) for row in design.rows]
    assert analyse(design, responses, noise_sd=10).lack_of_fit > 2
