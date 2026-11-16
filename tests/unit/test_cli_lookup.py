"""get fjobs / get pods — specs/fournos-cli.allium rules ListAllFJobs,
ListFJobsByOwner, ListAllExecutionPods, ListExecutionPodsFiltered, and the
FournosCli surface exposure contract (row columns).

Mine is the default scope: rows are filtered by the resolved owner unless
--all opts out; --owner always filters, whoever it names.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fournos.cli import CommandFailed, FailureReason, JobPhase, PodPhase
from fournos.cli.commands import list_fjob_rows, list_pod_rows
from tests.unit.fakes import FakeCluster, make_fjob, make_invocation, make_pod


class TestListFJobs:
    def test_default_scope_is_mine(self):
        cluster = FakeCluster(
            fjobs=[
                make_fjob("mine-1", owner="alice"),
                make_fjob("bobs-1", owner="bob"),
                make_fjob("no-owner", owner=None),
            ]
        )
        rows = list_fjob_rows(cluster, make_invocation())
        assert [r.shown_name for r in rows] == ["mine-1"]

    def test_all_lists_every_job_in_namespace(self):
        cluster = FakeCluster(
            fjobs=[
                make_fjob("mine-1"),
                make_fjob("bobs-1", owner="bob"),
                make_fjob("elsewhere", namespace="other-ns"),
            ]
        )
        rows = list_fjob_rows(cluster, make_invocation(), all_jobs=True)
        assert sorted(r.shown_name for r in rows) == ["bobs-1", "mine-1"]

    def test_owner_filter_selects_that_owner(self):
        cluster = FakeCluster(
            fjobs=[make_fjob("mine-1"), make_fjob("bobs-1", owner="bob")]
        )
        rows = list_fjob_rows(cluster, make_invocation(), owner_filter="bob")
        assert [r.shown_name for r in rows] == ["bobs-1"]

    def test_owner_filter_needs_no_identity(self):
        cluster = FakeCluster(
            fjobs=[make_fjob("bobs-1", owner="bob"), make_fjob("mine-1")]
        )
        rows = list_fjob_rows(
            cluster, make_invocation(system_user=None), owner_filter="bob"
        )
        assert [r.shown_name for r in rows] == ["bobs-1"]

    def test_default_scope_with_unresolvable_owner_fails(self):
        cluster = FakeCluster(fjobs=[make_fjob("mine-1")])
        result = list_fjob_rows(cluster, make_invocation(system_user=None))
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.OWNER_UNRESOLVED
        assert "--owner" in (result.hint or "")
        assert "--all" in (result.hint or "")

    def test_all_scope_needs_no_identity(self):
        cluster = FakeCluster(fjobs=[make_fjob("bobs-1", owner="bob")])
        rows = list_fjob_rows(cluster, make_invocation(system_user=None), all_jobs=True)
        assert [r.shown_name for r in rows] == ["bobs-1"]

    def test_row_exposes_spec_columns(self):
        created = datetime.now(UTC) - timedelta(hours=2)
        cluster = FakeCluster(
            fjobs=[make_fjob("mine-1", created_at=created, phase=JobPhase.PENDING)]
        )
        (row,) = list_fjob_rows(cluster, make_invocation())
        assert row.shown_name == "mine-1"
        assert row.shown_owner == "alice"
        assert row.shown_phase is JobPhase.PENDING
        assert row.shown_cluster == "cluster-1"
        # derived age: about two hours, allow scheduling slack
        assert timedelta(hours=1.9) < row.shown_age < timedelta(hours=2.1)


class TestListPods:
    def test_default_scope_is_mine(self):
        cluster = FakeCluster(
            pods=[
                make_pod("mine-pod", job_name="job-a", owner_label="alice"),
                make_pod("bobs-pod", job_name="job-b", owner_label="bob"),
                make_pod("legacy-pod", job_name="job-c", owner_label=None),
                make_pod("other-ns-pod", namespace="other-ns"),
            ]
        )
        rows = list_pod_rows(cluster, make_invocation())
        assert [r.shown_name for r in rows] == ["mine-pod"]
        assert cluster.pod_call() == ("ns", None)

    def test_all_lists_every_pod_in_namespace(self):
        cluster = FakeCluster(
            pods=[
                make_pod("job-a-pod", job_name="job-a"),
                make_pod("other-ns-pod", namespace="other-ns"),
            ]
        )
        rows = list_pod_rows(cluster, make_invocation(), all_jobs=True)
        assert [r.shown_name for r in rows] == ["job-a-pod"]
        assert cluster.pod_call() == ("ns", None)

    def test_fjob_argument_scopes_by_job_name_label(self):
        cluster = FakeCluster(
            pods=[
                make_pod("job-a-pod", job_name="job-a"),
                make_pod("job-b-pod", job_name="job-b"),
            ]
        )
        rows = list_pod_rows(
            cluster, make_invocation(), fjob_name="job-a", all_jobs=True
        )
        assert [r.shown_name for r in rows] == ["job-a-pod"]
        assert cluster.pod_call() == ("ns", "job-a")

    def test_fjob_argument_and_default_mine_intersect(self):
        cluster = FakeCluster(
            pods=[
                make_pod("mine-a", job_name="job-a", owner_label="alice"),
                make_pod("bob-a", job_name="job-a", owner_label="bob"),
            ]
        )
        rows = list_pod_rows(cluster, make_invocation(), fjob_name="job-a")
        assert [r.shown_name for r in rows] == ["mine-a"]
        assert cluster.pod_call() == ("ns", "job-a")

    def test_unlabelled_pod_falls_back_to_fjob_owner(self):
        cluster = FakeCluster(
            fjobs=[make_fjob("job-a", owner="alice")],
            pods=[make_pod("job-a-pod", job_name="job-a", owner_label=None)],
        )
        rows = list_pod_rows(cluster, make_invocation())
        assert [r.shown_name for r in rows] == ["job-a-pod"]
        assert rows[0].shown_owner == "alice"

    def test_unlabelled_pod_of_foreign_job_excluded_from_mine(self):
        cluster = FakeCluster(
            fjobs=[make_fjob("job-b", owner="bob")],
            pods=[make_pod("job-b-pod", job_name="job-b", owner_label=None)],
        )
        assert list_pod_rows(cluster, make_invocation()) == []

    def test_owner_label_wins_over_fjob_owner(self):
        cluster = FakeCluster(
            fjobs=[make_fjob("job-a", owner="bob")],
            pods=[make_pod("job-a-pod", job_name="job-a", owner_label="alice")],
        )
        (row,) = list_pod_rows(cluster, make_invocation())
        assert row.shown_owner == "alice"

    def test_all_shows_fallback_owner(self):
        cluster = FakeCluster(
            fjobs=[make_fjob("job-a", owner="alice")],
            pods=[make_pod("job-a-pod", job_name="job-a", owner_label=None)],
        )
        (row,) = list_pod_rows(cluster, make_invocation(), all_jobs=True)
        assert row.shown_owner == "alice"

    def test_default_scope_with_unresolvable_owner_fails(self):
        cluster = FakeCluster(pods=[make_pod("job-a-pod", job_name="job-a")])
        result = list_pod_rows(cluster, make_invocation(system_user=None))
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.OWNER_UNRESOLVED

    def test_all_scope_with_fjob_needs_no_identity(self):
        cluster = FakeCluster(
            pods=[
                make_pod("job-a-pod", job_name="job-a", owner_label=None),
                make_pod("job-b-pod", job_name="job-b"),
            ]
        )
        rows = list_pod_rows(
            cluster,
            make_invocation(system_user=None),
            fjob_name="job-a",
            all_jobs=True,
        )
        assert [r.shown_name for r in rows] == ["job-a-pod"]

    def test_row_exposes_spec_columns(self):
        started = datetime.now(UTC) - timedelta(minutes=30)
        cluster = FakeCluster(
            pods=[
                make_pod("job-a-pod", job_name="job-a", started_at=started, restarts=2)
            ]
        )
        (row,) = list_pod_rows(cluster, make_invocation())
        assert row.shown_name == "job-a-pod"
        assert row.shown_fjob == "job-a"
        assert row.shown_owner == "alice"
        assert row.shown_status is PodPhase.RUNNING
        assert row.shown_restarts == 2
        assert timedelta(minutes=29) < row.shown_age < timedelta(minutes=31)
