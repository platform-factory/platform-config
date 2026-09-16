# platform-config

The Argo CD root of the Platform Factory reference implementation: an
app-of-apps (one root Application that creates other Applications) that
installs and configures the shared platform layer. Everything the running
platform is made of arrives through this repo, as a PR. `platform-bootstrap`
(Terraform) stops at installing Argo CD and pointing it here — that is the
"Terraform's last job" boundary (claim C-01).

## How the root finds this repo

`platform-bootstrap`'s `3-argocd` layer installs Argo CD plus a single
`root` Application whose source is this repo, path `apps/`, revision `main`,
automated sync with prune and self-heal. Every YAML file in `apps/` is one
child Application; each child owns one component and syncs it from
elsewhere in this repo.

```
apps/                          one Application per component — the root syncs this directory
├── crossplane.yaml            wave 0: Crossplane core, Helm chart rendered from charts/crossplane
├── crossplane-providers.yaml  wave 1: everything under crossplane/providers/
├── crossplane-platform.yaml   wave 2: everything under crossplane/platform/
├── kyverno.yaml               wave 2: Kyverno, Helm chart rendered from charts/kyverno
├── compositions.yaml          wave 3: everything under crossplane/compositions/, recursively
├── kyverno-policies.yaml      wave 3: everything under kyverno/policies/
└── systems.yaml               wave 4: the tenants — a DIFFERENT repo, github.com/platform-factory/systems, path tenants/
charts/
├── crossplane/                vendored upstream chart 2.3.5, unmodified (see charts/README.md)
└── kyverno/                   vendored upstream chart 3.9.1, unmodified (see charts/README.md)
crossplane/
├── providers/                 ImageConfig (mirror rule), DeploymentRuntimeConfigs, Provider packages, in waves
├── platform/                  ClusterProviderConfig, EnvironmentConfig, composition Functions, Crossplane's aggregated ClusterRole
└── compositions/              the platform's own APIs — one subdirectory per kind
    ├── system/                XRD + Composition + a non-applied example for `crossplane render`
    └── database/              XRD + Composition + a non-applied example for `crossplane render`
kyverno/
└── policies/                  the two validate-only ClusterPolicies, plus the read grant one of them needs
```

## Order, and why it holds

Argo CD applies sync waves lowest first and waits for a wave to be Healthy
before starting the next. That is used at two levels:

1. **Between components** — the chain is `crossplane` (0) →
   `crossplane-providers` (1) → `crossplane-platform` and `kyverno` (2) →
   `compositions` and `kyverno-policies` (3) → `systems` (4), and every link
   is the same argument: a wave installs the CRDs the next wave's objects are
   instances of. Everything under providers is a Crossplane CRD type, so it
   can't be validated until Crossplane core is running. The
   `ClusterProviderConfig` in `crossplane-platform` is a kind that ships
   *inside* a wave-1 provider package (`gcp.m.upbound.io`), and the
   composition `Function`s there ride the wave-1 `ImageConfig` mirror rule for
   their own image pulls. The XRDs in `compositions` need those Functions
   installed before a Composition referencing them can render, and the
   tenants in `systems` are instances of the XRDs. This gating only works
   because `3-argocd` restores Argo CD's health check for `Application`
   resources (removed from the built-ins in Argo CD 1.8, and without it a
   parent counts an unhealthy child as Healthy). Each child also carries a
   retry policy as the backstop.
2. **Inside providers** — `ImageConfig` (wave 0), then `provider-family-gcp`
   (wave 1), then the service providers (wave 2). The mirror rule has to
   exist before the first package pull; the family has to be Healthy before
   the packages that depend on it install. Argo CD's built-in health check
   for `pkg.crossplane.io/Provider` is what makes "Healthy" mean *installed
   and running*, not merely *object created*.

## The image plane

ADR-0010: nodes are private and pull images through Artifact Registry
remote repositories, never from the internet. Two places honor that here:

- **Crossplane core** — `apps/crossplane.yaml` overrides the chart's
  `image.repository` to the `ghcr-io` remote.
- **Packages** — `crossplane/providers/image-config.yaml` is a single
  `ImageConfig` rewriting the `xpkg.crossplane.io` prefix to the `ghcr-io`
  remote. Provider manifests keep their canonical upstream names; the
  rewrite covers them *and* the family dependency they bake in, which
  Crossplane would otherwise resolve straight from the internet.

Crossplane pulls packages itself — from its own pod, not through kubelet —
so it needs its own read access to Artifact Registry. `platform-bootstrap`'s
`0-foundation/iam.tf` grants `artifactregistry.reader` to the
`crossplane-system/crossplane` service account via Workload Identity; no
pull secret exists anywhere.

## Pinned versions

| Component | Version | Verified against | Date |
|---|---|---|---|
| Crossplane | 2.3.5 | `crossplane/crossplane` GitHub releases; `charts.crossplane.io/stable` index | 2026-08-27 |
| provider-upjet-gcp family (`provider-family-gcp`, `-storage`, `-sql`, `-cloudplatform`, `-artifact`, `-dns`) | v3.0.0 | repo release list and v3.0.0 release notes; every package pulled through the `ghcr-io` remote by registry probe | 2026-08-11, 2026-08-27 |
| composition functions (`function-go-templating`, `function-environment-configs`, `function-auto-ready`) | v0.12.5 / v0.7.5 / v0.6.9 | anonymous manifest fetch against `ghcr.io/crossplane-contrib/*`, which `xpkg.crossplane.io` fronts | 2026-09-16 |

Crossplane 2.4.0 shipped on 2026-08-20; 2.3.5 is the latest patch of the
minor the v3.0.0 providers were built against (`CROSSPLANE_VERSION = 2.3.4`
in their Makefile). Bumping either is a PR here, and the M4 dependency-bump
change class is expected to make exactly this kind of PR.

## Provider identity and platform config

M1 installed the provider packages but gave them no cloud credentials, so
nothing here could create a real resource. M2 adds that, and the pieces live
in two places because they answer two different questions.

**Which identity does a provider pod run as?** `crossplane/providers/runtime-configs.yaml`.
Workload Identity binds a Kubernetes service account to a Google one, and both
halves name the Kubernetes service account — the IAM binding in
`platform-bootstrap`'s `0-foundation/iam.tf`, and an
`iam.gke.io/gcp-service-account` annotation on the service account itself. By
default Crossplane names a provider's service account after its
`ProviderRevision`, so every version bump would mint a new name and silently
break the binding. A `DeploymentRuntimeConfig` per service provider pins it to
`provider-gcp-<svc>` instead, and each `Provider` manifest points at its own
with `runtimeConfigRef`. Five providers, five Kubernetes service accounts, one
Google service account behind them — Crossplane's docs call sharing one name
across packages a common mistake, and it causes reconcile loops.

Crossplane core keeps the *other* identity flow it has always used: the direct
federated principal, no Google service account, for pulling packages. Two flows
in one platform is deliberate; `0-foundation/iam.tf` explains which is which.

**What does a managed resource do with that identity?** `crossplane/platform/`,
synced by `apps/crossplane-platform.yaml` at wave 2 — after the provider
packages, because the first file's CRD ships inside one of them.

| File | What it is |
|---|---|
| `provider-config.yaml` | One cluster-scoped `ClusterProviderConfig` named `default`, `credentials.source: InjectedIdentity`. Crossplane v2's namespaced managed resources default their `providerConfigRef` to exactly `{kind: ClusterProviderConfig, name: default}`, so this one object serves every tenant namespace with no patch in any Composition — and no per-namespace `ProviderConfig` to compose. |
| `environment.yaml` | An `EnvironmentConfig` named `platform` holding the per-environment constants: project, region, org domain, git host, the VPC path (`projects/<p>/global/networks/<n>` — the form Cloud SQL's `privateNetwork` wants, which is `layers/1-network`'s `network_path` output, *not* its `network_self_link`), the Artifact Registry base and location, and the `docker-hub` and `ghcr-io` remotes. ADR-0012 §3 resolves a team to `<team>@<orgDomain>` from here, so there is no second file mapping teams to groups. Note the schema: `data` is a top-level field, not `spec.data`. |
| `functions.yaml` | The three composition functions M2 runs: `function-go-templating` v0.12.5, `function-environment-configs` v0.7.5, `function-auto-ready` v0.6.9. Package names stay canonical (`xpkg.crossplane.io/...`) so the existing `ImageConfig` mirror rule covers them exactly as it covers the providers. `function-patch-and-transform` is deliberately not installed. |
| `rbac.yaml` | A `ClusterRole` labelled `rbac.crossplane.io/aggregate-to-crossplane`, granting Crossplane the kinds the Compositions compose that its core role does not cover: namespaces, resource quotas, role bindings, batch jobs, and Argo `AppProject`/`Application`. |

Two things in `rbac.yaml` are worth knowing before reading a Composition.
Kubernetes will not let anything grant a permission it does not hold itself, so
creating a `RoleBinding` to a `ClusterRole` means either holding everything that
role confers or holding the `bind` verb on it by name; this file takes the
second, narrower route, which doubles as the allow-list of roles the platform
may ever hand a tenant. And Crossplane aggregates managed-resource and composite
kinds into its own `crossplane-admin`/`crossplane-edit` roles using its own
label — *not* Kubernetes' `aggregate-to-admin` — so a team bound only to the
built-in `admin` role gets nothing on managed resources or claims. Both role
names are on the `bind` list for that reason.

`function-auto-ready` is pinned to v0.6.9 and not to the numerically higher
v0.7.0: the maintainers returned to the 0.6 line after v0.7.0 and shipped three
security-only releases there that v0.7.0 does not carry, and v0.7.0's two new
features are unused here. Tags verified by anonymous manifest fetch against
`ghcr.io/crossplane-contrib/*` on 2026-09-16.


## The System XR

`crossplane/compositions/system/` is the first half of the paved road, and the
whole of claim C-05: **onboarding a tenant is one file.** That file lives in a
different repo — `github.com/platform-factory/systems`, one YAML per tenant —
and it says five things:

```yaml
apiVersion: platform.thecloudgeek.io/v1alpha1
kind: System
metadata:
  name: svc-hello          # = namespace = AppProject = registry repo = GCP service account
spec:
  owner:
    team: payments         # = payments@thecloudgeek.io, by convention
    repo: platform-factory/svc-hello
  tier: standard
  securityTier: internal
  size: S
```

Nothing there names a Kubernetes or GCP resource. That is ADR-0012 §6: the
schema speaks intent, the Composition holds the mechanism, and the schema is
where a mistake gets caught — a bad name, an unknown size, a team name that
is not a legal group local part all fail at `kubectl apply` with the field
named, before anything is created.

From those five fields the Composition builds sixteen objects in three places:

| Where | What | Why |
|---|---|---|
| The tenant's namespace | `Namespace`, `ResourceQuota`, two `RoleBinding`s, `ServiceAccount`, `ConfigMap` | the workload's home, its size class, its team's access, and its cloud identity |
| `argocd` | `AppProject`, `Application` | the tenant may sync its own repo's `k8s/` directory, into its own namespace, and nothing else |
| Google Cloud | a service account, a Workload Identity binding, four project IAM members, an Artifact Registry repository, and a repository IAM member | the System's identity, its database access, and somewhere to push images |

Four things are worth knowing before reading the Composition.

**A System is Cluster-scoped, and that is not cosmetic.** A cluster-scoped XR
may compose namespaced objects in *any* namespace; a namespaced one is confined
to its own. That is the only reason a single recipe can put an `AppProject` into
`argocd` and a `RoleBinding` into the tenant's namespace. It is also honest: a
System *creates* the namespace, so it cannot live inside one.

**The team is a field, and it binds in six places, not four.** ADR-0012 §4
lists four: the `RoleBinding` subjects (there are two bindings, see below), the
`AppProject` role, the `team` label, and the registry IAM member. ADR-0013 §5
added two more after the fact — the project IAM members granting
`roles/cloudsql.client` and `roles/cloudsql.instanceUser`, which are what let a
human log into their own database as themselves. The two ADRs disagree on the
count and ADR-0013 is the newer decision; that belongs in the M2 build log as an
ADR-0012 erratum. `systems/README.md` and the Composition's header carry the
same six-item list so C-06 is measured against one enumeration. A seventh
carrier exists and is not a grant: the `platform-system` ConfigMap's `group`
key, a derived copy that updates in place. C-06's predicted "re-created" column
is three entries — the three IAM members, because changing `member` on an
`IdentifierFromProvider` resource is a replacement rather than an update — and
everything else updates in place. That prediction is written into the
Composition's header, before the run.

**The team gets two ClusterRoles, and the second one is the surprising part.**
Crossplane aggregates managed-resource and composite kinds into its own
`crossplane-edit` role using its own label — *not* Kubernetes'
`aggregate-to-admin` — so a team bound only to the built-in `admin` role could
not create the very `Database` claim the paved road exists to offer. Binding
`crossplane-edit` as well fixes that and, in doing so, hands the team verbs `*`
on every managed resource in its own namespace. `deny-raw-managed-resources`
covers the half of that which matters most — hand-creating a cloud resource the
platform never sees — but it matches `CREATE` only, so patching or deleting a
*composed* managed resource is still reachable by the namespace's own team.
Crossplane restores it on the next reconcile; the case worth naming is patching
`managementPolicies` on a durable resource before deleting it. The narrower
design (a purpose-built `Role` over `databases` alone, instead of
`crossplane-edit`) needs `roles` added to `rbac.yaml` and is an M3 change; the
Composition's own comment records it so nobody has to rediscover it.

**Four composed objects are marked ready explicitly, and without that the
System would never go Ready.** Crossplane marks an XR Ready only when every
composed resource is ready, and "unspecified" counts as not ready.
`function-auto-ready` has no health check for `ResourceQuota`, `RoleBinding`,
`AppProject` or `Application`, and none of them publishes a `Ready` condition —
so each carries `gotemplating.fn.crossplane.io/ready: "True"`. The Argo
`Application` is deliberately *not* gated on Argo's own health: a tenant whose
repo has no `k8s/` directory yet would otherwise hang its System forever, and
C-05 onboards exactly such a tenant.

**What the platform never deletes.** Pruning a tenant's file removes the
namespace, the RBAC, the Argo project and the Google service account. It does
not remove the Artifact Registry repository: that is composed with management
policies that omit `Delete` (ADR-0015 §1), and it is deleted only by a human
following ADR-0015 §5's runbook. A mistaken `git rm` costs a rebuildable
namespace, never images or data.

One manual prerequisite the platform does not hide, and it is a **hard**
prerequisite rather than a graceful degradation: the Google Group
`<team>@thecloudgeek.io` must exist and be nested under
`gke-security-groups@thecloudgeek.io` before the first `System` syncs. Creating
groups is a Workspace-admin task (ADR-0012 §3). The Kubernetes half is settled —
a `RoleBinding` naming a group nobody belongs to is accepted and inert. The
Google Cloud half is **not verified**: five composed resources put
`group:<team>@thecloudgeek.io` into an IAM binding, and a search of Google's IAM
documentation on 2026-09-16 found no statement on whether `setIamPolicy` accepts
a principal email that does not resolve. If it rejects, those resources sit
not-Ready and the `System` never reports Ready. The first cycle settles it in
one observation.

`crossplane/compositions/system/example.yaml` is the same tenant as a
standalone file for `crossplane render`. It is **excluded from the sync** by
`apps/compositions.yaml`; applying it would create a second `svc-hello` System
competing with the one the `systems` repo owns.

## The Database XR

`crossplane/compositions/database/` is the second half of the paved road: a
service team writes six lines of YAML in its own repo and gets a private
Postgres its application logs into **as itself, with no password anywhere**.

The claim lives in the service repo's `k8s/` directory — it already does, at
`svc-hello/k8s/database.yaml` — and reaches the cluster through the System's
Argo CD `Application`. The tenant `AppProject` deliberately leaves
`platform.thecloudgeek.io` off its `namespaceResourceBlacklist` for exactly
that reason; the kind a tenant must *not* mint, `System`, is cluster-scoped and
is already denied by the absent `clusterResourceWhitelist`, and how many claims
a namespace may hold is `database-claim-budget`'s job rather than Argo's.

```yaml
apiVersion: platform.thecloudgeek.io/v1alpha1
kind: Database
metadata:
  name: main
  namespace: svc-hello     # the namespace IS the System (ADR-0012)
spec:
  engine: POSTGRES_16
  region: us-central1
  size: S
```

No project, no VPC, no machine type, no instance name, no user, no password,
no team. The namespace carries the ownership answer, so the Composition
derives the Cloud SQL instance (`svc-hello-main`), the database (`app`) and
the IAM database user (`svc-hello@platform-factory-ref.iam`) from the
namespace and the claim name alone.

**The credential is an identity, not a secret (ADR-0013).** The Composition
creates a Cloud SQL `User` of `type: CLOUD_IAM_SERVICE_ACCOUNT` for the
System's Google service account — a Postgres role with no password at all.
The application's pod runs the Cloud SQL Auth Proxy as a sidecar with
`--auto-iam-authn`, which mints a token from the pod's own identity and hands
the app a plaintext socket on `127.0.0.1:5432`. The token expires in an hour,
so "managed rotation" is delivered by construction rather than by a rotation
job.

**One password still exists, and it is honest about why.** Cloud SQL creates
an IAM database user with no privileges on anything, and only a privileged
Postgres role can `GRANT` them — on a fresh instance that means the built-in
`postgres` superuser with a password. So the Composition generates one, keeps
it in the Secret `<claim>-admin` in the team's own namespace, and spends it in
exactly one place: a `Job` named `<claim>-grant-<hash>` that runs `psql` over
the instance's private IP and grants the IAM user its privileges. Nothing on
the golden path mounts that Secret. It is break-glass, and the hash in the
Job's name means a change to what is granted produces a new Job rather than an
attempt to patch an immutable PodSpec.

That Job asks the *application's* identity first. It runs as the System's
service account, takes a token from the GKE metadata server, and checks
whether the IAM user already holds `CREATE` on schema `public`; only if the
answer is no does it spend the admin password. That ordering is what lets a
claim survive a cluster rebuild, because the password in a rebuilt cluster's
Secret is **not** the one Cloud SQL holds: the Secret dies with the cluster,
the Composition mints a new one, and the provider only ever sends
`root_password` at instance create (upjet injects the Secret's current value
into both the Terraform state and the config, so the diff is always empty).
Grants live in Postgres, which is durable, so on cycle 2 the check answers yes
and the Job completes without touching the password. The Composition's header
carries the code references, the one-command recovery for a developer who
needs the break-glass path, and the real fix: a seed held in
`platform-bootstrap` layer 3, whose Terraform state outlives the cluster.

That Job is also what makes readiness mean something. `function-auto-ready`
has a real health check for `batch/v1 Job` — ready only on `Complete=True` —
so the XR reports `Ready` when the application can actually log in, not merely
when an instance exists. That is what C-07(a) times. It is rendered only once
the instance, the `app` database *and* the IAM user are all Ready, so it never
runs against a database or a role that does not exist yet; and once it exists
it is never withdrawn from desired state, because a completed Job
garbage-collected by a flicker in observed state would take the evidence and
the readiness with it.

**What the team reads.** `status` carries `connectionName`, `privateIp`,
`databaseName`, `iamUser` and `adminSecret`, and the same four connection
values are written to a ConfigMap `<claim>-connection` in the namespace so a
Deployment can consume them without reading the XR.

**Durability, three locks deep (ADR-0015).** The instance and the database are
composed with `managementPolicies: [Observe, Create, Update, LateInitialize]`
— Crossplane will never issue a delete — plus, on the instance,
`deletionProtection` on the Terraform side and
`settings.deletionProtectionEnabled` on the Cloud SQL side. A `sql Database`
has neither field, so its second lock is the provider-side
`forProvider.deletionPolicy: ABANDON`: "stop managing it, do not drop it."
Note there is no Crossplane `deletionPolicy: Orphan` to reach for: Crossplane v2's
namespaced managed resources do not have that field, only the cluster-scoped
variants do, so `managementPolicies` is how durability is spelled. The
external name is deterministic (`<system>-<claim>`), which is the entire
mechanism by which a cluster rebuild *adopts* the existing instance instead of
creating a second empty one.

**Two things a reader should not mistake for oversights.**

- **No `CLOUD_IAM_GROUP` user.** ADR-0013 §5 gives humans the same IAM door as
  the application, via a Cloud SQL user for the owning team's group. It is
  deferred *within* M2 and the Composition's header says so at the top. The
  reason is scope, not capability: the team label lives on the namespace, and
  a Composition can read a resource it did not compose, so the lookup is about
  eight lines plus one RBAC rule. Until it lands, a human uses the break-glass
  Secret — which is exactly the shared credential ADR-0013 set out to remove,
  and therefore worth closing.
- **`edition: ENTERPRISE` is load-bearing.** The Cloud SQL API defaults
  PostgreSQL 16+ to `ENTERPRISE_PLUS`, which accepts only `db-perf-optimized-*`
  machine types. Every size class here is shared-core or custom, so omitting
  the edition fails every create with `Invalid Tier (...) for
  (ENTERPRISE_PLUS) Edition`.

**Prerequisite outside this repo.** A private-IP instance needs Private
Services Access — an allocated range and a service-networking peering on the
VPC — from `platform-bootstrap`'s `layers/1-network`. Without it the
Composition renders fine and the instance sits in a `NETWORK_NOT_PEERED` loop,
so that is the first place to look when an instance never gets an IP.

**Checked before merge.** ADR-0014's first gate runs with no cluster:

```
crossplane resource validate crossplane/compositions/database/xrd.yaml <claim>.yaml
```

which evaluates the schema's enums *and* its CEL rules, and rejects unknown
fields. A claim asking for `region: eu-west1` is told which regions exist; a
claim asking for `size: L` without `tier: critical` gets the rule's own
sentence; a claim inventing `machineType:` is told the field does not exist.

## Kyverno

Kyverno arrives in M2 as `apps/kyverno.yaml` (wave 2, the vendored chart in
`charts/kyverno`) and `apps/kyverno-policies.yaml` (wave 3, the policies in
`kyverno/policies/`).

**It is validate-only, and that is a decision, not a starting point.** No
mutating policy exists here and none should: a mutation rewrites an object
after Argo CD applied it, so Argo's next diff sees a cluster that no longer
matches git and reports drift forever. Validate-only keeps "OutOfSync"
meaning what it says and keeps `ServerSideDiff` unnecessary (ADR-0014).

**It is also the last gate, not the first.** ADR-0014's order is: the XRD
schema says no to everything it can express — `enum`, `pattern`, CEL — because
that is free, checked offline in CI by `crossplane resource validate`, and
produces the clearest message. Kyverno is only for the two things a schema
cannot say, one per file:

| Policy | What a schema cannot say |
|---|---|
| `deny-raw-managed-resources` | "only Crossplane creates these" — a rule about *who is asking*, not about the object |
| `database-claim-budget` | "at most two per namespace" — a rule about *the other objects*, not this one |

`database-claim-budget` counts existing claims through an `apiCall`, which
needs a read grant Kyverno does not ship with; `database-claim-budget-rbac.yaml`
is that grant, and it sits next to the policy because without it the policy
fails silently open rather than loudly.

The two differ in `failurePolicy` on purpose. The reality gate is `Fail`, so
a Kyverno outage blocks managed-resource creation — the ADR's "and nothing
else" is what scopes the blast radius to two API groups. The budget is
`Ignore`, because a cost guardrail should not wedge every tenant's deploys
when the policy engine is down.

Images: chart 3.9.1's default registry is `reg.kyverno.io`, which is not one of
the five Artifact Registry remotes, so `apps/kyverno.yaml` overrides it to the
`ghcr-io` remote. One key covers every image that actually runs. Two others
(`webhooksCleanup`, `test`) pin `ghcr.io` literally and out-rank
`global.image.registry`, but they are deliberately *not* overridden: both
belong to Helm hooks — `pre-delete` and `test` — that Argo CD 3.4.6 does not
implement, so neither image is ever pulled. `webhooksCleanup` is switched off
for the same reason, and its absence has a consequence worth knowing: Kyverno
creates its webhook configurations at runtime, so they are not in git, Argo CD
will not prune them, and **removing Kyverno means deleting this Application
and then deleting the `kyverno-*-webhook-cfg` objects by hand.** Skipping the
second step leaves `validate.kyverno.svc-fail` in place at `failurePolicy:
Fail` with nothing behind it, which blocks every managed-resource create in
the cluster. The full reasoning and the commands are in `apps/kyverno.yaml`.

Two more things a reader should not have to discover the hard way. **Healthy
is a weak signal here:** Argo CD 3.4.6 ships no health check for
`kyverno.io/ClusterPolicy`, so the wave-3 Application reports Healthy once the
objects exist, not once their rules are registered on a webhook — `kubectl get
cpol` is the check to run before trusting a denial test. And both policies use
`kyverno.io/v1 ClusterPolicy`, a group upstream has marked deprecated; the
successor `ValidatingPolicy` cannot express either rule today (no
partial-wildcard API groups, no `apiCall` context), and the migration trigger
is recorded in `kyverno/policies/deny-raw-managed-resources.yaml` and
`charts/README.md`.

## Adding a component

1. Put its manifests (or a vendored chart) in a directory named for it.
2. Add one Application in `apps/` pointing at that directory, with a sync
   wave that places it after anything whose CRDs it needs.
3. If it pulls images, route them through an Artifact Registry remote; add
   the upstream to `platform-bootstrap`'s `0-foundation/registry.tf` if no
   existing remote covers it.

## Not here yet

External Secrets Operator, external-dns, and the Gateway are M3 work.

The `CLOUD_IAM_GROUP` database user — the path by which a human on the owning
team logs into Cloud SQL with their own identity rather than the break-glass
admin password — is deferred within M2 and is documented in the header of
`crossplane/compositions/database/composition.yaml`.

Everything else this section used to list is now written here: the two XRDs and
Compositions, the `System` API, Kyverno, and the `ClusterProviderConfig` that
gives the providers their GCP identity. Written, not yet run — see **Status**
at the bottom. Once synced, this repo does create cloud resources: a `System`
composes an Artifact Registry repository, a service account and its IAM grants,
and a `Database` composes a Cloud SQL instance.

## Part of the Platform Factory

This repo is one of seven that make up the reference implementation of the
**Platform Factory** pattern. The design seed — pattern docs, ADRs, and the
build plan — lives at [https://github.com/thecloudgeek/platform-factory](https://github.com/thecloudgeek/platform-factory).

This repo is built out in **M1** and extended in **M2**.

## Status

**Status:** M2 — authored, not yet synced to a cluster.

M1 landed the spine and has been exercised: Crossplane core plus the GCP
provider family, ordered by sync waves, images and packages routed through
Artifact Registry. Three scripted cycles ran at zero interventions (C-02/C-04,
recorded in the design seed's `docs/build-log/m1-spine.md`).

M2 adds the paved road on top, and **none of it has run against a cluster
yet.** Every file below is written and internally consistent; the first sync is
what turns that into evidence:

- `crossplane/platform/` — the `ClusterProviderConfig` that finally gives the
  providers a GCP identity, the `EnvironmentConfig`, the three composition
  functions, and Crossplane's aggregated `ClusterRole`.
- `crossplane/compositions/` — the `System` and `Database` XRDs and
  Compositions: the whole developer-facing API surface.
- `charts/kyverno/` + `kyverno/policies/` — the two validate-only
  `ClusterPolicies` and the read grant one of them needs.
- `apps/systems.yaml` — wave 4, pointing at the `systems` repo, whose first
  tenant file is `tenants/svc-hello.yaml`.

Three things outside this repo have to be true before the first sync means
anything, and each is a named manual step rather than something the platform
hides: `platform-bootstrap`'s M2 layer-0 and layer-1 applies (the provider
Google service account, the new APIs, and Private Services Access), the Google
Workspace groups (`gke-security-groups@` with the team groups nested inside),
and one hand-push of the `svc-hello` image. The consolidated order lives in
`platform-bootstrap`'s README.

The first live run is C-02 cycle 4 — the first rebuild with a tenant, a
database and durable cloud resources on the other side of it — and it doubles
as C-05 (merge-to-usable for a second tenant), C-07(a) (merge-to-usable for a
database) and C-07(c) (the rebuild adopts rather than re-creates). Results land
in the design seed repo's `docs/build-log/m2-paved-road.md`.
