"""Tests for the Zoom AI Companion processing tri-state (W11-A).

Covers completed/pending/not_entitled, the no-200-empty-fake-success rule, the
readiness webhook, and negative fault tests 4 (pending read) and 5 (unentitled
read).
"""

from kiro_crew.connections.vendors.zoom.processing import (
    SummaryState,
    classify_summary_state,
    is_summary_ready_signal,
)


class TestCompleted:
    def test_200_with_content_is_completed(self):
        assert (
            classify_summary_state(http_status=200, summary_present=True) is SummaryState.COMPLETED
        )

    def test_explicit_completed_status_is_completed(self):
        assert (
            classify_summary_state(
                http_status=200, summary_present=True, processing_status="completed"
            )
            is SummaryState.COMPLETED
        )

    def test_completed_status_is_case_insensitive(self):
        assert (
            classify_summary_state(
                http_status=200, summary_present=True, processing_status="Completed"
            )
            is SummaryState.COMPLETED
        )

    def test_completed_status_without_content_falls_to_pending(self):
        # a 'completed' status with no body is contradictory -> PENDING, never a
        # readable summary with nothing in it.
        assert (
            classify_summary_state(
                http_status=200, summary_present=False, processing_status="completed"
            )
            is SummaryState.PENDING
        )


class TestPending:
    def test_pending_read_surfaces_pending_not_fake_success(self):
        # negative fault test 4: a pending read is PENDING, never a fabricated
        # completion.
        assert (
            classify_summary_state(
                http_status=200, summary_present=False, processing_status="processing"
            )
            is SummaryState.PENDING
        )

    def test_200_empty_body_is_pending_never_fake_success(self):
        # the "200-empty fake success" the tri-state exists to forbid.
        assert (
            classify_summary_state(http_status=200, summary_present=False) is SummaryState.PENDING
        )

    def test_404_without_entitlement_signal_is_pending(self):
        assert (
            classify_summary_state(http_status=404, summary_present=False) is SummaryState.PENDING
        )


class TestNotEntitled:
    def test_ai_companion_forbidden_is_not_entitled(self):
        # negative fault test 5: an unentitled read maps to NOT_ENTITLED.
        assert (
            classify_summary_state(http_status=403, summary_present=False, error_code=2314)
            is SummaryState.NOT_ENTITLED
        )

    def test_plan_missing_business_error_is_not_entitled(self):
        # Zoom returns some entitlement errors as 200-coded business errors.
        assert (
            classify_summary_state(http_status=200, summary_present=False, error_code=200)
            is SummaryState.NOT_ENTITLED
        )

    def test_not_entitled_beats_a_present_body(self):
        assert (
            classify_summary_state(http_status=200, summary_present=True, error_code=200)
            is SummaryState.NOT_ENTITLED
        )


class TestTriStateIsNeverCollapsed:
    def test_three_states_are_distinct(self):
        completed = classify_summary_state(http_status=200, summary_present=True)
        pending = classify_summary_state(http_status=200, summary_present=False)
        not_entitled = classify_summary_state(
            http_status=403, summary_present=False, error_code=2314
        )
        assert len({completed, pending, not_entitled}) == 3


class TestReadinessWebhook:
    def test_summary_completed_is_ready_signal(self):
        assert is_summary_ready_signal("meeting.summary_completed") is True

    def test_other_webhook_is_not_ready_signal(self):
        assert is_summary_ready_signal("meeting.started") is False
