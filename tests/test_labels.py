import pytest

from fournos.core.constants import ANNOTATION_ORIGINAL_OWNER, LABEL_OWNER
from fournos.core.labels import job_annotations, job_labels, sanitize_label_value


@pytest.fixture(autouse=True)
def _clean_around_test():
    """Override the integration cleanup fixture; this test uses only pure functions."""


class TestSanitizeLabelValue:
    def test_valid_passthrough(self):
        assert sanitize_label_value("alice") == "alice"

    def test_alphanumeric_with_dots_dashes_underscores(self):
        assert sanitize_label_value("my-team_v2.0") == "my-team_v2.0"

    def test_empty_string(self):
        assert sanitize_label_value("") == ""

    def test_replaces_slash(self):
        assert (
            sanitize_label_value("fournos-dashboard/scheduler")
            == "fournos-dashboard_scheduler"
        )

    def test_replaces_at_sign(self):
        assert sanitize_label_value("user@domain.com") == "user_domain.com"

    def test_replaces_spaces(self):
        assert sanitize_label_value("first last") == "first_last"

    def test_strips_leading_non_alphanumeric(self):
        assert sanitize_label_value("--alice") == "alice"
        assert sanitize_label_value("..bob") == "bob"
        assert sanitize_label_value("__carol") == "carol"

    def test_strips_trailing_non_alphanumeric(self):
        assert sanitize_label_value("alice--") == "alice"
        assert sanitize_label_value("bob..") == "bob"

    def test_truncates_to_63_chars(self):
        long_owner = "a" * 100
        result = sanitize_label_value(long_owner)
        assert len(result) == 63
        assert result == "a" * 63

    def test_strips_trailing_after_truncation(self):
        owner = "a" * 62 + ".-" + "b" * 10
        result = sanitize_label_value(owner)
        assert len(result) <= 63
        assert result[-1].isalnum()

    def test_all_invalid_chars_becomes_empty(self):
        assert sanitize_label_value("@@@") == ""

    def test_scheduler_default(self):
        result = sanitize_label_value("fournos-dashboard/scheduler")
        assert result == "fournos-dashboard_scheduler"
        assert len(result) <= 63
        assert result[0].isalnum()
        assert result[-1].isalnum()


class TestJobLabels:
    def test_no_owner(self):
        assert job_labels({}) == {}
        assert job_labels({"owner": ""}) == {}

    def test_valid_owner(self):
        assert job_labels({"owner": "alice"}) == {LABEL_OWNER: "alice"}

    def test_scheduler_default(self):
        labels = job_labels({"owner": "fournos-dashboard/scheduler"})
        assert labels == {LABEL_OWNER: "fournos-dashboard_scheduler"}

    def test_owner_longer_than_63_chars(self):
        long_owner = "a" * 100
        labels = job_labels({"owner": long_owner})
        assert LABEL_OWNER in labels
        assert len(labels[LABEL_OWNER]) == 63

    def test_all_invalid_owner_produces_no_label(self):
        assert job_labels({"owner": "@@@"}) == {}


class TestJobAnnotations:
    def test_no_owner(self):
        assert job_annotations({}) == {}

    def test_valid_owner_no_annotation(self):
        assert job_annotations({"owner": "alice"}) == {}

    def test_sanitized_owner_gets_annotation(self):
        annotations = job_annotations({"owner": "fournos-dashboard/scheduler"})
        assert annotations == {
            ANNOTATION_ORIGINAL_OWNER: "fournos-dashboard/scheduler",
        }

    def test_long_owner_gets_annotation(self):
        long_owner = "a" * 100
        annotations = job_annotations({"owner": long_owner})
        assert annotations == {ANNOTATION_ORIGINAL_OWNER: long_owner}
