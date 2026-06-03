from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.live_llm


def test_glm_prompt_behavior_live() -> None:
    if os.getenv("INFOFLOW_RUN_LIVE_LLM_TESTS") != "1":
        pytest.skip("set INFOFLOW_RUN_LIVE_LLM_TESTS=1 to run live GLM prompt checks")
    repo_root = Path(__file__).resolve().parents[2]
    sim_dir = repo_root / "scripts" / "sim"
    sys.path.insert(0, str(sim_dir))
    from prompt_behavior_glm import run_cases

    _, results = run_cases()
    failures = [result.as_dict() for result in results if not result.passed]
    assert not failures
