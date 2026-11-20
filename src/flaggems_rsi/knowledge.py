"""Compact research memory derived from immutable experiment facts."""

from __future__ import annotations

from .map import IterationMap


def planning_context(iteration_map: IterationMap, operator: str) -> dict[str, object]:
    """Only public experiment summaries enter the model context.

    Raw logs, private holdout cases, and evaluator implementation never pass
    through this function.
    """
    return iteration_map.summary(operator)
