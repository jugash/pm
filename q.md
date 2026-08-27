# Cluster Quota Charter

**A multi-tenant OpenShift resource quota policy**
Version 1.0 · OpenShift 4.x

> A guaranteed floor every team can count on, headroom to burst into shared capacity when it is free, and hard ceilings that keep one noisy tenant from starving the rest. Governed with `ClusterResourceQuota`, one per team, across all of that team's projects.

- **Model:** Guaranteed floor + burst
- **Scope:** ClusterResourceQuota per team
- **Covers:** Compute · Storage · Object counts
- **Fleet:** Applied per cluster — separate clusters per environment (dev / prod)

---

## 1. Principles

The policy exists to make a shared cluster feel private to each team while keeping utilization high. Six commitments hold it together — every rule downstream traces back to one of them.

1. **Guarantee, then share.** Each team gets a reserved floor of requests it can always schedule. Everything above the floor is shared burst capacity, allocated first-come while nodes have room.
2. **No unbounded pod.** Every container carries a request and a limit — supplied automatically by a `LimitRange` when the author omits them. A quota on `limits.*` is meaningless without it.
3. **Blast radius is one tenant.** Hard ceilings on compute, storage, and object counts mean a runaway workload exhausts its own quota and stops — never the cluster.
4. **Self-service within the box.** Teams create projects, deploy, and scale freely up to their quota without a ticket. Tickets are only for growing the box itself.
5. **Legible and reviewable.** Quota is declarative, version-controlled, and named by team. Anyone can read the current allocation; changes go through the same review as any other config.
6. **Measured before enforced.** New quotas ship in a monitor-first mode. We size from real usage percentiles, not guesses, and tighten only after the data is in.

---

## 2. Tenancy model

A **team** is the unit of tenancy, not a project. Within any one cluster a team routinely owns several projects — `web-api`, `web-storefront`, `web-checkout` — and a per-project `ResourceQuota` would let a team dodge its cap simply by making another project. OpenShift's `ClusterResourceQuota` solves this: a single cluster-scoped object aggregates usage across *every* project a team owns in that cluster and enforces one shared budget.

Projects are bound to a team's quota by a label the platform stamps at creation time, so onboarding a new project is automatic — no quota edit required.

```yaml
# Every project a team owns carries this label. The platform's
# project-request template applies it automatically at creation.
metadata:
  labels:
    tenant.company.io/team: web-platform
    tenant.company.io/env:  prod       # matches the cluster's environment
```

**Why label, not annotation.** `ClusterResourceQuota` can select by annotation too, but labels are indexable and let the same key drive `NetworkPolicy`, RBAC `RoleBinding`s, and cost reporting. One identity for the team, reused everywhere.

### Across clusters and environments

Environments are **separate clusters** (a dev cluster, a prod cluster, and so on), not namespaces inside one cluster. `ClusterResourceQuota` is a cluster-scoped object, so each cluster carries its own copy of a team's quota — a team's prod budget lives only on the prod cluster, its dev budget only on dev. Two consequences shape how the policy is applied:

- **One identity, many budgets.** The `tenant.company.io/team` label is the same everywhere, so a team is recognizably itself on every cluster, but the *numbers* are set per cluster. A team can be Large on prod and Small on dev.
- **Environment sets the posture.** Prod clusters lean toward tighter overcommit and firmer guarantees (a denial there protects live traffic); dev clusters run more elastic — a wider request-to-limit gap, more generous object counts — because the cost of a burst is low and iteration speed matters.

Manage this with GitOps overlays (Kustomize/Argo), not copy-paste: a single `base/` holds each team's quota and LimitRange, and per-cluster overlays (`overlays/prod-us-east`, `overlays/dev`) patch only the numbers. Adding a cluster is a new overlay; the policy itself is written once.

```
quota/
  base/
    team-web-platform/        # crq + limitrange, no numbers hard-coded
  overlays/
    prod-us-east/  → patches team quotas to prod (Medium/Large) sizes
    prod-eu-west/  → same teams, region-local sizing
    dev/           → dev (Small, elastic) sizes for every team
```

---

## 3. The floor-and-burst model

The whole model rests on Kubernetes' split between **requests** (what the scheduler reserves for a pod) and **limits** (the ceiling a pod may consume before it is throttled or OOM-killed). We quota the two separately, and that difference *is* the burst.

| Layer | Backed by | Meaning |
|---|---|---|
| **Guaranteed floor** | `requests.cpu` / `requests.memory` | Reserved — always schedulable |
| **Burst ceiling** | `limits.cpu` / `limits.memory` | Use shared capacity when it is free |
| **Hard cap** | object counts · storage | Absolute — cannot be exceeded |

Set the **requests** quota to what the team genuinely needs steady-state — this is subtracted from cluster capacity and guaranteed to them. Set the **limits** quota higher, typically `1.5×` to `2×` the floor, so their pods can climb into unused capacity during spikes. Because the sum of every team's floor stays at or below allocatable capacity while the sum of ceilings is deliberately oversubscribed, the cluster runs full without ever breaking a guarantee.

> **The rule that makes it safe:** Σ `requests` across all teams ≤ allocatable cluster capacity. Σ `limits` may exceed it. Burst is opportunistic; the floor is a contract. Keep the first inequality true and no team can ever be denied its reservation.

---

## 4. What we put quota on

Three families of resource, each for a different failure it prevents. Compute stops capacity exhaustion; storage stops a team filling the backing store; object counts stop control-plane and cloud-API abuse.

| Resource | Governs | Type | Why it's on the list |
|---|---|---|---|
| `requests.cpu` | Reserved CPU | floor | The team's guaranteed schedulable CPU. |
| `requests.memory` | Reserved memory | floor | Guaranteed memory; memory is non-compressible, so this is the hard guarantee. |
| `limits.cpu` | CPU ceiling | burst | Cap on burst; prevents one team monopolizing spare cores. |
| `limits.memory` | Memory ceiling | burst | Cap on burst memory before pods are OOM-killed. |
| `requests.storage` | Total PV capacity | cap | Aggregate persistent storage the team can claim. |
| `persistentvolumeclaims` | PVC count | cap | Stops PVC sprawl exhausting the provisioner. |
| `<class>.storageclass.storage.k8s.io/requests.storage` | Per-class capacity | cap | Ration fast/expensive classes (e.g. NVMe) separately from bulk. |
| `requests.ephemeral-storage` | Node scratch space | cap | Prevents logs/temp files filling node local disk. |
| `pods` | Running pods | cap | Bounds scheduler and kubelet load per tenant. |
| `services.loadbalancers` | Cloud LBs | cap | Each one costs money and a public IP — the tightest object cap. |
| `services.nodeports` | NodePort services | cap | NodePorts are a scarce shared range; usually held near zero. |
| `count/routes.route.openshift.io` | Exposed routes | cap | Bounds ingress/router config a tenant can generate. |
| `secrets` | Secret objects | cap | Guards against etcd bloat and token-farm abuse. |
| `configmaps` | ConfigMap objects | cap | Same etcd-pressure reasoning as secrets. |

Anything not listed is deliberately unquoted — `configmaps` and `secrets` caps are set generously because a false denial there breaks deploys for little benefit. The scarce, costly, or capacity-defining resources get the tight numbers.

---

## 5. Sizing profiles

Rather than negotiate every number per team, we publish three starting profiles. A team requests the closest fit and we tune from there. Floors below are the reservation; ceilings are what they can burst to. These are per *team, per cluster* (summed across all their projects on that cluster), not per project.

| Dimension | Small | Medium | Large |
|---|--:|--:|--:|
| `requests.cpu` *(floor)* | 8 | 32 | 96 |
| `limits.cpu` *(burst)* | 16 | 64 | 160 |
| `requests.memory` *(floor)* | 16Gi | 64Gi | 192Gi |
| `limits.memory` *(burst)* | 32Gi | 128Gi | 320Gi |
| `requests.storage` | 100Gi | 500Gi | 2Ti |
| `persistentvolumeclaims` | 10 | 40 | 120 |
| `pods` | 40 | 150 | 400 |
| `services.loadbalancers` | 1 | 3 | 8 |

> **Treat as a starting point.** Profiles are anchors, not tiers a team is locked into. The burst ceiling sits at ~`1.7–2×` the floor throughout; hold that ratio when you tune so overcommit math stays predictable across the fleet.

**Sizing by environment.** The same team usually maps to a different profile per cluster. Prod gets the profile that matches real steady-state load with a firm floor; dev is typically one size down with a wider burst ratio (say `limits ≈ 3× requests`) so experiments are cheap and denials rare. Set these in the per-cluster overlay, not by hand-editing each cluster.

---

## 6. The manifests

Everything below is what actually ships to the cluster for one team (`web-platform`, Medium profile). Keep these in Git; apply through your GitOps pipeline, never by hand.

### Primary quota — the floor and burst

One `ClusterResourceQuota`, selecting all of the team's projects by label, scoped to long-running workloads so batch jobs don't consume the service reservation.

```yaml
apiVersion: quota.openshift.io/v1
kind: ClusterResourceQuota
metadata:
  name: team-web-platform
spec:
  selector:
    labels:
      matchLabels:
        tenant.company.io/team: web-platform
  quota:
    hard:
      # --- guaranteed floor (reserved, always schedulable) ---
      requests.cpu:    "32"
      requests.memory: 64Gi
      # --- burst ceiling (opportunistic, ~2x the floor) ---
      limits.cpu:      "64"
      limits.memory:   128Gi
      # --- object & storage caps ---
      pods:                       "150"
      requests.storage:           500Gi
      persistentvolumeclaims:     "40"
      requests.ephemeral-storage: 50Gi
      services.loadbalancers:     "3"
      services.nodeports:         "0"
      secrets:                    "200"
      configmaps:                 "200"
      count/routes.route.openshift.io: "30"
      # --- per storage class (ration the fast tier) ---
      nvme.storageclass.storage.k8s.io/requests.storage: 100Gi
    scopes:
      - NotTerminating   # services & long-running only; jobs quota'd separately
```

### Batch quota — jobs, builds, cron

A second quota, same selector, scoped to `Terminating` (anything with an `activeDeadlineSeconds` — Jobs, builds, short-lived pods). This keeps a burst of CI builds from eating the capacity the team's services rely on.

```yaml
apiVersion: quota.openshift.io/v1
kind: ClusterResourceQuota
metadata:
  name: team-web-platform-batch
spec:
  selector:
    labels:
      matchLabels:
        tenant.company.io/team: web-platform
  quota:
    hard:
      requests.cpu:    "16"
      requests.memory: 32Gi
      limits.cpu:      "32"
      limits.memory:   64Gi
      pods:            "60"
    scopes:
      - Terminating
```

---

## 7. LimitRange — the quiet dependency

A quota on `limits.cpu` or `limits.memory` rejects any pod whose containers don't declare limits. Most authors won't. Without a safety net, the quota turns into a wall of failed deploys and angry tickets. The `LimitRange` is that net: it injects sane defaults, sets a floor and ceiling per container, and — via `maxLimitRequestRatio` — caps how far a single container may burst.

Ship one `LimitRange` into every project (through the project template) alongside the quota.

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: tenant-defaults
spec:
  limits:
    - type: Container
      default:              # limit if author omits one
        cpu: "500m"
        memory: 512Mi
      defaultRequest:       # request if author omits one
        cpu: "100m"
        memory: 128Mi
      max:                  # no single container larger than this
        cpu: "8"
        memory: 16Gi
      min:
        cpu: "10m"
        memory: 16Mi
      maxLimitRequestRatio: # limit may be at most 4x the request
        cpu: "4"
        memory: "4"
    - type: PersistentVolumeClaim
      max:
        storage: 200Gi
      min:
        storage: 1Gi
```

> **Order of operations:** Apply the `LimitRange` *before* or *with* the quota, never after. If the quota lands first, in-flight deploys without explicit limits are rejected until the LimitRange catches up.

---

## 8. Scopes and priority classes

Scopes let one selector carry several budgets, each targeting a slice of the team's workloads. We use three deliberately.

- **NotTerminating** — long-running services. Holds the reservation the team's apps depend on; this is the primary quota.
- **Terminating** — Jobs, builds, cron, anything with a deadline. A separate, smaller budget so batch bursts can't starve services.
- **PriorityClass** — cap how much a team may schedule at high priority, so `preempt`-capable pods can't be used to jump the shared queue at will.

To bound high-priority scheduling, add a scoped quota keyed to the priority classes you allow:

```yaml
spec:
  quota:
    hard:
      pods: "20"          # at most 20 high-priority pods for this team
    scopeSelector:
      matchExpressions:
        - scopeName: PriorityClass
          operator: In
          values: ["high-priority", "system-critical"]
```

---

## 9. Overcommit strategy

Overcommit is what turns "sum of floors ≤ capacity" into a cluster that runs at 80–90% utilization instead of 40%. Two dials control it, and they must be set together.

- **Fleet dial** — the ratio between total ceilings and total floors. We target Σ`limits` ≈ `1.5×` to `2×` Σ`requests`. Higher means better utilization but more risk of contention when many teams burst at once.
- **Container dial** — `maxLimitRequestRatio` in the LimitRange (set to `4` above) stops a single container from requesting a sliver and bursting enormously, which would let one pod dodge the intent of the floor.

Keep the request-to-limit gap honest: a workload that permanently runs near its limit should have its *request* raised, not just its limit. Requests are the number the scheduler and the guarantee are built on; systematically under-requesting to hide inside burst capacity is the one behavior this policy is designed to catch.

> **Watch for:** If aggregate *usage* regularly approaches aggregate *requests*, the cluster is genuinely full — add nodes. If usage is low but *limits* are exhausted, teams are over-declaring ceilings; tighten `maxLimitRequestRatio` rather than buying hardware.

---

## 10. Enforcement and rollout

Quota that lands as a surprise breaks production and burns trust. Roll out in three phases, per team, and never skip the first.

1. **Observe (2–4 weeks).** Apply generous quotas set well above current usage — effectively non-binding. Collect the `p95` and `p99` of requests and limits actually used from the metrics below. This is where the real numbers come from.
2. **Right-size and warn.** Set floors to `p95` of requests plus headroom, ceilings per the overcommit ratio. Alert (don't block) when a team crosses 80% of any dimension, and share a per-team dashboard so teams see their own position.
3. **Enforce.** Quotas now bind. By this point every team has weeks of visibility into where they sit, so denials are rare and expected. Keep the 80% alerts running permanently.

```yaml
# fires when a team uses >80% of its guaranteed request quota
expr: 100 *
  openshift_clusterresourcequota_usage{resource="requests.cpu", type="used"}
  /
  openshift_clusterresourcequota_usage{resource="requests.cpu", type="hard"}
  > 80
for: 15m
labels: {severity: warning}
annotations:
  summary: "{{ $labels.name }} is at {{ $value | printf \"%.0f\" }}% of its CPU floor"
```

---

## 11. Operational runbook

### A deploy failed with "exceeded quota"

Expected behavior, not an incident. The error names the dimension and the numbers. Read which resource is exhausted, then either free some (scale an idle deployment down, delete unused PVCs) or request an increase.

```bash
# current usage vs. hard limit, every dimension
oc get clusterresourcequota team-web-platform -o yaml

# quick human-readable view
oc describe clusterresourcequota team-web-platform

# what a specific project is contributing to the shared budget
oc get resourcequota -n web-prod
```

### A team needs more

1. **Request in Git.** Open a PR against the team's quota manifest with the new numbers and a one-line justification (what workload, expected steady-state usage).
2. **Check the budget.** The platform team confirms Σ`requests` across all teams stays ≤ allocatable capacity after the change. If it wouldn't, the answer is add nodes or trade against an underused team — not silently oversubscribe the floor.
3. **Merge and observe.** GitOps applies it. Watch the team's dashboard for a week to confirm the new size fits.

> **Metrics that matter:** Track `openshift_clusterresourcequota_usage` (used vs. hard, per team per resource) and `kube_resourcequota` for the project-level breakdown. A team pinned at 100% on any dimension for days is either under-provisioned or leaking resources — investigate before raising.

---

## 12. Governance

The policy only holds if the quota objects themselves are protected and reviewed on a cadence.

- **Only the platform team writes quota.** `ClusterResourceQuota` is cluster-scoped; tenants get no RBAC verb on it beyond `get`. They can read their own budget, never edit it. The `LimitRange` in a project is likewise owned by the template, not the tenant.
- **Everything is in Git.** No `oc edit` in production. The manifests are the source of truth; the cluster is reconciled to them by GitOps. An out-of-band edit is an incident, not a shortcut.
- **Quarterly review.** Each quarter, reconcile every team's allocation against actual `p95` usage. Reclaim floors that sit permanently idle; grow the teams that are pinned. This keeps Σ`requests` honest as the fleet grows.
- **Default-deny for new projects.** A project created outside the template — without the team label, and so matched by no quota — should be caught by policy (Gatekeeper / Kyverno) and rejected. An unquoted project is an unbounded project.

> **The one rule to never break:** A workload must never run unquoted. Every project belongs to a team, every team has a `ClusterResourceQuota`, and every container gets requests and limits from the `LimitRange`. Miss any link in that chain and a single tenant can take the cluster down.

---

*Cluster Quota Charter · v1.0 · OpenShift 4.x*
*requests = the contract · limits = the burst · counts = the cap*
