"""Tests for validate_ask_user_question schema validation."""

from __future__ import annotations

import pytest

from kiro_crew.validation import ValidationError, validate_ask_user_question


class TestValidateAskUserQuestion:
    """Tests for the AskUserQuestion input schema validator."""

    def _valid_input(self, **overrides):
        base = {
            "questions": [
                {
                    "question": "What is your favorite color?",
                    "header": "Preference",
                    "options": [
                        {"label": "Red", "description": "A warm color"},
                        {"label": "Blue", "description": "A cool color"},
                    ],
                    "multiSelect": False,
                }
            ]
        }
        base.update(overrides)
        return base

    def test_valid_single_question(self):
        result = validate_ask_user_question(self._valid_input())
        assert len(result) == 1
        assert result[0]["question"] == "What is your favorite color?"
        assert result[0]["header"] == "Preference"
        assert result[0]["multiSelect"] is False
        assert len(result[0]["options"]) == 2
        assert result[0]["options"][0]["label"] == "Red"

    def test_valid_multi_select(self):
        inp = self._valid_input()
        inp["questions"][0]["multiSelect"] = True
        result = validate_ask_user_question(inp)
        assert result[0]["multiSelect"] is True

    def test_rejects_non_dict(self):
        with pytest.raises(ValidationError, match="must be a JSON object"):
            validate_ask_user_question([1, 2, 3])

    def test_rejects_missing_questions(self):
        with pytest.raises(ValidationError, match="must be a non-empty list"):
            validate_ask_user_question({})

    def test_rejects_empty_questions_list(self):
        with pytest.raises(ValidationError, match="must be a non-empty list"):
            validate_ask_user_question({"questions": []})

    def test_rejects_questions_not_list(self):
        with pytest.raises(ValidationError, match="must be a non-empty list"):
            validate_ask_user_question({"questions": "not a list"})

    def test_skips_non_dict_question(self):
        inp = {"questions": ["not a dict", self._valid_input()["questions"][0]]}
        result = validate_ask_user_question(inp)
        assert len(result) == 1

    def test_skips_question_without_text(self):
        inp = {"questions": [{"question": "", "options": [{"label": "A"}, {"label": "B"}]}]}
        with pytest.raises(ValidationError, match="no valid questions"):
            validate_ask_user_question(inp)

    def test_skips_question_without_options(self):
        inp = {"questions": [{"question": "Hello?", "options": []}]}
        with pytest.raises(ValidationError, match="no valid questions"):
            validate_ask_user_question(inp)

    def test_truncates_long_question(self):
        inp = self._valid_input()
        inp["questions"][0]["question"] = "x" * 1000
        result = validate_ask_user_question(inp)
        assert len(result[0]["question"]) == 500

    def test_truncates_long_header(self):
        inp = self._valid_input()
        inp["questions"][0]["header"] = "h" * 100
        result = validate_ask_user_question(inp)
        assert len(result[0]["header"]) == 50

    def test_truncates_long_label(self):
        inp = self._valid_input()
        inp["questions"][0]["options"][0]["label"] = "L" * 500
        result = validate_ask_user_question(inp)
        assert len(result[0]["options"][0]["label"]) == 200

    def test_truncates_long_description(self):
        inp = self._valid_input()
        inp["questions"][0]["options"][0]["description"] = "D" * 1000
        result = validate_ask_user_question(inp)
        assert len(result[0]["options"][0]["description"]) == 500

    def test_max_4_questions(self):
        # Distinct texts: identical texts are rejected (see
        # test_rejects_duplicate_question_text); this asserts the count cap.
        base = self._valid_input()["questions"][0]
        inp = {"questions": [dict(base, question=f"Question {i}?") for i in range(6)]}
        result = validate_ask_user_question(inp)
        assert len(result) == 4

    def test_max_6_options(self):
        inp = self._valid_input()
        inp["questions"][0]["options"] = [{"label": f"Opt{i}", "description": ""} for i in range(10)]
        result = validate_ask_user_question(inp)
        assert len(result[0]["options"]) == 6

    def test_rejects_duplicate_option_labels_normalized(self):
        # Labels are the selection identity and returned answer; descriptions
        # cannot distinguish duplicate labels for the blocked agent.
        inp = self._valid_input()
        inp["questions"][0]["options"] = [
            {"label": "Deploy", "description": "staging"},
            {"label": "  deploy  ", "description": "production"},
        ]
        with pytest.raises(ValidationError, match="duplicate option labels"):
            validate_ask_user_question(inp)

    def test_skips_option_without_label(self):
        inp = self._valid_input()
        inp["questions"][0]["options"].append({"label": "", "description": "empty"})
        result = validate_ask_user_question(inp)
        assert len(result[0]["options"]) == 2

    def test_skips_non_dict_option(self):
        inp = self._valid_input()
        inp["questions"][0]["options"].append("not a dict")
        result = validate_ask_user_question(inp)
        assert len(result[0]["options"]) == 2

    def test_missing_header_defaults_empty(self):
        inp = self._valid_input()
        del inp["questions"][0]["header"]
        result = validate_ask_user_question(inp)
        assert result[0]["header"] == ""

    def test_missing_description_defaults_empty(self):
        inp = self._valid_input()
        inp["questions"][0]["options"][0] = {"label": "Red"}
        result = validate_ask_user_question(inp)
        assert result[0]["options"][0]["description"] == ""

    def test_multiple_questions(self):
        q1 = {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]}
        q2 = {"question": "Q2", "header": "H2", "options": [{"label": "X"}, {"label": "Y"}], "multiSelect": True}
        result = validate_ask_user_question({"questions": [q1, q2]})
        assert len(result) == 2
        assert result[1]["header"] == "H2"
        assert result[1]["multiSelect"] is True

    def test_rejects_duplicate_question_text(self):
        # Answers are keyed by question text end-to-end, so two questions with
        # the same text collapse to one answer map entry — the user answers
        # both but only the last reaches the blocked agent. Reject duplicates.
        q1 = {"question": "Pick one?", "options": [{"label": "A"}, {"label": "B"}]}
        q2 = {"question": "Pick one?", "options": [{"label": "X"}, {"label": "Y"}]}
        with pytest.raises(ValidationError, match="duplicate question text"):
            validate_ask_user_question({"questions": [q1, q2]})

    def test_rejects_duplicate_question_text_normalized(self):
        # Normalization is case- and whitespace-insensitive so trivially
        # different renderings of the same prompt are still caught.
        q1 = {"question": "Pick   one?", "options": [{"label": "A"}, {"label": "B"}]}
        q2 = {"question": "pick one?", "options": [{"label": "X"}, {"label": "Y"}]}
        with pytest.raises(ValidationError, match="duplicate question text"):
            validate_ask_user_question({"questions": [q1, q2]})
