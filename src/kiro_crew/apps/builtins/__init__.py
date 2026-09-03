# Built-in apps package.

BUILTIN_NAMES: list[str] = [
    "auto_improvement",
    "auto_research",
    "aws_control",
    "code_review_sage",
    "crew_companion",
    "design_critique",
    "issue_radar",
    "meetings",
    "ops_mission_control",
    "papyrus",
    "mochi",
    "personal_shopper",
    "pptx_maker",
    "spec_builder",
]

# Deploy-web lives in the core deploy module, not as a separate builtin.
# Kept as a constant so the startup migration can identify stale installs.
# Include both forms: hyphenated (legacy installed dir name) and underscored
# (Python module name) to handle either naming convention.
_MIGRATED_BUILTINS: list[str] = [
    "deploy-web",
    "deploy_web",
    # The auto-triage pipeline is no longer an app: it is one of Issue Radar's
    # dashboards. Dropping it from BUILTIN_NAMES stops it being REGISTERED, but an
    # install that already has it keeps the directory and its installed.json
    # entry -- leaving an App Store card for an app with no manifest behind it,
    # present enough to show and not present enough to open. Both spellings are
    # listed because the installed directory name is hyphenated while the Python
    # module was not, and an install can carry either.
    "auto-triage-pipeline",
    "auto_triage_pipeline",
]
