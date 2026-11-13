"""The recall bound drops low-ranked rows before it drops the lessons block."""

from kiro_crew.memory_recall import bound_recall_payload

LESSON = "[lesson] In wiki markup a bare double dash starts struck-out text.\n"


def _payload(fact_count: int, *, lessons: str = LESSON) -> dict:
    facts = [
        {
            "id": f"history-{index}",
            "snippet": f"daily history snippet {index} " + "x" * 240,
            "recall_relevance": 0.5,
        }
        for index in range(fact_count)
    ]
    return {
        "retrieval": {"facts": facts, "episodes": []},
        "semantic_context": "",
        "episodic_context": "",
        "lessons_context": lessons,
    }


def test_low_relevance_history_rows_go_before_the_lessons_block():
    result = bound_recall_payload(_payload(12), context_cap=1200)
    assert result["total_chars"] <= 1200
    assert result["lessons_context"] == LESSON
    assert "lessons_omitted_for_payload_budget" not in result["retrieval"]
    assert result["retrieval"]["facts"]
    assert result["retrieval"]["omitted_for_payload_budget"] > 0


def test_the_lessons_block_goes_before_the_last_row_is_cut_to_nothing():
    lessons = "[lesson] " + "y" * 900 + "\n"
    result = bound_recall_payload(_payload(3, lessons=lessons), context_cap=700)
    assert result["total_chars"] <= 700
    assert result["lessons_context"] == ""
    assert result["retrieval"]["lessons_omitted_for_payload_budget"] is True
    [kept] = result["retrieval"]["facts"]
    assert len(kept["snippet"]) >= 128


def test_a_lone_row_without_lessons_still_shrinks_below_the_floor():
    result = bound_recall_payload(_payload(1, lessons=""), context_cap=100)
    assert result["total_chars"] <= 100
    [kept] = result["retrieval"]["facts"]
    assert kept["snippet_truncated"] is True
