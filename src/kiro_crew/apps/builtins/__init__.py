# Built-in apps package.

BUILTIN_NAMES: list[str] = [
    "auto_research",
    "code_review_sage",
    "issue_radar",
    "meetings",
    "papyrus",
    "mochi",
    "pptx_maker",
]

# Deploy-web lives in the core deploy module, not as a separate builtin.
# Kept as a constant so the startup migration can identify stale installs.
# Include both forms: hyphenated (legacy installed dir name) and underscored
# (Python module name) to handle either naming convention.
_MIGRATED_BUILTINS: list[str] = ["deploy-web", "deploy_web"]
