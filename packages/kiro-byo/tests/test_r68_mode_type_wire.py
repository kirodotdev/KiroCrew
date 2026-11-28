"""R68: preserve the product Mode/Type/Model UX over real ACP frames."""
from scripts.crew_byo_acceptance import smoke_mode_type


def test_mode_type_model_acp_child_isolated_smoke():
    report = smoke_mode_type()
    assert report["status"] == "pass", report
    assert report["isolatedHome"] and not report["liveProviderRequests"]
    assert [row["mode"] for row in report["modes"]] == ["tiga", "dyna"]
    for row in report["modes"]:
        assert len(row["checks"]) >= 14
        assert all(row["checks"].values())
