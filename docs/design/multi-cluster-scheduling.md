# Multi-Cluster Scheduling and Lifecycle Specification

This document details the operational model, scheduling invariants, failure resilience, and preemption handoff algorithms governing multi-cluster admission across Fournos and Hearth. Quota and eviction semantics follow Kueue v0.16.4, where an eviction sets an `Evicted` condition and the victim's controller must release the reservation explicitly.

---

## 1. Overview and Core Invariants

In distributed performance engineering of Large Language Model (LLM) serving stacks (e.g., vLLM, SGLang, TensorRT-LLM), benchmarks represent multi-hour experiments that are highly vulnerable to hardware co-tenancy and resource contention. Simultaneous execution of uncoordinated benchmarks on shared accelerators distorts latency metrics—such as Time to First Token (TTFT) and Inter-Token Latency (ITL)—due to memory bandwidth and PCIe/NVLink thrashing.

To ensure benchmark validity without mandating custom scheduler daemons or admission webhooks on worker clusters (**Zero Worker Footprint**), Fournos and Hearth decouple centralized admission control on a management cluster from remote containerized execution driven via Tekton.

The architectural specification defines three primary operational invariants (with their current enforcement boundaries and dependencies detailed in Section 4):
1. **Dual-Contract Exclusivity**: A cluster is admitted to either a single exclusive workload (locking the entire hardware slot pool) or multiple fractional shared workloads bounded by aggregate physical GPU capacity.
2. **Quarantine-and-Reroute Resilience**: If a target cluster becomes unreachable, its quota is dynamically zeroed in Kueue, and pending pool-scoped workloads are automatically rerouted to healthy sibling clusters without dropping jobs.
3. **Fail-Closed Preemption Handoff**: When a running virtual workload is evicted by Kueue to admit a higher-priority job, the active Tekton execution is gracefully canceled (`CancelledRunFinally`), and the quota reservation is held until target-side cleanup tasks verify accelerator deallocation (`preemption-cleanup-verified`).

---

## 2. Formal Workload Algebra

### 2.1 Benchmark Workload Definition

A benchmark workload submitted to Fournos is defined as a 7-tuple:

$$j = \langle \mathcal{M}, \theta, \mathcal{E}, \pi, \mathcal{W}, \mathcal{P}, x \rangle$$

Where:
* $\mathcal{M}$: Model architecture and weights identifier (e.g., `Llama-3-70B-Instruct`, `Mistral-Large-2407`).
* $\theta$: Model parameter scale in billions (e.g., $70\text{B}$).
* $\mathcal{E}$: Inference serving engine runtime and release tag (e.g., `vLLM v0.22.0`, `SGLang v0.4.1`, `TensorRT-LLM v0.12.0`).
* $\pi = \langle \pi_{\text{TP}}, \pi_{\text{PP}} \rangle$: Parallelism degrees, where $\pi_{\text{TP}}$ is the tensor parallel degree and $\pi_{\text{PP}}$ is the pipeline parallel degree.
* $\mathcal{W} = \langle s^{\text{in}}, s^{\text{out}}, \lambda, \mathcal{D} \rangle$: Workload traffic specification denoting prompt token length $s^{\text{in}}$, generation token length $s^{\text{out}}$, client arrival request rate $\lambda$, and traffic distribution $\mathcal{D}$ (constant, Poisson, or bursty).
* $\mathcal{P}$: Placement constraint, either a named cluster $c \in \mathcal{C}$ or a fungible GPU pool identifier $P_p$.
* $x \in \{\text{shared}, \text{exclusive}\}$: Hardware isolation contract mode.

### 2.2 Pre-Admission Demand Derivation

Standard Kubernetes schedulers evaluate resource demand declared in static container specifications. In inference benchmarking, required accelerator counts depend dynamically on model weights, quantization format, and tensor parallelism.

Prior to queueing, Fournos executes a deterministic demand mapping:

$$\text{Resolve}(j) \mapsto \langle g, r_{j,g}, s_j \rangle$$

Where:
* $g = \text{MapTarget}(\mathcal{M}, \theta, \mathcal{P})$ is the derived accelerator resource flavor key (e.g., `fournos/gpu-h200`, `fournos/gpu-b200`, `fournos/gpu-mi300x`).
* $r_{j,g} = \pi_{\text{TP}} \times \pi_{\text{PP}}$ is the derived aggregate physical GPU request.
* $s_j$ is the synthetic slot demand:
  $$s_j = \begin{cases} 100, & \text{if } x = \text{exclusive} \\ 1, & \text{if } x = \text{shared} \end{cases}$$

### 2.3 Multi-Cluster Fleet State & Dynamic Quota Modeling

Let $\mathcal{C} = \{C_1, \dots, C_N\}$ denote the managed fleet of remote target GPU clusters. For each cluster $c \in \mathcal{C}$ and accelerator flavor $g$, Hearth probes physical allocatable GPU capacity $K_{c,g}(t)$ at time $t$ using read-only API credentials.

Hearth monitors cluster health via periodic active probes with a per-probe timeout of $10\,\text{s}$, executed at the cluster's configured discovery interval. Any failed discovery — network timeout, API error, or rejected credential — increments the failure counter. Let $f_c(t)$ denote the count of consecutive probe failures at time $t$. The dynamic health indicator $H_c(t)$ with a failure threshold $\theta_{\text{fail}} = 5$ (a $50\,\text{s}$ detection window at a $10\,\text{s}$ probe cadence) is:

$$H_c(t) = \mathbf{1}(f_c(t) < \theta_{\text{fail}})$$

Active GPU admission quota $Q_{c,g}(t)$ and synthetic slot capacity $S_c(t)$ exposed to the central Kueue `ClusterQueue` are:

$$Q_{c,g}(t) = K_{c,g}(t) \cdot H_c(t), \qquad S_c(t) = 100 \cdot H_c(t)$$

Upon health-check failure ($H_c(t) = 0$), both the accelerator quota and slot capacity are immediately clamped to 0 ($Q_{c,g} \to 0, S_c \to 0$).

### 2.4 Dual-Contract Admissibility Condition

Let $A_c(t)$ denote the set of currently admitted workloads actively running on cluster $c$ at time $t$. Kueue admits a pending workload $j$ to cluster $c$ if and only if both physical accelerator capacity and slot exclusivity constraints hold:

$$r_{j,g} + \sum_{k \in A_c(t)} r_{k,g} \le Q_{c,g}(t) \quad (\forall g)$$

$$s_j + \sum_{k \in A_c(t)} s_k \le S_c(t) \le 100$$

**Consequences of the Invariant:**
* **Exclusive Admission ($x = \text{exclusive}$)**: Requires $s_j = 100$. This can be admitted if and only if $\sum_{k \in A_c(t)} s_k = 0$ (the cluster has zero active workloads). Once admitted, the cluster slot capacity is fully saturated ($100/100$), blocking all subsequent admissions.
* **Shared Admission ($x = \text{shared}$)**: Requires $s_j = 1$. Up to 100 shared fractional workloads may co-exist, provided their cumulative GPU requests do not exceed $Q_{c,g}(t)$.
* **Mutual Blocking**: An exclusive job cannot be admitted if even one shared job is active ($s = 1 \implies$ available slots $\le 99 < 100$).

### 2.5 Fungible GPU Pool Targets and Failover Redirection

A GPU pool $P_p(g)$ for accelerator type $g$ defines a set of fungible clusters configured with identical hardware capabilities:

$$P_p(g) = \{ c \in \mathcal{C} \mid \text{nodeLabels}(c)[\texttt{fournos.dev/gpu-pool-}g] = p \land H_c(t) = 1 \}$$

Kueue admits a pool workload atomically to exactly one member $c^* \in P_p(g)$ with sufficient capacity; quota is never split across clusters.

When an outage occurs on cluster $c$ ($H_c(t) = 0$):
* Pending pool-scoped jobs are admitted to surviving healthy pool members automatically by Kueue without controller intervention.
* For pending jobs explicitly pinned to cluster $c$ with a non-zero accelerator request ($r_{j,g} > 0$), Fournos clears the cluster pin on the pending virtual `Workload`, allowing Kueue to re-admit the workload across any compatible cluster with sufficient quota:
  $$\text{Redirect}(j, c) \mapsto c' \in \mathcal{C} \setminus \{c\} \quad \text{s.t.} \quad Q_{c',g}(t) - \sum_{k \in A_{c'}(t)} r_{k,g} \ge r_{j,g}$$
  Redirection is bounded by a per-job redistribution budget $\theta_{\text{redist}} = 3$, and a job is never redirected away from the same source cluster twice: Fournos records the source in `redirectedFrom` and suppresses a repeat redirect from it. The counter is not reset when a cluster recovers, so a long campaign consumes it. Once the budget is exhausted, the job stays queued on its original pin and surfaces through the normal pending-reason conditions rather than failing. Lock-only jobs (exclusive slot without an accelerator request) have no target-agnostic equivalent and remain queued until the pinned cluster recovers.
  Because Kueue disallows in-place mutation of `podSets` on active Workload objects, Fournos safely recreates or updates the pending unadmitted virtual Workload's flavor requirements without perturbing active quota reservations of running jobs.

---

## 3. Forensic Evidence Algebra & Provenance

Upon benchmark execution completion, the harness generates an immutable forensic evidence item $e \in \mathcal{E}$:

$$e = \langle j, c^*, \tau_{\text{exec}}, \mathbf{K}, \mathbf{T}, \pi_e \rangle$$

Where:
* $c^* \in \mathcal{C}$: Confirmed bound execution cluster.
* $\tau_{\text{exec}} = [t_{\text{start}}, t_{\text{end}}]$: Precise wall-clock execution window.
* $\mathbf{K} \in \mathbb{R}^{33}$: Vector of 33 canonical serving KPIs (including TTFT, ITL, throughput $\Theta$, request latency percentiles).
* $\mathbf{T}$: Raw time-series telemetry slice captured via Prometheus/Thanos and DCGM (GPU SM utilization, memory bandwidth, power draw, temperature).
* $\pi_e = \langle \sigma_{\text{git}}, \sigma_{\text{img}}, \nu_{\text{drv}}, \upsilon_{\text{run}} \rangle$: Immutable provenance capturing repository commit SHA, serving container image digest, host GPU driver/ROCm version, and Tekton `PipelineRun` UID.

### Comparability and Performance Regressions

A performance comparison between baseline evidence $e_b$ and candidate/regressed evidence $e_r$ is valid if and only if both share identical execution parameters, isolation contracts, and container image/driver baselines:

$$\text{ValidCompare}(e_b, e_r) \iff (\mathcal{M}_b, \theta_b, \mathcal{E}_b, \pi_b, \mathcal{W}_b, g_b, x_b, \sigma_{\text{img}, b}, \nu_{\text{drv}, b}) = (\mathcal{M}_r, \theta_r, \mathcal{E}_r, \pi_r, \mathcal{W}_r, g_r, x_r, \sigma_{\text{img}, r}, \nu_{\text{drv}, r})$$

The comparison key is the normalized parameter tuple persisted with each evidence item, so the check can be repeated from the artifact bundle alone. Comparisons across disparate isolation contracts ($x_{\text{exclusive}} \neq x_{\text{shared}}$) or drifting container/driver baselines are strictly invalidated.

For valid comparisons, the normalized relative delta for metric $K \in \mathbf{K}$ is defined as:

$$\Delta K(e_b, e_r) = \frac{K(e_r) - K(e_b)}{K(e_b)}$$

* For latency KPIs ($T_{\text{TTFT}}, T_{\text{ITL}}$), $\Delta K > 0$ denotes a regression.
* For throughput KPIs ($\Theta$), $\Delta K < 0$ denotes a regression.
* Across $n$ concurrency load points, aggregate throughput ratios are evaluated via geometric mean:
  $$\rho_G = \exp\left(\frac{1}{n} \sum_{i=1}^n \ln \frac{\Theta_{r,i}}{\Theta_{b,i}}\right)$$

---

## 4. Assumptions and Non-Guarantees

The guarantees in Sections 2 and 3 hold within the following scope. Each item is a known boundary of the current design, not an implementation defect.

* **Quota is virtual, not physical.** Kueue's accounting guarantees mutual exclusion among jobs admitted through the central queue. It does not prevent unmanaged workloads from running directly on worker nodes, so a benchmark can still be disturbed by an actor outside the queue.
* **Demand is trusted, not verified.** The request $r_{j,g}$ is derived from the job specification, but execution never asserts that the deployed service requests the same GPU count. Users may supply `spec.hardware` directly, and a failed derivation records an error while exiting successfully, leaving a slot-only job that Kueue may place on any cluster with a free slot. The divergence surfaces as a deployment failure; the per-run DCGM telemetry retained in the artifact bundle makes it visible after the fact.
* **Credentials are split by purpose.** Discovery uses a read-only service-account kubeconfig. Execution uses a separate credential scoped to the namespaces Forge deploys into, injected into the Tekton task and never stored on the target. Concentrating both on the management cluster is a deliberate trade-off: a compromise there reaches every target cluster.
* **Preemption is bound in one direction.** An eviction cancels the pipeline gracefully, and the reservation is released only after verified cleanup. A worker-side failure that Kueue cannot see releases the reservation only through the pipeline's own failure handling; translating it into an immediate release is future work.
* **Quarantine affects pending work only.** A pipeline already running on a cluster that becomes unreachable runs to completion or network timeout. Quarantine reroutes pending jobs; it does not cancel running ones.
* **Cleanup proof is opt-in.** The fail-closed release gate applies to pipelines that declare the contracted cleanup task and emit `preemption-cleanup-verified`. A run that produces no proof does not pass the gate.
* **Admission is eventually consistent.** Hearth, Fournos, and Kueue reconcile on independent timers, so a quota value read during admission may be up to one discovery interval old. A job admitted in the window between a cluster failing and Hearth zeroing its quota is left to its pipeline's failure handling; quarantine governs subsequent admissions.

---

## 5. Admission, Resilience, and Eviction Reconciliation Algorithm

The following algorithm details the unified control loop spanning Hearth, Fournos, and Kueue across discovery, dynamic quota enforcement, preemption handoff, and evidence capture. It is a conceptual global loop, not an atomic transaction: the three components run as independent controllers, and each phase observes the quota state produced by the most recent Hearth reconcile.

```text
Algorithm: Dynamic Multi-Cluster Admission, Quarantine, and Preemption Handoff
--------------------------------------------------------------------------------
Require:
  Queue of submitted jobs Q
  Target cluster fleet C
  Failure threshold theta_fail = 5
  Redirection threshold theta_redist = 3
  Initial allocatable capacity K_{c,g} = 0 for all c in C, flavor g

Ensure:
  Active admitted runs A
  Dynamic quota state {Q_{c,g}, S_c}
  Immutable evidence store E

1:  // =========================================================================
2:  // Phase 1: Hearth Hardware Discovery & Quarantine Probing
3:  // =========================================================================
4:  for each cluster c in C do
5:      <ok, allocatable> = ProbeClusterAllocatable(c)
6:      if ok then
7:          K_{c,g} = allocatable           // Update to latest successfully observed capacity
8:          f_c = 0
9:      else
10:         f_c = f_c + 1                   // Retain last known K_{c,g} until quarantined
11:     end if
12:     H_c = (f_c < theta_fail) ? 1 : 0
13:     Q_{c,g} = K_{c,g} * H_c              // Active GPU quota clamped to 0 on failure
14:     S_c = 100 * H_c                      // Slot quota clamped to 0 on failure
15:     ReconcileKueueResourceFlavor(c, Q_{c,g}, S_c)
16: end for
17: 
18: // =========================================================================
19: // Phase 2: Fournos Demand Resolution & Outage Redirection
20: // =========================================================================
21: for each job j in Q do
22:     if j.state == "Resolving" then
23:         <g, r_{j,g}, s_j> = ResolveDemand(j)
24:         W_j = SynthesizeVirtualWorkload(j, g, r_{j,g}, s_j)
25:         j.state = "Pending"
26:     end if
27: 
28:     // Outage failover: redirect pending, pinned work if its target is quarantined
29:     if j.pinned != NULL and j.redirectedFrom != j.pinned
30:        and j.state == "Pending" and H_{j.pinned} == 0
31:        and r_{j,g} > 0 and j.reroutes < theta_redist then
32:         UnpinTargetFlavor(W_j)           // Allows Kueue to re-admit across healthy flavor
33:         j.redirectedFrom = j.pinned      // Do not redirect away from the same source twice
34:         j.reroutes = j.reroutes + 1
35:     end if
36: end for
37: 
38: // =========================================================================
39: // Phase 3: Kueue Dual-Contract Admission & Dispatch
40: // =========================================================================
41: for each virtual workload W_j in Q with j.state == "Pending" do
42:     c_star = EvaluateAdmissibility(W_j, C)  // Enforces r <= Q_{c,g} and s <= S_c
43:     if c_star != NULL then
44:         AdmitWorkload(W_j, c_star)
45:         j.assignedCluster = c_star       // Persist cluster assignment for evidence capture
46:         j.state = "Admitted"             // Quota reserved; pipeline not yet dispatched
47:         A = A union {j}
48:         LaunchTektonPipelineRun(j, c_star)
49:         j.state = "Running"              // Set once the PipelineRun is dispatched
50:     end if
51: end for
52: 
53: // =========================================================================
54: // Phase 4: Preemption Handoff & Terminal Evidence Lifecycle
55: // =========================================================================
56: for each active job j in A do
57:     // Path A: Kueue Eviction / Preemption Handoff
58:     if W_j.isEvicted then
59:         CancelPipelineGracefully(j, reason="CancelledRunFinally")
60:         if HasVerifiedCleanupProof(j, key="preemption-cleanup-verified") then
61:             ReleaseKueueWorkload(W_j)    // Quota released only after target confirmed clean
62:             A = A \ {j}
63:             j.state = "Stopped"
64:         else
65:             // Fail-Closed Safety: Retain virtual Workload reservation to prevent
66:             // higher-priority jobs from colliding while remote GPUs are still active
67:             HoldWorkloadReservation(W_j)
68:         end if
69: 
70:     // Path B: Normal Benchmark Completion
71:     else if j.pipelineCompleted then
72:         e = CaptureForensicEvidence(j, j.assignedCluster)
73:         E = E union {e}
74:         ReleaseKueueWorkload(W_j)
75:         A = A \ {j}
76:         j.state = "Succeeded"
77: 
78:     // Path C: Pipeline Failure
79:     else if j.pipelineFailed then
80:         ReleaseKueueWorkload(W_j)        // Terminal failure: reservation released
81:         A = A \ {j}
82:         j.state = "Failed"
83:     end if
84: end for
85: 
86: return <A, E>
```

---

## 6. Provenance Schema Reference

The following table summarizes the forensic dimensions captured for every executed benchmark:

| Dimension | Captured Artifacts | Forensic Purpose |
| :--- | :--- | :--- |
| **Host Node** | Driver version, ROCm release, NUMA topology, node conditions | Guarantees identical host hardware/driver baseline |
| **Runtime** | Container image digest (SHA256), serving engine flags, model hash | Prevents silent parameter and runtime drift |
| **Workload** | GuideLLM random seed, prompt/generation token distributions | Enables deterministic replay of traffic sweeps |
| **Cluster State** | Kubernetes namespace events, container termination exit codes | Differentiates platform evictions from serving OOMs |
| **Telemetry** | Thanos TSDB metrics (SM utilization, HBM bandwidth, power) | Correlates latency spikes with hardware saturation |
| **KPI Output** | Caliper `kpis.json` (TTFT, ITL, TPS), MLflow tracking link | Provides machine-readable data for automated CI gates |
