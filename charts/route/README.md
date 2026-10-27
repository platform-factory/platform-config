# charts/route

One route file from the `edge-config` repo, rendered into the System's
namespace (ADR-0021 §2, §3).

## Why it is shaped this way

A route is one file, `edge-config/<exposure>/<system>/<route>.yaml`, with four
fields: `host`, `service`, `port` and optionally `path`. There is no `exposure`
field. The folder says how exposed the route is, and the platform reads the
folder: the two ApplicationSets in `apps/edge-routes.yaml` write
`platform.exposure`, `platform.system` and `platform.route` into each
Application's `helm.valuesObject`, which outranks every value file (ADR-0017
§4). The file therefore cannot set what the folder says, and the
internal-to-external flip is one `git mv`.

The schema closes the file's top level to the four fields. The platform's keys
sit under `platform`, beside `environment`, because Helm merges value files key
by key and the schema sees only the merged tree: a top-level `exposure` that the
platform also wrote would be overridden in silence. Nested under `platform`, a
top-level `exposure`, `system` or `route` is an unknown key and is refused. A
file that sets `platform.exposure` itself is not refused but overridden; the
checks prove that it changes nothing.

## The inputs, in order

1. The route file (`host`, `service`, `port`, `path`).
2. `environments/<env>.yaml` from this repo: `environment:`, the facts. The
   last value file, so no route file can replace one.
3. `helm.valuesObject`: `platform:` (exposure, system, route).

## What a route renders

| Object | Where | Notes |
|---|---|---|
| `HTTPRoute` `<route>-<exposure>` | the System's namespace | attached to Gateway `external` or `internal` in namespace `edge`; hostname `<host>.<zone>` from `environment.externalZone` or `environment.internalZone` |
| `GCPBackendPolicy` `<service>` | the System's namespace, external routes only | `spec.default.securityPolicy: edge-external`; one per Service, which GKE allows (ADR-0021 §7) |

Every object carries the System label (`platform.thecloudgeek.io/system`) and
`platform.thecloudgeek.io/route` and `/exposure`. The team, tier and security
tier labels the other charts carry are not here: the ApplicationSets read the
route file, not the tenant file.

Nothing renders a Namespace, so removing a route file removes its
`HTTPRoute` and policy and nothing else. This is the one stated exception to
ADR-0019's rule (ADR-0021 §3).
