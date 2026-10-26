"""Configuration loading; every path is resolved relative to the repository root."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_DIR = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def settings() -> dict:
    with open(CODE_DIR / "config.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    # Environment overrides, e.g. MODEL_PLANNER=claude-opus-5-5
    for agent in list(cfg.get("models", {})):
        env = os.getenv(f"MODEL_{agent.upper()}")
        if env:
            cfg["models"][agent] = env
    return cfg


def path(key: str) -> Path:
    return REPO_ROOT / settings()["paths"][key]


def dataset_path(*parts: str) -> Path:
    return path("dataset").joinpath(*parts)
