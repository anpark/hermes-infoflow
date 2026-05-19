"""Ensure the deprecated Layer 1 / Layer 3 LLM judge modules are gone.

After the 3-LLM → 1-LLM merge, ``hermes_infoflow.llm_judge`` is removed
from the package. ``hermes_infoflow.bot`` must not import it, and the Bot
constructor must no longer accept ``llm_config`` / ``http_session``
parameters that only existed to feed the judge.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


def test_llm_judge_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hermes_infoflow.llm_judge")


def test_bot_module_does_not_reference_llm_judge() -> None:
    bot_mod = importlib.import_module("hermes_infoflow.bot")
    src = inspect.getsource(bot_mod)
    assert "llm_judge" not in src
    assert "_classify_followup_intent" not in src
    assert "_evaluate_reply_value" not in src


def test_bot_constructor_no_longer_takes_llm_config() -> None:
    from hermes_infoflow.bot import Bot

    sig = inspect.signature(Bot.__init__)
    assert "llm_config" not in sig.parameters
    assert "http_session" not in sig.parameters


def test_bot_module_no_aiohttp_import() -> None:
    """aiohttp was only used by Bot to feed llm_judge; should be gone now."""
    bot_mod = importlib.import_module("hermes_infoflow.bot")
    src = inspect.getsource(bot_mod)
    assert "import aiohttp" not in src
    assert "aiohttp.ClientSession" not in src
