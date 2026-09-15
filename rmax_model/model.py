"""Per-side model assembly and artifact save/load. Loading dispatches on the
backend tag stored alongside each side's fitted object, not on the current
config -- a model file trained with `call: linreg` keeps working after the
config default changes to something else.
"""
from __future__ import annotations

import pickle
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .backends.base import SideModel, get_backend_class
from .config import REPO_ROOT, LgbParams
from .logging_setup import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _library_versions() -> dict:
    versions = {}
    for mod_name in ("numpy", "pandas", "sklearn", "lightgbm", "scipy"):
        try:
            mod = __import__(mod_name)
            versions[mod_name] = getattr(mod, "__version__", "unknown")
        except ImportError:
            versions[mod_name] = "not installed"
    return versions


def build_side_model(backend_name: str, r_edges: tuple[float, ...], feature_names: list[str],
                      lgb_params: LgbParams) -> SideModel:
    cls = get_backend_class(backend_name)
    return cls.create(r_edges, feature_names, lgb_params)


@dataclass
class SideArtifact:
    backend_tag: str
    model: SideModel
    calibrators: dict = field(default_factory=dict)  # k -> fitted isotonic calibrator


@dataclass
class RmaxModel:
    r_edges: tuple[float, ...]
    candidate_k: tuple[float, ...]
    feature_names: list[str]
    sides: dict[str, SideArtifact]
    metadata: dict

    def predict_survival(self, option_type: str, X: pd.DataFrame, k: float, calibrated: bool = True) -> np.ndarray:
        side = self.sides[option_type]
        raw = side.model.predict_survival(X, k)
        if calibrated and k in side.calibrators:
            raw = side.calibrators[k].predict(raw)
        return raw

    def predict_expected_loss_multiple(self, option_type: str, X: pd.DataFrame, k: float) -> np.ndarray:
        return self.sides[option_type].model.predict_expected_loss_multiple(X, k)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)
        logger.info(f"Saved model artifact to {path}")

    @staticmethod
    def load(path: str | Path) -> "RmaxModel":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, RmaxModel):
            raise TypeError(f"{path} does not contain an RmaxModel")
        if obj.metadata.get("schema_version") != SCHEMA_VERSION:
            logger.warning(
                f"Loaded artifact schema_version={obj.metadata.get('schema_version')} "
                f"!= current SCHEMA_VERSION={SCHEMA_VERSION}"
            )
        return obj


def build_metadata(train_date_range: tuple, extra: dict | None = None) -> dict:
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "train_date_range": train_date_range,
        "git_sha": _git_sha(),
        "library_versions": _library_versions(),
    }
    if extra:
        metadata.update(extra)
    return metadata
