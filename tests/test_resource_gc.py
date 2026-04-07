"""Resource GC tests — verify that stale Workloads and PipelineRuns
(whose parent FournosJob no longer exists) are collected by the
background GC loop.

These tests require the operator to be started with a short GC interval,
e.g. ``FOURNOS_GC_INTERVAL_SEC=5``.
"""

from tests.conftest import (
    create_stale_pipelinerun,
    create_stale_workload,
    pipelinerun_exists,
    poll_resource_gone,
    workload_exists,
)


def test_stale_workload_collected(k8s):
    """A fournos-labeled Workload with no parent FournosJob is deleted by GC."""
    create_stale_workload(k8s, "stale-wl")
    assert workload_exists("stale-wl"), (
        "Stale Workload fournos-stale-wl should exist after creation"
    )

    poll_resource_gone(workload_exists, "stale-wl", timeout=60)


def test_stale_pipelinerun_collected(k8s):
    """A fournos-labeled PipelineRun with no parent FournosJob is deleted by GC."""
    create_stale_pipelinerun(k8s, "stale-pr")
    assert pipelinerun_exists("stale-pr"), (
        "Stale PipelineRun fournos-stale-pr should exist after creation"
    )

    poll_resource_gone(pipelinerun_exists, "stale-pr", timeout=60)
