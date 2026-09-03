"""Picklable target-transform helpers for select_probability_model.py.

Deliberately kept in a module that is only ever imported, never executed
directly as a script. A class or function pickles with a reference to
whatever module it was *defined* in; if that module is run via `-m` (or as
a bare script), Python executes it as `__main__`, and the pickle then
requires an unpickling process to have `__main__.<name>` defined -- which
is true only by coincidence (see app/main.py's import of ProbabilityClassifier
from regress_max_ask.py for the existing, more fragile example of working
around this same issue). Living here instead avoids that footgun for this
artifact: this module's dotted import path never changes no matter how
select_probability_model.py itself is invoked.
"""
from __future__ import annotations

import numpy as np
from sklearn.linear_model import LinearRegression


def _identity_forward(y, extra):
    return y


def _identity_inverse(pred, extra):
    return pred


def _log_forward(y, extra):
    return np.log(y)


def _log_inverse(pred, extra):
    return np.exp(pred)


def _log_ratio_forward(y, extra):
    return np.log(y / extra)


def _log_ratio_inverse(pred, extra):
    return np.exp(pred) * extra


# Each transform maps the raw target (dollar max_ask) to and from a fitting
# space. forward()/inverse() always take (values, extra) -- `extra` is the
# array named by "extra_column" (an entry of CANDIDATE_FEATURE_COLUMNS the
# transform needs beyond the target itself, e.g. log_ratio divides by
# estimated_sell_price) or None when a transform doesn't need one.
TRANSFORMS = {
    "raw": {"forward": _identity_forward, "inverse": _identity_inverse, "extra_column": None},
    "log": {"forward": _log_forward, "inverse": _log_inverse, "extra_column": None},
    "log_ratio": {"forward": _log_ratio_forward, "inverse": _log_ratio_inverse, "extra_column": "estimated_sell_price"},
}


class TransformedLinearModel:
    """Wraps a fitted LinearRegression trained on a transformed target so
    .predict() returns predictions back in the original (raw, dollar)
    target space.

    `linear_feature_columns` is the exact subset the LinearRegression was
    fit on (best_subset). `extra_column`, if set, names a column needed by
    inverse_transform that is NOT necessarily part of that subset (e.g.
    log_ratio's estimated_sell_price) -- predict() re-selects both by name
    from whatever columns X happens to carry, so callers only need to make
    sure X contains the union of the two, in any order.
    """

    def __init__(self, linear_model: LinearRegression, linear_feature_columns: list[str],
                 inverse_transform, extra_column: str | None = None):
        self.linear_model = linear_model
        self.linear_feature_columns = linear_feature_columns
        self.inverse_transform = inverse_transform
        self.extra_column = extra_column

    def predict(self, X):
        linear_pred = self.linear_model.predict(X[self.linear_feature_columns])
        extra = X[self.extra_column].to_numpy() if self.extra_column is not None else None
        return self.inverse_transform(linear_pred, extra)
