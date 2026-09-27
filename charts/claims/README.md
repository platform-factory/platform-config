# charts/claims

A service's databases, from its `claims.yaml`. Only the platform renders this
chart (ADR-0017 §4).

## Why it is shaped this way

A developer should be able to say five things about a database and nothing
else: engine, region, size, tier, backups. Under Crossplane that allowlist was
a custom API the cluster enforced. Here it is enforced by **who runs the
recipe**: the service's repo supplies one values file, and the platform's own
Argo CD Application renders this chart with it. The service cannot choose the
chart, its version, or the project it renders under.

So `values.schema.json` is a security boundary, not a convenience. Every level
of it is closed, every value a tenant supplies is an enum, a pattern or a
quoted string, and no template calls `tpl` on a tenant value (ADR-0017 §6).
Review a change to this chart as policy, not as templating.

## The inputs

```yaml
# claims.yaml, at the root of the service repo
databases:
  main:                 # the claim's name: lowercase letters and digits, no hyphen
    engine: POSTGRES_16
    region: us-central1
    size: S             # S, M or L
    tier: standard      # optional; size L needs critical here AND a critical System
    backups: true       # optional; only size S may turn it off
```

Everything else arrives in the Application's `helm.valuesObject`, written by
`charts/system`: `system` (name, team, tier) and `environment` (every key of
`environments/<env>.yaml`). The valuesObject outranks the value file, and it
writes every key of both, so a `claims.yaml` that sets `system.tier` or
`environment.projectID` changes nothing; the CI proves it for every key.

## What a claim gets

| Object | Name | Durable |
|---|---|---|
| `SQLInstance` | `<system>-<claim>` | yes |
| `SQLDatabase` | `<system>-<claim>-app` (database `app`) | yes |
| `SQLUser` | `<system>-<claim>-iam-user` (user `<system>@<project>.iam`) | yes |
| `ConfigMap` | `<claim>-connection` | no |
| `Job` | `<claim>-seed-<hash>`: creates `<claim>-admin` once | no |
| `Job` | `<claim>-grant-<hash>`: gives the IAM user its privileges | no |

**Durable** means the object carries `cnrm.cloud.google.com/deletion-policy:
abandon` from its first sync. When Argo CD prunes it, Config Connector lets go
of the cloud resource instead of deleting it (ADR-0017 §9). A Kyverno rule
refuses any edit that removes the annotation.

**The chart emits nothing that does not belong to a claim** (ADR-0019 §2).
With no databases, it renders nothing at all. That matters because of a guard
in Argo CD: its automated sync will not prune an Application down to nothing.
So:

- a `claims.yaml` that is missing, empty or misnamed (`claims.yml`) deletes
  nothing. The claims Application reports a sync error and waits.
- **Removing a System's last database is a person's step.** Remove the entry,
  let the claims Application refuse the automated prune, then run a manual
  sync with prune: `argocd app sync <system>-claims --prune`. The instance,
  database and user stay in Cloud SQL, abandoned. Deleting them for real is
  ADR-0015 §5's runbook.

## Chart details

- **Helm 3.19.x.** Argo CD v3.4.6 bundles Helm 3.19.4. Render locally with the
  same minor version, or a check that passes on your machine may not match
  what Argo CD renders.
- **Names come from the release namespace.** Argo CD sets it from the
  Application's destination, and no value file can change it. The chart
  refuses to render if `system.name` differs from it.
- **The Jobs' names end in a hash of their own spec.** A Job's pod template
  cannot change in place, so any change produces a new Job, Argo CD prunes the
  old, finished one, and the new one runs. Both scripts are safe to repeat.
- **No private IP in the ConfigMap.** A render from git cannot know it. The
  GRANT Job looks it up from the Cloud SQL Admin API when it runs, and
  applications connect through the Cloud SQL Auth Proxy by connection name.
- **Fields left unset on purpose.** Config Connector copies the zone and disk
  size from a live instance it adopts, ignores an unset backup start time, and
  defaults retention to seven backups. Every other field it would default is
  stated, because its defaults include a public IP address
  (v1.156.0 `sqlinstance_defaults.go`). If a field ever has to be left to the
  live instance, the `cnrm.cloud.google.com/unmanaged` annotation accepts only
  a fixed list of paths; `availabilityType`, `tier` and `databaseFlags` are not
  on it, and `spec.settings` as a whole is, which would also freeze
  `activationPolicy` and break `cycle.sh park` (v1.156.0
  `sqlinstance_controller.go`, `supportedUnmanageableFields`).
- **The IAM database user name has a limit.** Postgres user names are at most
  63 bytes. `<system>@<project>.iam` stays inside that for a 30-character
  System name only while the project ID is 28 characters or fewer.

## After a rebuild

The `<claim>-admin` Secret lives in the cluster, and the cluster is rebuilt
between sessions. After a rebuild, the seed Job creates a new random password,
and nothing tells Cloud SQL. The Secret then no longer opens the `postgres`
role on an instance that already existed (ADR-0016 §4, carried by ADR-0017 §8).

The GRANT Job does not depend on it: it asks the application's own identity
first, and the grant survives in Postgres. The break-glass path does:

```sh
gcloud sql users set-password postgres --instance=<system>-<claim> \
  --password="$(kubectl -n <system> get secret <claim>-admin -o jsonpath='{.data.password}' | base64 -d)"
```

A durable fix is tracked in platform-bootstrap#23.
