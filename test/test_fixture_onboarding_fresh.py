"""Consumer contract for the onboarding-fresh seeded-home fixture."""

from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.testing.fixtures import seeded_home


def _loaded_onboarding_state() -> tuple[bool, bool]:
    """Report whether config exists and the value the production loader exposes."""
    return config_path().is_file(), KiroCrewConfig.load().dashboard.onboarded


def test_onboarding_fresh_is_explicit_first_run_and_distinct_from_empty() -> None:
    """The fixture pins first-run state instead of relying on loader defaults."""
    with seeded_home("onboarding-fresh") as home:
        assert {path.name for path in home.iterdir()} == {"config.json", "fixture.yaml"}
        onboarding_state = _loaded_onboarding_state()
        assert onboarding_state == (True, False)
        assert not (home / "sessions").exists()
        assert not (home / "crons.json").exists()
        assert not (home / "memory").exists()
        assert not (home / "workspace" / "memory").exists()

    with seeded_home("empty") as home:
        empty_state = _loaded_onboarding_state()
        assert empty_state == (False, False)
        assert {path.name for path in home.iterdir()} == {"fixture.yaml"}

    assert onboarding_state != empty_state
