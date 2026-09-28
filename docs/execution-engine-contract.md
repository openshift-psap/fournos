# Execution engine contract

Fournos supports two modes for running workloads:

1. **Generic mode** (recommended for most users) — your container image has
   zero Fournos awareness. You just write a FournosJob YAML and provide a
   container image. Fournos's built-in `fournos-generic` pipeline handles
   everything.

2. **Custom engine mode** (for advanced use cases like FORGE) — you provide
   your own Tekton Pipeline/Task with a custom `/opt/fournos/entrypoint`
   image. Full control over lifecycle steps.

---

## Mode 1: Generic (zero Fournos code in your project)

### What you provide

1. A **container image** that does your job — just a normal container with a
   normal entrypoint. It reads configuration from plain **environment
   variables**. It has no knowledge of Fournos, FournosJob CRDs, or anything
   Kubernetes-specific (unless your job itself needs to talk to K8s, which is
   your business).

2. A **FournosJob YAML** that references the built-in `fournos-generic`
   pipeline:

```yaml
apiVersion: fournos.dev/v1
kind: FournosJob
metadata:
  generateName: my-benchmark-
spec:
  owner: my-team
  clusterless: true
  exclusive: false
  pipeline: fournos-generic
  executionEngine:
    generic:
      image: quay.io/myteam/my-job:latest
      command: ["python", "run.py"]     # optional — overrides ENTRYPOINT
      args: ["--verbose"]               # optional — overrides CMD
      env:                              # optional — injected as env vars
        MODEL: gpt-4
        RATE: "10"
```

That's it. No Pipeline YAML. No Task YAML. No SDK. No `/opt/fournos/entrypoint`.

### How it works

1. Fournos creates a resolve Job using its built-in **generic runner** image.
   The runner reads `spec.executionEngine.generic.image` from the FournosJob
   and validates it exists. If you set `spec.hardware` or `spec.secretRefs`,
   they're already on the spec — nothing to patch. Resolve exits 0.

2. After Kueue admission, Fournos creates a PipelineRun using the built-in
   `fournos-generic` Pipeline + `fournos-generic-step` Task.

3. The Task step runs the **generic runner** image again. The runner:
   - Fetches the FournosJob spec
   - Reads `spec.executionEngine.generic.{image, command, args, env}`
   - Creates a **child K8s Job** with your container image
   - Waits for it to complete
   - Streams its logs to stdout and saves them as artifacts
   - Cleans up the child Job

4. Your container runs normally — it receives the `env` values from the
   FournosJob as plain environment variables. It does its work and exits.

### Example: guidellm-bench

See `../../guidellm-bench/` — a complete, minimal example. The project
contains:
- `entrypoint.py` — pure domain logic (deploy model, benchmark, cleanup).
  Zero Fournos imports. Reads config from `MODEL`, `RATE`, etc. env vars.
- `Containerfile` — a normal container build. No `/opt/fournos/entrypoint`.
- `manifests/sample-fjob.yaml` — the FournosJob. That's the only Fournos
  file in the entire project.

---

## Mode 2: Custom engine (advanced)

For engines that need multi-step Tekton Pipelines, custom resolve logic, or
tight lifecycle control (like FORGE), you provide your own Pipeline, Task(s),
and a container image implementing the `/opt/fournos/entrypoint` convention.

### The fixed entrypoint path

Custom engine images **must** provide an executable at:

```
/opt/fournos/entrypoint
```

This can be a real script or a symlink. Both the resolve Job
(`config/resolve/resolve_job.yaml`) and your Tekton Task(s) invoke it:

```yaml
command: ["/opt/fournos/entrypoint"]
args: []
```

### Environment variables

| Variable | Always set? | Meaning |
|---|---|---|
| `FJOB_NAME` | yes | `metadata.name` of the FournosJob |
| `FOURNOS_WORKLOAD_NAMESPACE` | yes | Namespace where the FournosJob CR lives |
| `FOURNOS_STEP` | yes | `resolve-fournos-config` during resolve; otherwise the step name from your Pipeline |
| `FOURNOS_CI` | yes | `"true"` — signals non-interactive execution |

Your entrypoint fetches the full FournosJob spec itself:

```bash
oc get "fjob/$FJOB_NAME" -n "$FOURNOS_WORKLOAD_NAMESPACE" -o yaml
```

`spec.executionEngine.<your-engine-name>` is opaque to Fournos — it is
exactly what your users write and exactly what your entrypoint reads.

### Resolve phase (`FOURNOS_STEP=resolve-fournos-config`)

- Exit `0` on success, non-zero on failure.
- Patch `spec.hardware` / `spec.secretRefs` if needed.
- If neither is needed, just validate your config and exit `0`.

### Optional: Fournos Engine SDK

For custom engines that want a decorator-style integration, the SDK
(`fournos.sdk.engine.FournosEngine`) handles all plumbing. See
`fournos/sdk/engine.py`. This is optional — you can also implement
`/opt/fournos/entrypoint` as a plain bash script.
