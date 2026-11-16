"""logs fjob/<name> — specs/fournos-cli.allium rule StreamJobLogs and the
LogStreamPodBelongsToJob invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fournos.cli import CommandFailed, FailureReason, LogStream, PodPhase
from fournos.cli.commands import open_log_stream
from tests.unit.fakes import FakeCluster, make_fjob, make_invocation, make_pod


def _started(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


class TestStreamJobLogs:
    def test_single_running_pod_streams_it(self):
        pod = make_pod("job-a-pod", job_name="job-a", started_at=_started(30))
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=[pod])
        stream = open_log_stream(cluster, make_invocation(), "job-a")
        assert isinstance(stream, LogStream)
        assert stream.pod is pod
        assert stream.offered_pods == [pod]
        assert stream.follow is False

    def test_multiple_running_pods_offer_newest_first_and_stream_first(self):
        old = make_pod("job-a-step1", job_name="job-a", started_at=_started(60))
        new = make_pod("job-a-step2", job_name="job-a", started_at=_started(5))
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=[old, new])
        stream = open_log_stream(cluster, make_invocation(), "job-a")
        assert stream.offered_pods == [new, old]
        assert stream.pod is new

    def test_terminated_pods_are_not_offered(self):
        running = make_pod("job-a-live", job_name="job-a", started_at=_started(5))
        done = make_pod(
            "job-a-done",
            job_name="job-a",
            status=PodPhase.SUCCEEDED,
            started_at=_started(60),
        )
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=[running, done])
        stream = open_log_stream(cluster, make_invocation(), "job-a")
        assert stream.offered_pods == [running]

    def test_pod_name_selects_named_pod(self):
        old = make_pod("job-a-step1", job_name="job-a", started_at=_started(60))
        new = make_pod("job-a-step2", job_name="job-a", started_at=_started(5))
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=[old, new])
        stream = open_log_stream(
            cluster, make_invocation(), "job-a", pod_name="job-a-step1"
        )
        assert stream.pod is old

    def test_unknown_pod_name_fails(self):
        pod = make_pod("job-a-pod", job_name="job-a")
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=[pod])
        result = open_log_stream(
            cluster, make_invocation(), "job-a", pod_name="no-such-pod"
        )
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.POD_NOT_FOUND
        assert "no-such-pod" in (result.hint or "")

    def test_no_running_pod_fails_with_dashboard_hint(self):
        pod = make_pod("job-a-done", job_name="job-a", status=PodPhase.SUCCEEDED)
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=[pod])
        result = open_log_stream(cluster, make_invocation(), "job-a")
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.NO_RUNNING_POD
        assert result.job is not None and result.job.name == "job-a"
        assert result.hint == "https://tekton/job-a"

    def test_unknown_fjob_fails_with_name_and_namespace_hint(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = open_log_stream(cluster, make_invocation(), "no-such-job")
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.FJOB_NOT_FOUND
        assert "no-such-job" in (result.hint or "")
        assert "ns" in (result.hint or "")

    def test_follow_flag_is_carried_on_stream(self):
        pod = make_pod("job-a-pod", job_name="job-a")
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=[pod])
        stream = open_log_stream(cluster, make_invocation(), "job-a", follow=True)
        assert stream.follow is True

    def test_invariant_streamed_pod_belongs_to_job_and_is_offered(self):
        pods = [
            make_pod("job-a-step1", job_name="job-a", started_at=_started(60)),
            make_pod("job-a-step2", job_name="job-a", started_at=_started(5)),
        ]
        cluster = FakeCluster(fjobs=[make_fjob("job-a")], pods=pods)
        stream = open_log_stream(cluster, make_invocation(), "job-a")
        # LogStreamPodBelongsToJob: pod.job_name = stream.job.name and pod in offered
        assert stream.pod.job_name == stream.job.name
        assert stream.pod in stream.offered_pods
