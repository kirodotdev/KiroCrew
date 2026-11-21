"""R32: test ACP link exchange on real BYO child, never claim Crew host E2E."""
from scripts.crew_byo_acceptance import smoke_work_link


def test_isolated_work_link_round_trip():
    result = smoke_work_link()
    assert result["status"] == "pass", result
    assert result["isolatedHome"] is True
    assert len(result["checks"]) == 7
    assert all(result["checks"].values())
