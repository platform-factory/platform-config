# Vendored charts

Charts here are copied from upstream, unmodified, and rendered by Argo CD
straight from this repo. They are not fetched from a chart repository at
sync time.

Why: the cluster's one allowed internet egress is Argo CD's git traffic to
GitHub (C-23, ADR-0010). A chart repository is a second internet endpoint
the repo-server would reach over Cloud NAT. Vendoring keeps the pinhole
singular, and makes a chart bump a reviewable PR — the same path every other
platform change takes. (Crossplane does not publish its chart as an OCI
artifact on a registry the Artifact Registry remotes front; if that changes,
an OCI source through the `ghcr-io` remote is the alternative.)

| Chart | Version | Source | sha256 of the tarball | Vendored |
|---|---|---|---|---|
| `crossplane` | 2.3.5 (app v2.3.5) | https://charts.crossplane.io/stable/crossplane-2.3.5.tgz | `ad9b5005c9b9885ed7c7db712c1fa78643892782d5b3120aa6e101567d3fb698` | 2026-08-27 |
| `kyverno` | 3.9.1 (app v1.19.1) | https://kyverno.github.io/kyverno/kyverno-3.9.1.tgz | `7b7fe51a431b5b133b0ce7eb9dcb222b6a37fb967c163223cf480054ad14d752` | 2026-09-16 |

Kyverno publishes no chart tarball as a GitHub release asset; the gh-pages
Helm repository above is upstream's own publishing channel, and the sha256
recorded here is the `digest` its `index.yaml` advertises, re-computed
locally from the download.

The Kyverno chart declares five dependencies, three of them from other Helm
repositories (`kyverno-api`, `openreports`, `reports-server`). All five are
already packaged inside the 3.9.1 tarball under `kyverno/charts/`, and Argo
CD renders a chart directory as-is without running `helm dependency update`,
so vendoring stays genuinely offline. Three of those subcharts
(`grafana`, `openreports`, `reports-server`) are disabled by default and
render nothing. Kyverno *does* publish its chart as an OCI artifact
(`ghcr.io/kyverno/charts/kyverno:3.9.1`), which the `ghcr-io` remote fronts —
but whether an Artifact Registry remote proxies a non-image OCI artifact is
untested, so vendoring avoids the question.

**Check this at the next Kyverno bump.** Chart 3.9.1's own `values.yaml`
(lines 111-114, read 2026-09-16) says the legacy policy types in the
`kyverno.io` group — `ClusterPolicy`, `Policy`, `ClusterCleanupPolicy`,
`CleanupPolicy`, `PolicyException` — "are deprecated and will be removed in a
future release", pointing at the `policies.kyverno.io` types instead. Both
policies in `kyverno/policies/` are `kyverno.io/v1 ClusterPolicy`, which is
fully functional at app v1.19.1 and chosen deliberately (the reason is in the
header comment of `kyverno/policies/deny-raw-managed-resources.yaml`). Because
the upgrade procedure below replaces the whole chart directory, a release that
finally drops those CRDs would show up as a wave-3 sync failure rather than as
a line in the diff — so read the release notes for a removal schedule before
bumping.

## Upgrading a chart

1. Download the new tarball and record its sha256.
2. Delete the chart directory and extract the tarball in its place. Never
   edit files inside a vendored chart — overrides belong in the owning
   Application's `helm.valuesObject`, where the diff is visible.
3. Update the table above and open a PR. The diff is the upgrade review.
