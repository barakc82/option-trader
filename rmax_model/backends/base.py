"""SideModel protocol every backend must satisfy, plus a name->class
registry. Adding a third backend later means writing a new module and
registering it here -- nothing in the training loop, calibration, or
evaluation code should ever need to change or branch on backend identity.

Shared calling convention for every backend (so the training loop can treat
them identically):
  - X passed to fit()/predict_survival()/predict_expected_loss_multiple() is
    the full per-side processed frame: original (possibly renamed) raw CSV
    columns plus the engineered FEATURES columns, concatenated. Each backend
    selects the columns it actually needs by name and ignores the rest.
  - y is R_max (the ratio max_ask/credit), never log(R_max) and never a
    dollar amount -- backends convert internally if their underlying
    estimator needs a different target scale.
  - groups is session_date, for whatever internal CV a backend does on its
    own (e.g. the linreg adapter's nested best-subset selection).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd


@runtime_checkable
class SideModel(Protocol):
    name: str

    def fit(self, X: pd.DataFrame, y: pd.Series, groups: pd.Series,
            sample_weight: np.ndarray | None = None) -> None: ...

    def predict_survival(self, X: pd.DataFrame, k: float) -> np.ndarray: ...

    def predict_expected_loss_multiple(self, X: pd.DataFrame, k: float) -> np.ndarray: ...


_REGISTRY: dict[str, type] = {}


def register_backend(name: str):
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls
    return decorator


def get_backend_class(name: str) -> type:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown backend '{name}'. Registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def registered_backend_names() -> list[str]:
    return sorted(_REGISTRY)
