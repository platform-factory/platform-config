# charts/system

Everything one tenant gets, from its file in the `systems` repo (ADR-0012,
ADR-0017).

## Why it is shaped this way

Onboarding a tenant is one file (C-05). One recipe therefore has to reach
every place a tenant must exist: its namespace, Argo CD's namespace, and
Google Cloud. Under Crossplane that recipe was a Composition; from M2b it is
this chart, rendered by an Application the `systems` ApplicationSet generates
for each tenant file, in the platform's Argo CD project.

The tenant file does not change, not one byte (ADR-0017 §3). It used to be
applied as a `System` object; now it is read as values.

## The inputs, in order

The generated Application passes three value files, and the last one wins:

1. `systems/tenants/<name>.yaml`: the tenant file (`apiVersion`, `kind`,
   `metadata`, `spec`).
2. `systems/retired.yaml`: `retired:`, names that may not be used again.
3. `environments/<env>.yaml` from this repo: `environment:`, the facts.

`values.schema.json` checks the merged result and is closed at every level.
It carries the System XRD's rules unchanged and refuses more names
(ADR-0017 §6):

| Refused | Why |
|---|---|
| a name ending in `-system` or `-claims` | every tenant's three Applications share the `argocd` namespace: System `orders` owns `orders-claims`, which would also be the tenant Application of a System named `orders-claims` |
| `argocd`, `default`, `kyverno`, and any name starting `kube-`, `gke-`, `gmp-`, `cnrm-` or `configconnector-` | namespaces the platform or GKE already uses: the chart would bind the team as `admin` there, and offboarding would delete it |
| `systems`, `config-connector`, `kyverno-policies`, and the Crossplane-era `crossplane`, `crossplane-providers`, `crossplane-platform`, `compositions` | platform Applications in `argocd`, which a tenant Application of the same name would overwrite |
| `jumpbox`, `crossplane-provider-gcp`, `config-connector` (and `gke-nodes`, by prefix) | Google service accounts the platform's Terraform creates: the chart would adopt the account by name and bind it to the tenant's pods |
| `docker-hub`, `quay-io`, `ghcr-io`, `ecr-public`, `registry-k8s-io` | Artifact Registry repositories the platform's Terraform creates: the chart would try to make one the tenant's registry |
| a name on `systems/retired.yaml` | refused by `templates/guard.yaml`, because a schema cannot compare one value with another (ADR-0017 §9) |

The tenant file is approved by the platform, so these refusals guard against
mistakes. The tenant boundary is `charts/claims`' schema. Both are enforced at
Argo CD's render, which no merge can skip, and tested only as CI fixtures,
never against a live cluster.

## What a tenant gets

| Object | Where | Notes |
|---|---|---|
| `Namespace` | `<name>` | `Prune=false`, so a chart change that stopped rendering it cannot delete it (ADR-0019 §1); carries the project annotation Config Connector falls back to |
| `ResourceQuota` | `<name>` | from the size class; makes CPU and memory limits mandatory for every container |
| `RoleBinding` `<name>-admin` | `<name>` | the team's Google Group gets the built-in `admin` role |
| `ServiceAccount` `<name>` | `<name>` | the System's Kubernetes identity, mapped to its Google one |
| `ServiceAccount`, `Role`, `RoleBinding` `database-seed` | `<name>` | lets charts/claims' seed Job create a `<claim>-admin` Secret, and nothing more |
| `ConfigMap` `platform-system` | `<name>` | the facts, for people and tools |
| `AppProject` `<name>` | `argocd` | the tenant's wall on the git side: its repo, its namespace, no cloud kinds, no Argo CD kinds, no RBAC |
| `Application` `<name>` | `argocd` | the service repo's `k8s/`, under the project above |
| `Application` `<name>-claims` | `argocd` | the service's `claims.yaml`, rendered by charts/claims in the platform's project |
| `IAMServiceAccount` `<name>` | `<name>` | `<name>@<project>.iam.gserviceaccount.com` |
| `IAMPolicyMember` ×6 | `<name>` | Workload Identity; Cloud SQL client and instance user for the app and for the team; registry writer for the team |
| `ArtifactRegistryRepository` `<name>` | `<name>` | durable: abandoned, never deleted, by the engine |

## Moving a System to another team

Edit `spec.owner.team`. The RoleBinding, the AppProject's role, the labels and
the ConfigMap update in place. Three IAM members change hands: an IAM
member's fields cannot change, so the ones naming the team carry the team in
their object name. A move renders three new members, and Argo CD prunes the
three old ones (ADR-0017 §10). C-27 measures it.

## Removing a System

A PR, then a person's step (ADR-0019 §1), and the person's step is two
commands, in this order:

1. **One PR to `systems`** removes the tenant file and adds its name to
   `retired.yaml`. The `systems` check fails a PR that forgets the second part.
   After the merge, the tenant's `<name>-system` Application can no longer
   render, so Argo CD stops syncing it. Nothing is deleted yet.
2. **A person deletes the claims Application first:**
   `argocd app delete <name>-claims`, and waits until it is gone. Its objects
   are deleted while the `<claim>-admin` Secret still exists, so Config
   Connector releases each durable one (abandons it) and clears its finalizer.
   Skip this step for a System with no databases.
3. **Then the System:** `argocd app delete <name>-system`. Its finalizer
   removes the namespace and every non-durable object in it. The registry and
   any databases stay, abandoned, for ADR-0015 §5's runbook.

Why step 2 comes first: Config Connector reads a database's `<claim>-admin`
Secret even when it is only letting go of the instance. If the namespace's
teardown deleted that Secret first, the engine would retry for ever, and the
namespace would hang in Terminating [code confirmed at v1.156.0, effect
inferred until C-31's return build; see the permission trace in
`platform-roles`].

One thing the runbook has to clean up: when a namespace is deleted, the
registry's object can go before the team's writer grant on it, and then that
grant is left on the abandoned registry (same trace). ADR-0015 §5's runbook
removes it with the registry.

The `systems` ApplicationSet never deletes an Application by itself, so a PR
that renamed or emptied `tenants/` would delete nothing.

## Chart details

- **Helm 3.19.x**, as for charts/claims: Argo CD v3.4.6 bundles 3.19.4.
- **The tenant Application skips `database.yaml`** in the service's `k8s/`, for
  M2b only. svc-hello keeps its Crossplane claim there so a rollback to
  Crossplane still has one to read (ADR-0017 §12). The exclusion goes when M2b
  closes.
- **The claims Application reads this repo at `environment.platformRevision`**,
  the M2b branch while it is being built and `main` after it merges
  (ADR-0017 §12).
