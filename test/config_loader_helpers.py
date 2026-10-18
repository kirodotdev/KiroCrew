"""Load helpers and hypothesis strategies the config loader test modules share.

``_load_from_dict`` writes a document to a temporary JSON file, points
``config_path`` at it and loads it through ``KiroCrewConfig.load()``;
``_load_from_dict_with_logs`` does the same while capturing the loader's warnings.
The ``_*_st`` strategies generate names and valid config sections for the property
tests. The ``test_config_loader*.py`` modules that need them import them from here.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest.mock
from pathlib import Path

from hypothesis import strategies as st

from kiro_crew.config.loader import (
    AgentConfig,
    DashboardConfig,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    MemoryConfig,
    MemoryStoreConfig,
    SessionConfig,
    SlackConfig,
    WorkspaceConfig,
)
from kiro_crew.memory_stores import memory_store_name_defect

# ---------------------------------------------------------------------------
# Helpers / Strategies
# ---------------------------------------------------------------------------


def _load_from_dict(data: object) -> KiroCrewConfig:
    """Write *data* to a temp config file and load via KiroCrewConfig.load()."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
    ) as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


def _load_from_dict_with_logs(data: object) -> tuple[KiroCrewConfig, list[str]]:
    """Load config and capture warning log messages."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
    ) as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            logger = logging.getLogger("kiro_crew.config.loader")
            messages: list[str] = []
            original_warning = logger.warning

            def capture_warning(msg: object, *args: object) -> None:
                try:
                    messages.append(str(msg) % args)
                except Exception:
                    messages.append(str(msg))
                original_warning(msg, *args)

            with unittest.mock.patch.object(logger, "warning", capture_warning):
                result = KiroCrewConfig.load()
            return result, messages
    finally:
        tmp.unlink(missing_ok=True)


# Hypothesis strategy for safe identifier strings (no control chars, JSON-safe)
_safe_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
    min_size=1,
    max_size=15,
)

# Memory-store names are stricter than the generic identifier alphabet above: a
# store name becomes a single path segment, so ``memory_store_name_defect``
# refuses an underscore, an outer hyphen and a Windows device basename. Such a
# name is UNDECLARED for resolution however the config spells it, which is what
# stops ``resolve_agent_bindings`` from handing a crew a store no resolver will
# compose a path for. A property about a store a crew is genuinely BOUND to
# therefore has to generate a name the shape rule accepts.
_store_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
    min_size=1,
    max_size=15,
).filter(lambda n: memory_store_name_defect(n) is None)

# Strategy for KiroCrewAgentConfig instances
_kirocrew_agent_config_st = st.builds(
    KiroCrewAgentConfig,
    kiro_agent=st.text(min_size=0, max_size=20),
    workspace=_safe_name_st,
    memory_store=_safe_name_st,
)

# Strategy for WorkspaceConfig instances
_workspace_config_st = st.builds(
    WorkspaceConfig,
    dir=st.text(min_size=1, max_size=30),
)

# Strategy for MemoryStoreConfig instances
_memory_store_config_st = st.builds(
    MemoryStoreConfig,
    description=st.text(min_size=0, max_size=30),
    embedding_provider=st.sampled_from(["", "none", "llama_cpp"]),
)

# Hypothesis strategy for generating valid KiroCrewConfig instances
_agent_config_st = st.builds(
    AgentConfig,
    approval_mode=st.sampled_from(["auto", "interactive"]),
    streaming=st.booleans(),
    model=st.text(min_size=0, max_size=20),
    provider=st.just("acp"),
    default_agent=st.text(min_size=0, max_size=20),
    sandbox=st.sampled_from(["auto", "off"]),
    soft_stop_budget_secs=st.floats(min_value=0.5, max_value=60.0),
)

_session_config_st = st.builds(
    SessionConfig,
    timeout_secs=st.integers(min_value=60, max_value=7200),
)

_memory_config_st = st.builds(
    MemoryConfig,
    embedding_provider=st.sampled_from(["llama_cpp"]),
    embedding_dim=st.sampled_from([256, 512, 1024]),
    semantic_confidence_threshold=st.floats(min_value=0.0, max_value=1.0),
    episodic_dedup_threshold=st.floats(min_value=0.0, max_value=1.0),
    episodic_max_results=st.integers(min_value=1, max_value=50),
    episodic_max_count=st.integers(min_value=100, max_value=50000),
    semantic_keys=st.just([]),
    history_idle_hours=st.floats(min_value=0.5, max_value=24.0),
    history_max_days=st.integers(min_value=1, max_value=365),
    migrated=st.booleans(),
)

_slack_config_st = st.builds(
    SlackConfig,
    allowed_users=st.just([]),
    tracking_channels=st.just([]),
    open_channels=st.lists(st.from_regex(r"C[A-Z0-9]{8,10}", fullmatch=True), max_size=5),
    command=st.text(min_size=1, max_size=20),
    reactions_enabled=st.booleans(),
    show_thinking=st.booleans(),
)

_dashboard_config_st = st.builds(
    DashboardConfig,
    url=st.text(min_size=0, max_size=50),
)

_kirocrew_config_st = st.builds(
    KiroCrewConfig,
    agent=_agent_config_st,
    session=_session_config_st,
    memory=_memory_config_st,
    slack=_slack_config_st,
    dashboard=_dashboard_config_st,
    hooks=st.just({}),
    agents=st.dictionaries(
        keys=_safe_name_st,
        values=_kirocrew_agent_config_st,
        min_size=0,
        max_size=3,
    ),
    default_agent=st.one_of(st.just(""), _safe_name_st),
    workspaces=st.dictionaries(
        keys=_safe_name_st,
        values=_workspace_config_st,
        min_size=0,
        max_size=3,
    ),
    default_workspace=st.text(min_size=1, max_size=20),
    memory_stores=st.dictionaries(
        keys=_safe_name_st,
        values=_memory_store_config_st,
        min_size=0,
        max_size=3,
    ),
    default_memory_store=st.one_of(st.just("default"), _safe_name_st),
    auto_update=st.booleans(),
)
