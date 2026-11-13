# Built-in apps package.

BUILTIN_NAMES: list[str] = [
    "auto_improvement",
    "auto_research",
    "aws_control",
    "code_review_sage",
    "crew_companion",
    "design_critique",
    # Spawned backend AND in-gateway routes: the live-target cutover must run in the
    # gateway process (gateway_routes.py), so the package exports register_routes.
    "dev_fleet",
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
    # The auto-triage pipeline is not an app: it is one of Issue Radar's
    # dashboards. Dropping it from BUILTIN_NAMES stops it being REGISTERED, but an
    # install that already has it keeps the directory and its installed.json
    # entry -- leaving an App Store card for an app with no manifest behind it,
    # present enough to show and not present enough to open. Both spellings are
    # listed because the installed directory name is hyphenated while the Python
    # module was not, and an install can carry either.
    "auto-triage-pipeline",
    "auto_triage_pipeline",
    # The Command Bar is the dashboard's own quick-search launcher, in
    # ``website/src/components/commandBar/``. Its app record carried the one
    # thing the feature kept per install, whether it was on, so that moves to the
    # setting named in _FOLDED_BUILTIN_SWITCHES before the record is retired.
    "command-bar",
    # Task Runner is a core page (``website/src/pages/ProjectsPage.tsx`` over the
    # host's ``/api/taskrunner``). Its app record carried the one thing the feature
    # kept per install, whether it was on, so that moves to the setting named in
    # _FOLDED_BUILTIN_SWITCHES before the record is retired.
    "projects",
]

# A migrated built-in whose enabled state was its feature's switch, mapped to the
# dotted ``config.json`` path that is the switch now. The boot cleanup writes a
# record's opt-out there before retiring the record (``apps/builtin_fold.py``).
_FOLDED_BUILTIN_SWITCHES: dict[str, str] = {
    "command-bar": "dashboard.command_bar_enabled",
    "projects": "taskrunner.enabled",
}

# The folded built-ins that reached existing installs as a default-on PROMOTION
# (``manager_builtins._DEFAULT_ON_BACKFILL`` before they folded). A record of one
# still owed that promotion reads ``enabled: false`` without its user having chosen
# it; any other folded built-in's ``enabled: false`` is the user's own opt-out.
_FOLDED_PROMOTED_BUILTINS: frozenset[str] = frozenset({"command-bar"})
