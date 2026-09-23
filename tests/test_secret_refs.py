"""End-to-end tests — secretRef resolution -> PipelineRun.

SecretRefs live on the FournosJob spec and are populated by the execution
engine during the Resolving phase.

The test uses a noop resolve Job to avoid races with the mock resolver,
and supplies ``secretRefs`` directly in the FournosJob spec.
"""

from __future__ import annotations

from tests.conftest import (
    create_job,
    create_noop_resolve_job,
    job_status_summary,
    poll_phase,
)


def test_missing_secret_ref_fails(k8s):
    """A secretRef with no matching labelled Secret fails the job.

    A noop resolve Job is pre-created so the mock resolver doesn't
    inject valid secretRefs.  The FournosJob carries a nonexistent ref
    directly in its spec.
    """
    create_noop_resolve_job("test-missing-ref")

    create_job(
        k8s,
        "test-missing-ref",
        {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "secretRefs": ["nonexistent-vault-entry"],
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-missing-ref",
        terminal={"Failed"},
        message_substring="not found in namespace",
        timeout=60,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-missing-ref")
