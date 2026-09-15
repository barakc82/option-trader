"""Frozen dataclass configuration, parsed from config.yaml. No magic numbers
in library code -- everything tunable lives here."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class DataConfig:
    csv_path: str
    require_executed: bool = True

    @property
    def resolved_csv_path(self) -> Path:
        return REPO_ROOT / self.csv_path


@dataclass(frozen=True)
class PhysicsPriorConfig:
    reference_multiples: tuple[float, ...]


@dataclass(frozen=True)
class BackendsConfig:
    call: str
    put: str


@dataclass(frozen=True)
class LgbParams:
    objective: str = "multiclass"
    metric: str = "multi_logloss"
    max_depth: int = 3
    num_leaves: int = 15
    min_child_samples: int = 15
    feature_fraction: float = 0.7
    bagging_fraction: float = 0.8
    bagging_freq: int = 1
    learning_rate: float = 0.03
    num_boost_round: int = 500
    early_stopping_rounds: int = 30
    verbosity: int = -1
    seed: int = 42  # populated from the top-level Config.seed by load_config()

    def as_lgb_dict(self, num_class: int) -> dict:
        d = {
            "objective": self.objective,
            "metric": self.metric,
            "num_class": num_class,
            "max_depth": self.max_depth,
            "num_leaves": self.num_leaves,
            "min_child_samples": self.min_child_samples,
            "feature_fraction": self.feature_fraction,
            "bagging_fraction": self.bagging_fraction,
            "bagging_freq": self.bagging_freq,
            "learning_rate": self.learning_rate,
            "verbosity": self.verbosity,
            # Threading the single config seed through every LightGBM RNG use
            # (bagging row sampling, feature_fraction column sampling, and
            # the general "seed" fallback) so training is fully deterministic
            # given a fixed seed, per the "deterministic, single seed
            # threaded everywhere" requirement.
            "seed": self.seed,
            "bagging_seed": self.seed,
            "feature_fraction_seed": self.seed,
            "data_random_seed": self.seed,
        }
        # Deliberately no `monotone_constraints` here. LightGBM applies a
        # monotone constraint identically across every class column in
        # multiclass mode, which is not the per-class-boundary semantics we
        # want for an ordered-bin softmax. Do not add it back.
        return d


@dataclass(frozen=True)
class CvConfig:
    n_splits: int = 5
    walk_forward_min_train_days: int = 3


@dataclass(frozen=True)
class CalibrationConfig:
    method: str = "isotonic"


@dataclass(frozen=True)
class TailConfig:
    enabled: bool = True
    threshold_quantile: float = 0.90
    min_exceedances: int = 20


@dataclass(frozen=True)
class LabelSmoothingConfig:
    enabled: bool = False
    neighbor_weight: float = 0.1


@dataclass(frozen=True)
class BootstrapConfig:
    n_resamples: int = 1000
    ci: float = 0.95


@dataclass(frozen=True)
class BacktestConfig:
    fees: float = 0.0
    ev_threshold: float = 0.0
    stop_draw_low: float = 2.0
    stop_draw_high: float = 6.0
    n_random_draws: int = 200


@dataclass(frozen=True)
class PathsConfig:
    artifact_path: str
    report_path: str
    log_path: str

    @property
    def resolved_artifact_path(self) -> Path:
        return REPO_ROOT / self.artifact_path

    @property
    def resolved_report_path(self) -> Path:
        return REPO_ROOT / self.report_path

    @property
    def resolved_log_path(self) -> Path:
        return REPO_ROOT / self.log_path


@dataclass(frozen=True)
class Config:
    seed: int
    data: DataConfig
    r_edges: tuple[float, ...]
    candidate_k: tuple[float, ...]
    physics_prior: PhysicsPriorConfig
    backends: BackendsConfig
    lgb_params: LgbParams
    day_balanced_weights: bool
    cv: CvConfig
    calibration: CalibrationConfig
    tail: TailConfig
    label_smoothing: LabelSmoothingConfig
    bootstrap: BootstrapConfig
    backtest: BacktestConfig
    paths: PathsConfig


def _to_float(x) -> float:
    if isinstance(x, str) and x.strip().lower() in (".inf", "inf"):
        return math.inf
    return float(x)


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        path = Path(__file__).resolve().parent / "config.yaml"
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    r_edges = tuple(_to_float(x) for x in raw["r_edges"])
    candidate_k = tuple(_to_float(x) for x in raw["candidate_k"])

    return Config(
        seed=raw["seed"],
        data=DataConfig(**raw["data"]),
        r_edges=r_edges,
        candidate_k=candidate_k,
        physics_prior=PhysicsPriorConfig(
            reference_multiples=tuple(raw["physics_prior"]["reference_multiples"])
        ),
        backends=BackendsConfig(**raw["backends"]),
        lgb_params=LgbParams(**raw["lgb_params"], seed=raw["seed"]),
        day_balanced_weights=raw["day_balanced_weights"],
        cv=CvConfig(**raw["cv"]),
        calibration=CalibrationConfig(**raw["calibration"]),
        tail=TailConfig(**raw["tail"]),
        label_smoothing=LabelSmoothingConfig(**raw["label_smoothing"]),
        bootstrap=BootstrapConfig(**raw["bootstrap"]),
        backtest=BacktestConfig(**raw["backtest"]),
        paths=PathsConfig(**raw["paths"]),
    )
