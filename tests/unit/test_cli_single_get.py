"""get fjobs <name> — specs/fournos-cli.allium rule ListOneFJob.

Single-job get mirrors `oc get fjob <name>`: the named job is shown whoever
owns it — no owner filtering applies.
"""

from __future__ import annotations

from fournos.cli import CommandFailed, FailureReason
from fournos.cli.commands import get_fjob_row
from tests.unit.fakes import FakeCluster, make_fjob, make_invocation


class TestGetOneFJob:
    def test_found_job_returns_single_row(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a", owner="bob")])
        row = get_fjob_row(cluster, make_invocation(), "job-a")
        assert row.shown_name == "job-a"
        # no owner filtering on single-job get, even for someone else's job
        assert row.shown_owner == "bob"

    def test_row_is_scoped_to_resolved_namespace(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a", namespace="other-ns")])
        result = get_fjob_row(cluster, make_invocation(), "job-a")
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.FJOB_NOT_FOUND

    def test_unknown_job_fails_with_name_in_hint(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = get_fjob_row(cluster, make_invocation(), "nope")
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.FJOB_NOT_FOUND
        assert "nope" in (result.hint or "")
