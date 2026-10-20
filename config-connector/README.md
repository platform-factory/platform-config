# config-connector

The cloud engine from M2b on (ADR-0017): Config Connector, which turns the
objects `charts/system` and `charts/claims` render into Google Cloud
resources.

Two files, both applied by `apps/config-connector.yaml`:

| File | What it is |
|---|---|
| `configconnector-operator.yaml` | the operator, **vendored unmodified** from the version-pinned release bundle |
| `configconnector.yaml` | the one `ConfigConnector` object: cluster mode, one Google service account, `stateIntoSpec: Absent` |

The operator installs Config Connector's own CRDs and controllers when it sees
the `ConfigConnector` object. Its images come from `gcr.io/gke-release`, on the
Google-API path, with no Artifact Registry remote in between: the one widened
exception to ADR-0010 that ADR-0017 records.

## The vendored bundle

| Version | Source | Checked | Vendored |
|---|---|---|---|
| 1.156.0 | https://storage.googleapis.com/configconnector-operator/1.156.0/release-bundle.tar.gz | the bundle's MD5, `241447d9638349f1060445ddee0bb7b8`, matches the hash Google Cloud Storage publishes for the object (`x-goog-hash`); bundle sha256 `1ccad0867c46d965d0dc6f2c73ff65e9970e46459494c62ff009c4485b6ecbbc` | 2026-09-27 |

`configconnector-operator.yaml` is `operator-system/configconnector-operator.yaml`
from that bundle (sha256 `674ea70d8d191fb0998df277f0c14fb9c7a3347b900bf6191aa7b02fc6a6a443`).

Never `latest`, and never edited in place. Config Connector in cluster mode
has no in-place downgrade (ADR-0017 §12), so a version change is a reviewed PR
that replaces this file, re-traces the permission table in `platform-roles`
(ADR-0018, Consequences), and re-runs the desk rehearsal.

## Upgrading

1. Download the new release bundle, check its hash against the one Google
   publishes, and record both here.
2. Replace `configconnector-operator.yaml` with the bundle's
   `operator-system/configconnector-operator.yaml`.
3. Re-trace the engine's calls against `platform-roles`, and fetch the new
   version's CRDs for the charts' dry run.
