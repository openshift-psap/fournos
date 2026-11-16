"""stop / terminate / delete — specs/fournos-cli.allium rules
RequestJobShutdown, ApplyConfirmedShutdown, RequestJobDeletion,
ApplyConfirmedDeletion, and the ConfirmationPrompt contract.
"""

from __future__ import annotations

import pytest

from fournos.cli import (
    CommandFailed,
    ConfirmationAction,
    ConfirmationPrompt,
    DeletionApplied,
    FailureReason,
    FJob,
    Shutdown,
)
from fournos.cli.commands import (
    apply_confirmation,
    apply_deletion_confirmation,
    apply_shutdown_confirmation,
    request_deletion,
    request_shutdown,
)
from tests.unit.fakes import FakeCluster, make_fjob, make_invocation


class TestRequestShutdown:
    def test_stop_with_yes_applies_directly(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = request_shutdown(
            cluster, make_invocation(), "job-a", Shutdown.STOP, assume_yes=True
        )
        assert isinstance(result, FJob)
        assert result.shutdown is Shutdown.STOP
        assert cluster.get_fjob("ns", "job-a").shutdown is Shutdown.STOP

    def test_stop_without_yes_prompts(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = request_shutdown(
            cluster, make_invocation(), "job-a", Shutdown.STOP, assume_yes=False
        )
        assert isinstance(result, ConfirmationPrompt)
        assert result.action is ConfirmationAction.SHUTDOWN
        assert result.shutdown_mode is Shutdown.STOP
        # nothing applied yet
        assert cluster.get_fjob("ns", "job-a").shutdown is None

    def test_terminate_with_yes_still_prompts(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = request_shutdown(
            cluster, make_invocation(), "job-a", Shutdown.TERMINATE, assume_yes=True
        )
        assert isinstance(result, ConfirmationPrompt)
        assert result.shutdown_mode is Shutdown.TERMINATE
        assert cluster.get_fjob("ns", "job-a").shutdown is None

    def test_unknown_fjob_fails(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = request_shutdown(cluster, make_invocation(), "nope", Shutdown.STOP)
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.FJOB_NOT_FOUND


class TestConfirmationPrompt:
    def test_warns_when_job_owned_by_someone_else(self):
        prompt = ConfirmationPrompt(
            invocation=make_invocation(),
            job=make_fjob("job-a", owner="bob"),
            action=ConfirmationAction.SHUTDOWN,
            shutdown_mode=Shutdown.STOP,
        )
        assert prompt.warns_foreign_job is True

    def test_no_warning_for_own_job(self):
        prompt = ConfirmationPrompt(
            invocation=make_invocation(),
            job=make_fjob("job-a", owner="alice"),
            action=ConfirmationAction.SHUTDOWN,
            shutdown_mode=Shutdown.STOP,
        )
        assert prompt.warns_foreign_job is False


class TestApplyConfirmation:
    def test_confirmed_shutdown_sets_mode(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        prompt = ConfirmationPrompt(
            invocation=make_invocation(),
            job=cluster.get_fjob("ns", "job-a"),
            action=ConfirmationAction.SHUTDOWN,
            shutdown_mode=Shutdown.TERMINATE,
        )
        result = apply_shutdown_confirmation(cluster, prompt)
        assert result.shutdown is Shutdown.TERMINATE
        assert cluster.get_fjob("ns", "job-a").shutdown is Shutdown.TERMINATE

    def test_confirmed_shutdown_rejects_delete_prompt(self):
        prompt = ConfirmationPrompt(
            invocation=make_invocation(),
            job=make_fjob("job-a"),
            action=ConfirmationAction.DELETE,
        )
        with pytest.raises(ValueError):
            apply_shutdown_confirmation(FakeCluster(), prompt)

    def test_confirmed_deletion_deletes(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        prompt = ConfirmationPrompt(
            invocation=make_invocation(),
            job=cluster.get_fjob("ns", "job-a"),
            action=ConfirmationAction.DELETE,
        )
        result = apply_deletion_confirmation(cluster, prompt)
        assert isinstance(result, DeletionApplied)
        assert not cluster.has_fjob("job-a")

    def test_confirmed_deletion_rejects_shutdown_prompt(self):
        prompt = ConfirmationPrompt(
            invocation=make_invocation(),
            job=make_fjob("job-a"),
            action=ConfirmationAction.SHUTDOWN,
            shutdown_mode=Shutdown.STOP,
        )
        with pytest.raises(ValueError):
            apply_deletion_confirmation(FakeCluster(), prompt)

    def test_apply_confirmation_dispatches_on_action(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        prompt = ConfirmationPrompt(
            invocation=make_invocation(),
            job=cluster.get_fjob("ns", "job-a"),
            action=ConfirmationAction.DELETE,
        )
        result = apply_confirmation(cluster, prompt)
        assert isinstance(result, DeletionApplied)


class TestRequestDeletion:
    def test_delete_with_yes_applies_directly(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = request_deletion(cluster, make_invocation(), "job-a", assume_yes=True)
        assert isinstance(result, DeletionApplied)
        assert not cluster.has_fjob("job-a")

    def test_delete_without_yes_prompts(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = request_deletion(cluster, make_invocation(), "job-a", assume_yes=False)
        assert isinstance(result, ConfirmationPrompt)
        assert result.action is ConfirmationAction.DELETE
        assert result.shutdown_mode is None
        assert cluster.has_fjob("job-a")

    def test_unknown_fjob_fails(self):
        cluster = FakeCluster(fjobs=[make_fjob("job-a")])
        result = request_deletion(cluster, make_invocation(), "nope")
        assert isinstance(result, CommandFailed)
        assert result.reason is FailureReason.FJOB_NOT_FOUND
