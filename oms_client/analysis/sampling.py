"""Select chart points while preserving endpoints and global extrema."""

from __future__ import annotations

import numpy as np
import pandas as pd


def select_points(result: pd.Series, limit: int) -> np.ndarray:
    if len(result) <= limit:
        return np.arange(len(result))
    selected = set(np.linspace(0, len(result) - 1, limit, dtype=int))
    protected = {0, len(result) - 1}
    finite = np.flatnonzero(np.isfinite(result.to_numpy()))
    if len(finite):
        values = result.to_numpy()[finite]
        for index in (int(finite[np.argmin(values)]), int(finite[np.argmax(values)])):
            if len(protected) < limit:
                selected.add(index)
                protected.add(index)
    while len(selected) > limit:
        choices = [i for i in sorted(selected) if i not in protected]
        # Remove the point nearest another retained point, preserving endpoints
        # and the global extrema. Full data is retained separately for exports.
        ordered = sorted(selected)
        positions = {value: pos for pos, value in enumerate(ordered)}
        remove = min(
            choices, key=lambda i: min(i - ordered[positions[i] - 1], ordered[positions[i] + 1] - i)
        )
        selected.remove(remove)
    return np.array(sorted(selected), dtype=int)
