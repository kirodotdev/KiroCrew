# Built-in apps package.

BUILTIN_NAMES: list[str] = [
    "auto_improvement",
    "auto_research",
    "code_review_sage",
    "crew_companion",
    "issue_radar",
    "meetings",
    "ops_mission_control",
    "papyrus",
    "mochi",
    "pptx_maker",
    "spec_builder",
]

# Deploy-web lives in the core deploy module, not as a separate builtin.
# Kept as a constant so the startup migration can identify stale installs.
# Include both forms: hyphenated (legacy installed dir name) and underscored
# (Python module name) to handle either naming convention.
_MIGRATED_BUILTINS: list[str] = ["deploy-web", "deploy_web"]
