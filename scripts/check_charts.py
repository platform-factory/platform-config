#!/usr/bin/env python3
"""Checks for charts/system and charts/claims, run on every pull request.

Why it exists
-------------
From M2b, a tenant's cloud resources come from two Helm charts that only the
platform renders (ADR-0017), and from M3a a tenant's routes come from a third,
charts/route (ADR-0021 §2). The claims chart's schema is the boundary between
a tenant and the platform, so a loose schema or a template slip is a hole in
it. The API server no longer checks a tenant's form; `helm template` does, at
Argo CD's render and here. This script checks the charts the way Argo CD will
render them, and proves each refusal actually refuses (ADR-0019 §4).

It uses `helm template` only. `helm lint` reports a failed `required` or `fail`
only as information (ADR-0017 §6), so it is run last, with the real value files,
as an extra check and never as the gate.

What it checks
--------------
  1. The four charts agree with the environment file: the same `environment`
     schema in all of them, every key of environments/<env>.yaml required, and
     no value in any chart's values.yaml.
  2. Nothing in charts/ or apps/ turns a gate off: no skipSchemaValidation,
     no allowEmpty (ADR-0017 §6, ADR-0019 §2).
  3. Every case in tests/charts/cases.yaml: each must-fail case is refused,
     with the text it expects; each must-pass case renders.
  4. Every environment key, deleted and then blanked, stops all four charts, and
     the refusal names the key (ADR-0017 §8).
  5. A value a claims.yaml sets under `system:` or `environment:` changes
     nothing: the render is byte-identical with and without it, for every key
     (C-28 (b2)).
  6. Every real tenant file in the systems repo renders with the real
     environment file, and what it renders keeps the guards: the Namespace
     carries Prune=false (ADR-0019 §1), every Config Connector object carries
     its three annotations (ADR-0017 §8), every durable one the abandon
     annotation (ADR-0017 §9), and the claims Application's valuesObject
     writes every environment key (ADR-0017 §4).
  7. svc-hello's database renders what is live on svc-hello-main, field by
     field, so the engine's first update does not change the instance
     (ADR-0017 §9).
  8. The root app-of-apps: the tenants' ApplicationSet keeps its guards
     (ADR-0019 §1); every source that reads this repo names the same revision
     as environment.platformRevision; on any branch but main that is not
     main, and on main it is main (ADR-0017 §12); the engine's service
     account is a reserved System name.
  9. The reality gate refuses an edit as well as a create and a delete:
     every Kyverno rule whose deny has no conditions sets
     allowExistingViolations to false (ADR-0017 §7).
 10. A team cannot write Kyverno policy, by git or by kubectl: every real
     tenant's AppProject refuses both Kyverno policy groups, and the Kyverno
     chart, rendered with apps/kyverno.yaml's values, aggregates no role into
     `admin`. The second half first proves it can fail: with the chart's
     default it must find the roles it then finds absent.
 11. charts/route refuses what ADR-0021 §2 says it refuses, and renders what
     §3 says it renders: every must-fail route case (a top-level `exposure`,
     `system` or `route`, an unknown key, a non-integer port, a host outside
     the grammar) is in tests/charts/cases.yaml; the render is an HTTPRoute
     attached to the folder's Gateway with the folder's zone, plus a
     GCPBackendPolicy for an external route only, every object in the
     System's namespace with the System label; and a route file that sets
     `platform.*` or `environment.*` changes nothing.
 12. The edge Applications (apps/edge-routes.yaml): the two ApplicationSets
     read their own folder with a git-files generator and write exactly
     `platform.exposure`, `.system` and `.route` into valuesObject; the
     `edge` project's namespace whitelist is exactly HTTPRoute and
     GCPBackendPolicy; a removed route file removes its HTTPRoute (the
     finalizer is there and nothing stops a delete); and `edge` is a
     reserved System name. Each assertion first proves it can fail, on a
     copy with one thing broken.
 13. The Gateways (edge/, rendered with the environment file) are the ones
     ADR-0021 §4 describes: classes, listeners, addresses from the environment,
     the certificate map annotation, allowedRoutes by the System label key, in
     a namespace that carries no System label.
 14. The two edge policies (kyverno/policies) are the ones ADR-0021 §9
     describes: validate-only, failurePolicy Fail, allowExistingViolations
     false, the Service rule exempting nobody, the route-kind rules exempting
     exactly the identities §9 names, and the label rule comparing the label
     with the namespace; and every real tenant's AppProject refuses the same
     kinds from git.

How to run it
-------------
  HELM=/path/to/helm-3.19.x python3 scripts/check_charts.py --systems ../systems

`--systems` is a checkout of platform-factory/systems. HELM defaults to `helm`
on the PATH; use Helm 3.19.x, the version Argo CD v3.4.6 bundles.
"""

import argparse
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHARTS = REPO / "charts"
ENV_FILE = REPO / "environments" / "reference.yaml"
CASES = REPO / "tests" / "charts" / "cases.yaml"
# The platform's own edge is a chart too, but it lives in edge/ (ADR-0021 §4).
CHART_DIRS = {"edge": REPO / "edge"}
SCHEMA_CHARTS = ("system", "claims", "route", "edge")
FIXTURE_TENANTS = REPO / "tests" / "charts" / "tenants"
HELM = os.environ.get("HELM", "helm")

CNRM_GROUP = ".cnrm.cloud.google.com/"
CNRM_ANNOTATIONS = [
    "cnrm.cloud.google.com/state-into-spec",
    "cnrm.cloud.google.com/management-conflict-prevention-policy",
    "cnrm.cloud.google.com/project-id",
]
DURABLE_KINDS = {"SQLInstance", "SQLDatabase", "SQLUser", "ArtifactRegistryRepository"}
# Kyverno's two policy API groups: the legacy kinds, and the CEL kinds with
# their namespaced variants (NamespacedMutatingPolicy among them).
KYVERNO_POLICY_GROUPS = ["kyverno.io", "policies.kyverno.io"]
AGGREGATE_TO_ADMIN = "rbac.authorization.k8s.io/aggregate-to-admin"

failures = []


def report(ok, message):
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


# ---------------------------------------------------------------------------
# Rendering, the way Argo CD does it
# ---------------------------------------------------------------------------

def chart_dir(chart):
    return CHART_DIRS.get(chart, CHARTS / chart)


def helm_template(chart, release, namespace, value_files):
    """Run `helm template`. Returns (ok, stdout or stderr)."""
    command = [HELM, "template", release, str(chart_dir(chart)), "--namespace", namespace]
    for path in value_files:
        command += ["-f", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return True, result.stdout
    return False, result.stderr + result.stdout


def documents(text):
    return [d for d in yaml.safe_load_all(text) if d]


def write_yaml(folder, name, data):
    path = pathlib.Path(folder) / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def render_system(folder, tenant, retired, environment):
    """Render charts/system with the three value files, in Argo CD's order."""
    name = tenant["metadata"]["name"]
    files = [
        write_yaml(folder, "tenant.yaml", tenant),
        write_yaml(folder, "retired.yaml", {"retired": retired}),
        write_yaml(folder, "environment.yaml", environment),
    ]
    return helm_template("system", f"{name}-system", name, files)


def values_object(system_output, name):
    """The helm.valuesObject charts/system writes into the <name>-claims Application."""
    for doc in documents(system_output):
        if doc.get("kind") == "Application" and doc["metadata"]["name"] == f"{name}-claims":
            return doc["spec"]["sources"][0]["helm"]["valuesObject"]
    raise ValueError(f"no {name}-claims Application in the charts/system output")


def render_claims(folder, name, claims, valuesobject, namespace=None):
    """Render charts/claims: the service's claims.yaml, then the valuesObject,
    which wins, as it does under Argo CD (ADR-0017 §4)."""
    files = []
    if claims is not None:
        files.append(write_yaml(folder, "claims.yaml", claims))
    files.append(write_yaml(folder, "valuesobject.yaml", valuesobject))
    return helm_template("claims", f"{name}-claims", namespace or name, files)


ROUTE_PLATFORM = {"exposure": "external", "system": "svc-hello", "route": "hello"}
ROUTE_FILE = {"host": "hello", "service": "svc-hello", "port": 80}


def render_route(folder, route, platform, environment, namespace=None):
    """Render charts/route the way the edge ApplicationSets make Argo CD do it:
    the route file, then the environment file (the last value file), then the
    platform's valuesObject, which wins (ADR-0021 §2)."""
    files = [
        write_yaml(folder, "route.yaml", route),
        write_yaml(folder, "environment.yaml", environment),
        write_yaml(folder, "valuesobject.yaml", {"platform": platform}),
    ]
    return helm_template("route", "route", namespace or platform["system"], files)


def render_edge(folder, environment):
    """Render edge/ the way apps/edge.yaml makes Argo CD do it: the environment
    file is its only value file."""
    return helm_template("edge", "edge", "edge", [write_yaml(folder, "environment.yaml", environment)])


def load_env():
    return yaml.safe_load(ENV_FILE.read_text())


def load_fixture_tenant(name):
    return yaml.safe_load((FIXTURE_TENANTS / f"{name}.yaml").read_text())


def deep_merge(base, override):
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def run_case(case, environment):
    """Render one case from cases.yaml. Returns (ok, output)."""
    if case["chart"] == "route":
        platform = deep_merge(ROUTE_PLATFORM, case.get("platform", {}))
        with tempfile.TemporaryDirectory() as folder:
            return render_route(folder, case.get("route", ROUTE_FILE), platform, environment, case.get("namespace"))
    with tempfile.TemporaryDirectory() as folder:
        tenant = load_fixture_tenant(case.get("tenant", "standard-svc"))
        if "tenant_file" in case:
            tenant = deep_merge(tenant, case["tenant_file"])
        retired = case.get("retired", [])
        ok, output = render_system(folder, tenant, retired, environment)
        if case["chart"] == "system" or not ok:
            return ok, output
        name = tenant["metadata"]["name"]
        return render_claims(folder, name, case.get("claims"), values_object(output, name), case.get("namespace"))


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_consistency(environment):
    schemas = {c: json.loads((chart_dir(c) / "values.schema.json").read_text()) for c in SCHEMA_CHARTS}
    env_schema = schemas["claims"]["$defs"]["environment"]
    report(
        all(s["$defs"]["environment"] == env_schema for s in schemas.values()),
        "all four charts carry the same environment schema",
    )
    keys = set(environment["environment"])
    report(set(env_schema["required"]) == keys, "every key of environments/reference.yaml is required by the schema, and no other")
    report(set(env_schema["properties"]) == keys, "the environment schema lists exactly the environment file's keys")
    for chart in SCHEMA_CHARTS:
        values = yaml.safe_load((chart_dir(chart) / "values.yaml").read_text())
        report(not values, f"{chart_dir(chart).relative_to(REPO)}/values.yaml carries no value")


def check_no_off_switches():
    found = []
    for folder in (CHARTS / "system", CHARTS / "claims", CHARTS / "route", REPO / "edge", REPO / "apps"):
        for path in sorted(folder.rglob("*")) if folder.exists() else []:
            if path.is_file() and path.suffix in {".yaml", ".tpl", ".json"}:
                text = path.read_text()
                found += [f"{path.relative_to(REPO)}: {word}" for word in ("skipSchemaValidation", "allowEmpty") if word in text]
    report(not found, "no chart or Application turns off schema validation or allows pruning to empty" + (f": {found}" if found else ""))


def check_cases(environment):
    cases = yaml.safe_load(CASES.read_text())
    for case in cases["must_fail"]:
        ok, output = run_case(case, environment)
        if ok:
            report(False, f"must-fail '{case['name']}' rendered; it should have been refused")
        elif case["expect"] not in output:
            report(False, f"must-fail '{case['name']}' was refused, but not with {case['expect']!r}:\n{output.strip()}")
        else:
            report(True, f"must-fail '{case['name']}' refused ({case['expect']})")
    for case in cases["must_pass"]:
        ok, output = run_case(case, environment)
        if not ok:
            report(False, f"must-pass '{case['name']}' was refused:\n{output.strip()}")
            continue
        report(True, f"must-pass '{case['name']}' rendered")
        if case["chart"] == "route":
            report(any(d["kind"] == "HTTPRoute" for d in documents(output)), f"  '{case['name']}' rendered an HTTPRoute")
        # ADR-0019 §2: no databases means an empty render, so Argo CD's guard
        # against pruning to empty applies.
        if case["chart"] == "claims" and not (case.get("claims") or {}).get("databases"):
            report(not documents(output), f"  '{case['name']}' rendered no object at all")


def check_environment_keys(environment):
    """Delete, then blank, each environment key: every chart must stop, naming it."""
    tenant = load_fixture_tenant("standard-svc")
    with tempfile.TemporaryDirectory() as folder:
        ok, output = render_system(folder, tenant, [], environment)
        good_values = values_object(output, "standard-svc")
    for key in environment["environment"]:
        for how in ("deleted", "blanked"):
            env = copy.deepcopy(environment)
            vo = copy.deepcopy(good_values)
            if how == "deleted":
                del env["environment"][key]
                del vo["environment"][key]
            else:
                env["environment"][key] = ""
                vo["environment"][key] = ""
            with tempfile.TemporaryDirectory() as folder:
                ok_s, out_s = render_system(folder, tenant, [], env)
                ok_c, out_c = render_claims(folder, "standard-svc", None, vo)
                ok_r, out_r = render_route(folder, ROUTE_FILE, ROUTE_PLATFORM, env, "svc-hello")
                ok_e, out_e = render_edge(folder, env)
            report(
                not ok_s and key in out_s and not ok_c and key in out_c and not ok_r and key in out_r and not ok_e and key in out_e,
                f"environment.{key} {how}: all four charts refuse, naming it",
            )


def check_tenant_values_change_nothing(environment):
    """C-28 (b2): whatever a claims.yaml says under system: or environment:, the
    platform's valuesObject replaces it, so the render is byte-identical."""
    tenant = load_fixture_tenant("standard-svc")
    database = {"main": {"engine": "POSTGRES_16", "region": "us-central1", "size": "S"}}
    with tempfile.TemporaryDirectory() as folder:
        ok, output = render_system(folder, tenant, [], environment)
        vo = values_object(output, "standard-svc")
        ok, baseline = render_claims(folder, "standard-svc", {"databases": database}, vo)
    report(ok, "baseline claims render for the (b2) comparison")
    attempts = {("environment", key): f"tenant-supplied-{i}" for i, key in enumerate(environment["environment"])}
    attempts.update({("system", "name"): "another-svc", ("system", "team"): "team-z", ("system", "tier"): "critical"})
    for (section, key), value in attempts.items():
        with tempfile.TemporaryDirectory() as folder:
            claims = {"databases": database, section: {key: value}}
            ok, output = render_claims(folder, "standard-svc", claims, vo)
        report(ok and output == baseline, f"a claims.yaml that sets {section}.{key} changes nothing")


def check_real_tenants(systems, environment):
    retired = yaml.safe_load((systems / "retired.yaml").read_text())["retired"]
    env_keys = set(environment["environment"])
    tenants = sorted((systems / "tenants").glob("*.yaml"))
    report(bool(tenants), f"found {len(tenants)} tenant file(s) in {systems / 'tenants'}")
    for path in tenants:
        tenant = yaml.safe_load(path.read_text())
        name = tenant["metadata"]["name"]
        with tempfile.TemporaryDirectory() as folder:
            ok, output = render_system(folder, tenant, retired, environment)
        if not ok:
            report(False, f"tenants/{path.name} does not render:\n{output.strip()}")
            continue
        report(True, f"tenants/{path.name} renders with the real environment file")
        objects = documents(output)
        namespace = [o for o in objects if o["kind"] == "Namespace"]
        report(
            len(namespace) == 1 and namespace[0]["metadata"]["annotations"].get("argocd.argoproj.io/sync-options") == "Prune=false",
            f"  {name}: its Namespace carries Prune=false",
        )
        check_cnrm_annotations(objects, name)
        report(set(values_object(output, name)["environment"]) == env_keys, f"  {name}: the claims Application's valuesObject writes every environment key")
        check_no_policy_from_git(objects, name)
        check_no_edge_from_git(objects, name)


def check_no_policy_from_git(objects, label):
    """The git half of item 10: the tenant's AppProject refuses every kind in
    both Kyverno policy groups, so its own Application cannot apply one."""
    projects = [o for o in objects if o["kind"] == "AppProject"]
    blacklist = set()
    if len(projects) == 1:
        blacklist = {(e.get("group"), e.get("kind")) for e in projects[0]["spec"].get("namespaceResourceBlacklist", [])}
    for group in KYVERNO_POLICY_GROUPS:
        report((group, "*") in blacklist, f"  {label}: its AppProject refuses every {group} kind from git")


def check_cnrm_annotations(objects, label):
    for obj in objects:
        if CNRM_GROUP not in obj.get("apiVersion", "") + "/":
            continue
        annotations = obj["metadata"].get("annotations", {})
        missing = [a for a in CNRM_ANNOTATIONS if a not in annotations]
        if obj["kind"] in DURABLE_KINDS and annotations.get("cnrm.cloud.google.com/deletion-policy") != "abandon":
            missing.append("cnrm.cloud.google.com/deletion-policy: abandon")
        report(not missing, f"  {label}: {obj['kind']} {obj['metadata']['name']} carries its annotations" + (f" (missing {missing})" if missing else ""))


def check_svc_hello_matches_live(systems, environment):
    """ADR-0017 §9: the chart must render what is live, or the engine's first
    update changes the instance. The live values below were read with
    `gcloud sql instances describe svc-hello-main` on 2026-09-22 (ADR-0017 §5).
    The full field-by-field comparison with the live instance is a step of the
    first cluster session; this pins the fields the chart sets.

    Two differences from what is live are intended, so the first update is
    not empty: the chart states activationPolicy ALWAYS, which wakes a parked
    instance (ADR-0017 §12), and it leaves out the old engine's `managed-by`
    label (ADR-0017 §8)."""
    tenant = yaml.safe_load((systems / "tenants" / "svc-hello.yaml").read_text())
    claims = {"databases": {"main": {"engine": "POSTGRES_16", "region": "us-central1", "size": "S", "tier": "standard", "backups": True}}}
    with tempfile.TemporaryDirectory() as folder:
        ok, output = render_system(folder, tenant, [], environment)
        ok, output = render_claims(folder, "svc-hello", claims, values_object(output, "svc-hello"))
    if not ok:
        report(False, f"svc-hello's database does not render:\n{output}")
        return
    objects = {o["kind"]: o for o in documents(output)}
    check_cnrm_annotations(list(objects.values()), "svc-hello's database")
    instance = objects["SQLInstance"]
    settings = instance["spec"]["settings"]
    env = environment["environment"]
    # The live instance carries one label more than the chart renders:
    # `managed-by: crossplane`, stamped by the old engine. The new engine's
    # first update removes it, on purpose. The row below compares the other
    # labels and says so, rather than calling the chart's two labels "live".
    live_labels = {"system": "svc-hello", "database": "main", "managed-by": "crossplane"}
    kept_labels = {key: value for key, value in live_labels.items() if key != "managed-by"}
    expected = {
        "instance name": (instance["spec"]["resourceID"], "svc-hello-main"),
        "databaseVersion": (instance["spec"]["databaseVersion"], "POSTGRES_16"),
        "region": (instance["spec"]["region"], "us-central1"),
        "tier": (settings["tier"], "db-f1-micro"),
        "edition": (settings["edition"], "ENTERPRISE"),
        "availabilityType": (settings["availabilityType"], "ZONAL"),
        "backups": (settings["backupConfiguration"]["enabled"], True),
        "point-in-time recovery": (settings["backupConfiguration"]["pointInTimeRecoveryEnabled"], False),
        "public IPv4": (settings["ipConfiguration"]["ipv4Enabled"], False),
        "private network": (settings["ipConfiguration"]["privateNetworkRef"]["external"], env["network"]),
        "deletion protection": (settings["deletionProtectionEnabled"], True),
        "IAM login flag": (settings["databaseFlags"], [{"name": "cloudsql.iam_authentication", "value": "on"}]),
        "cloud labels, all but managed-by (live has it; the engine swap removes it)": ({k: v for k, v in instance["metadata"]["labels"].items() if "/" not in k}, kept_labels),
        "database name": (objects["SQLDatabase"]["spec"]["resourceID"], "app"),
        "database deletionPolicy": (objects["SQLDatabase"]["spec"]["deletionPolicy"], "ABANDON"),
        "IAM user": (objects["SQLUser"]["spec"]["resourceID"], f"svc-hello@{env['projectID']}.iam"),
        "IAM user type": (objects["SQLUser"]["spec"]["type"], "CLOUD_IAM_SERVICE_ACCOUNT"),
    }
    for field, (rendered, live) in expected.items():
        report(rendered == live, f"  svc-hello-main {field}: rendered {rendered!r}, live {live!r}")


def platform_config_sources(document):
    """Every source in an Application or ApplicationSet that reads this repo."""
    if document["kind"] not in ("Application", "ApplicationSet"):
        return []
    spec = document["spec"]
    if document["kind"] == "ApplicationSet":
        spec = spec["template"]["spec"]
    sources = spec.get("sources") or [spec["source"]]
    return [s for s in sources if s["repoURL"].rstrip("/").endswith("/platform-config")]


def check_apps(environment, branch):
    """The root app-of-apps: the ApplicationSet's guards (ADR-0019 §1), one
    revision for every read of this repo (ADR-0017 §12), and the engine's
    service account."""
    # A file may hold several documents (apps/edge-routes.yaml holds the edge
    # project and its two ApplicationSets).
    apps = {p.name: documents(p.read_text()) for p in sorted((REPO / "apps").glob("*.yaml"))}
    revision = environment["environment"]["platformRevision"]

    appset = (apps.get("systems.yaml") or [{}])[0]
    annotations = appset.get("metadata", {}).get("annotations", {})
    sync_options = {o.strip() for o in annotations.get("argocd.argoproj.io/sync-options", "").split(",")}
    report(appset.get("kind") == "ApplicationSet", "apps/systems.yaml is the tenants' ApplicationSet")
    report(appset.get("spec", {}).get("syncPolicy", {}).get("applicationsSync") == "create-update", "  it never deletes an Application (applicationsSync: create-update)")
    report({"Prune=false", "Delete=false"} <= sync_options, "  git cannot prune or delete it (Prune=false,Delete=false)")
    report("resources-finalizer.argocd.argoproj.io" in appset.get("metadata", {}).get("finalizers", []), "  a background kubectl delete keeps its Applications (its own finalizer)")
    report("missingkey=error" in appset.get("spec", {}).get("goTemplateOptions", []), "  a missing field is an error (missingkey=error)")

    for name, docs in apps.items():
        for document in docs:
            for source in platform_config_sources(document):
                report(
                    source["targetRevision"] == revision,
                    f"apps/{name} reads platform-config at {source['targetRevision']!r}, the same revision as environment.platformRevision ({revision!r})",
                )
    # Both directions. On a branch, a source that says main would render
    # main's charts. On main, a source that still names the branch means the
    # flip at merge was forgotten: everything keeps working until the branch
    # is deleted, and then every Application breaks at once.
    if branch == "main":
        report(revision == "main", f"on main, every platform-config source says main, not {revision!r} (ADR-0017 §12: they flip in the PR that merges the branch)")
    elif branch:
        report(revision != "main", f"on branch {branch!r}, no platform-config source says main (ADR-0017 §12)")

    configconnector = yaml.safe_load((REPO / "config-connector" / "configconnector.yaml").read_text())
    engine = configconnector["spec"]["googleServiceAccount"]
    project = environment["environment"]["projectID"]
    report(engine == f"config-connector@{project}.iam.gserviceaccount.com", f"the engine's service account is config-connector in {project}")
    reserved = json.loads((CHARTS / "system" / "values.schema.json").read_text())["$defs"]["systemName"]["allOf"][2]["not"]["enum"]
    report(engine.split("@")[0] in reserved, "  and its name is a reserved System name")


# ---------------------------------------------------------------------------
# M3a: the route chart and the edge Applications (ADR-0021 §2, §3)
# ---------------------------------------------------------------------------

EDGE_KINDS = {("gateway.networking.k8s.io", "HTTPRoute"), ("networking.gke.io", "GCPBackendPolicy")}
SYSTEM_LABEL = "platform.thecloudgeek.io/system"
PLATFORM_KEYS = {"exposure", "system", "route"}
SEGMENT_SYSTEM = "{{ index .path.segments 1 }}"
SEGMENT_ROUTE = '{{ .path.filename | trimSuffix ".yaml" }}'


def route_problems(objects, exposure, namespace, host, zones):
    """What charts/route must render for one route (ADR-0021 §3). Returns the
    problems found; none means the render is right."""
    problems = []
    kinds = sorted(o["kind"] for o in objects)
    want = ["GCPBackendPolicy", "HTTPRoute"] if exposure == "external" else ["HTTPRoute"]
    if kinds != want:
        problems.append(f"renders {kinds}, expected {want}")
    for o in objects:
        if o["metadata"].get("namespace") != namespace:
            problems.append(f"{o['kind']} is in {o['metadata'].get('namespace')!r}, not the System's namespace {namespace!r}")
        if o["metadata"].get("labels", {}).get(SYSTEM_LABEL) != namespace:
            problems.append(f"{o['kind']} does not carry the System label {namespace!r}")
        if (o["apiVersion"], o["kind"]) not in {("gateway.networking.k8s.io/v1", "HTTPRoute"), ("networking.gke.io/v1", "GCPBackendPolicy")}:
            problems.append(f"{o['kind']} has apiVersion {o['apiVersion']}")
    for o in objects:
        if o["kind"] == "HTTPRoute":
            parent = o["spec"]["parentRefs"]
            if [(r.get("kind"), r["name"], r.get("namespace")) for r in parent] != [("Gateway", exposure, "edge")]:
                problems.append(f"HTTPRoute is attached to {parent}, expected Gateway {exposure} in edge")
            if o["spec"]["hostnames"] != [f"{host}.{zones[exposure]}"]:
                problems.append(f"HTTPRoute hostnames are {o['spec']['hostnames']}, expected {host}.{zones[exposure]}")
        if o["kind"] == "GCPBackendPolicy":
            if o["spec"]["default"].get("securityPolicy") != "edge-external":
                problems.append("GCPBackendPolicy does not carry securityPolicy edge-external")
            if o["spec"]["targetRef"].get("kind") != "Service" or o["spec"]["targetRef"].get("name") != o["metadata"]["name"]:
                problems.append("GCPBackendPolicy is not named after the Service it targets")
    return problems


def check_route_render(environment):
    """A route renders what ADR-0021 §3 says. The checker first proves it can
    fail: each mutation below breaks one thing in a good render."""
    env = environment["environment"]
    zones = {"external": env["externalZone"], "internal": env["internalZone"]}
    renders = {}
    for exposure in ("external", "internal"):
        with tempfile.TemporaryDirectory() as folder:
            ok, output = render_route(folder, ROUTE_FILE, {**ROUTE_PLATFORM, "exposure": exposure}, environment)
        report(ok, f"{'an' if exposure == 'external' else 'an'} {exposure} route renders")
        if not ok:
            return
        renders[exposure] = documents(output)
        found = route_problems(renders[exposure], exposure, "svc-hello", "hello", zones)
        report(not found, f"  the {exposure} route is an HTTPRoute on the {exposure} Gateway at hello.{zones[exposure]}, in the System's namespace with its label" + (f": {found}" if found else ""))
    report(
        {o["kind"] for o in renders["internal"]} == {"HTTPRoute"},
        "  an internal route has no GCPBackendPolicy; an external one has exactly one, named after the Service",
    )

    def mutate(objects, edit):
        copied = copy.deepcopy(objects)
        edit(copied)
        return copied

    mutations = [
        ("external", "an external route without its policy", lambda os: os.pop(0 if os[0]["kind"] == "GCPBackendPolicy" else 1)),
        ("internal", "an internal route with a policy", lambda os: os.append({**copy.deepcopy(renders["external"][0]), "kind": "GCPBackendPolicy", "apiVersion": "networking.gke.io/v1"})),
        ("external", "a route attached to the other Gateway", lambda os: next(o for o in os if o["kind"] == "HTTPRoute")["spec"]["parentRefs"][0].update(name="internal")),
        ("internal", "a route with the other zone's hostname", lambda os: os[0]["spec"].update(hostnames=[f"hello.{zones['external']}"])),
        ("external", "an object without the System label", lambda os: os[0]["metadata"]["labels"].pop(SYSTEM_LABEL)),
        ("external", "an object in another namespace", lambda os: os[0]["metadata"].update(namespace="edge")),
        ("external", "a policy with another Cloud Armor policy", lambda os: next(o for o in os if o["kind"] == "GCPBackendPolicy")["spec"]["default"].update(securityPolicy="other")),
    ]
    for exposure, what, edit in mutations:
        found = route_problems(mutate(renders[exposure], edit), exposure, "svc-hello", "hello", zones)
        report(bool(found), f"  the render check catches {what}")


def check_route_file_cannot_set_platform(environment):
    """The route file sits below the platform's valuesObject and the environment
    file, so what it says under `platform` or `environment` changes nothing.
    (A KEY the platform does not write is another matter: it is refused, and
    the must-fail cases say so. Top-level `exposure`, `system` and `route` are
    refused for that reason.)"""
    with tempfile.TemporaryDirectory() as folder:
        ok, baseline = render_route(folder, ROUTE_FILE, ROUTE_PLATFORM, environment)
    report(ok, "baseline route render for the comparison")
    env = environment["environment"]
    attempts = {("platform", "exposure"): "internal", ("platform", "system"): "another-svc", ("platform", "route"): "other"}
    attempts.update({("environment", key): f"tenant-supplied-{i}.example.com" for i, key in enumerate(env)})
    for (section, key), value in attempts.items():
        with tempfile.TemporaryDirectory() as folder:
            ok, output = render_route(folder, {**ROUTE_FILE, section: {key: value}}, ROUTE_PLATFORM, environment)
        report(ok and output == baseline, f"a route file that sets {section}.{key} changes nothing")


def edge_problems(docs, systems_wave, reserved):
    """The edge Applications, as ADR-0021 §2 and §3 describe them. Returns the
    problems found; none means they are right."""
    problems = []
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    project = by.get(("AppProject", "edge"))
    if project is None:
        problems.append("no AppProject named edge")
    else:
        spec = project["spec"]
        whitelist = {(e.get("group"), e.get("kind")) for e in spec.get("namespaceResourceWhitelist") or []}
        if whitelist != EDGE_KINDS:
            problems.append(f"the edge project's namespaceResourceWhitelist is {sorted(whitelist)}, expected exactly HTTPRoute and GCPBackendPolicy")
        if "namespaceResourceBlacklist" in spec or "clusterResourceWhitelist" in spec:
            problems.append("the edge project carries a blacklist or a cluster-scoped whitelist; it is a namespaced whitelist and nothing else")
        repos = sorted(r.rstrip("/").rsplit("/", 1)[-1] for r in spec.get("sourceRepos", []))
        if repos != ["edge-config", "platform-config"]:
            problems.append(f"the edge project's sources are {repos}, expected edge-config and platform-config")
    for exposure in ("external", "internal"):
        appset = by.get(("ApplicationSet", f"edge-{exposure}"))
        if appset is None:
            problems.append(f"no ApplicationSet named edge-{exposure}")
            continue
        spec = appset["spec"]
        where = f"edge-{exposure}"
        generators = spec.get("generators", [])
        files = [f.get("path") for g in generators for f in (g.get("git") or {}).get("files", [])]
        if len(generators) != 1 or files != [f"{exposure}/*/*.yaml"]:
            problems.append(f"{where}: the generator must be one git-files generator over {exposure}/*/*.yaml, found {files}")
        if not all(((g.get("git") or {}).get("repoURL") or "").rstrip("/").endswith("/edge-config") for g in generators):
            problems.append(f"{where}: the generator must read edge-config")
        if spec.get("goTemplate") is not True or "missingkey=error" not in spec.get("goTemplateOptions", []):
            problems.append(f"{where}: goTemplate with missingkey=error is required")
        template = spec["template"]
        if template["spec"].get("project") != "edge":
            problems.append(f"{where}: its Applications must run in the edge project")
        if "resources-finalizer.argocd.argoproj.io" not in template["metadata"].get("finalizers", []):
            problems.append(f"{where}: its Applications need the resources finalizer, or a removed route file leaves its HTTPRoute (ADR-0021 §3)")
        if spec.get("syncPolicy", {}).get("applicationsSync") in ("create-update", "create-only"):
            problems.append(f"{where}: applicationsSync must not stop deletes; a removed route file removes its Application (ADR-0021 §3)")
        sync_options = appset["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-options", "")
        if "Delete=false" in sync_options or "Prune=false" in sync_options:
            problems.append(f"{where}: the ADR-0019 §1 guard Prune=false/Delete=false is the tenants' ApplicationSet's, not this one's (ADR-0021 §3)")
        chart = [s for s in template["spec"]["sources"] if s.get("path") == "charts/route"]
        if len(chart) != 1:
            problems.append(f"{where}: exactly one source must render charts/route")
            continue
        helm = chart[0]["helm"]
        values = helm.get("valuesObject") or {}
        if set(values) != {"platform"} or set(values.get("platform", {})) != PLATFORM_KEYS:
            problems.append(f"{where}: valuesObject must write platform.exposure, .system and .route and nothing else, found {values}")
        else:
            platform = values["platform"]
            if platform["exposure"] != exposure:
                problems.append(f"{where}: platform.exposure is {platform['exposure']!r}, the folder says {exposure}")
            if platform["system"] != SEGMENT_SYSTEM or platform["route"] != SEGMENT_ROUTE:
                problems.append(f"{where}: platform.system and .route must come from the file's path")
        value_files = helm.get("valueFiles", [])
        if len(value_files) != 2 or not value_files[0].startswith("$edge/") or not value_files[1].endswith("/environments/reference.yaml"):
            problems.append(f"{where}: value files must be the route file, then the environment file last, found {value_files}")
        if template["spec"]["destination"].get("namespace") != SEGMENT_SYSTEM:
            problems.append(f"{where}: destination namespace must be the System folder")
        wave = int(appset["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", -1))
        if wave <= systems_wave:
            problems.append(f"{where}: sync wave {wave} is not after systems ({systems_wave})")
    if "edge" not in reserved:
        problems.append("edge is not a reserved System name")
    return problems


def check_edge_apps():
    apps = documents((REPO / "apps" / "edge-routes.yaml").read_text())
    systems = yaml.safe_load((REPO / "apps" / "systems.yaml").read_text())
    systems_wave = int(systems["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
    reserved = json.loads((CHARTS / "system" / "values.schema.json").read_text())["$defs"]["systemName"]["allOf"][2]["not"]["enum"]
    found = edge_problems(apps, systems_wave, reserved)
    report(not found, "apps/edge-routes.yaml: the edge project and both ApplicationSets are as ADR-0021 §2 and §3 say" + (f": {found}" if found else ""))

    def appset(docs, exposure):
        return next(d for d in docs if d["kind"] == "ApplicationSet" and d["metadata"]["name"] == f"edge-{exposure}")

    def chart_values(docs, exposure):
        return next(s for s in appset(docs, exposure)["spec"]["template"]["spec"]["sources"] if s.get("path") == "charts/route")["helm"]["valuesObject"]

    mutations = {
        "a valuesObject without platform.route": lambda d: chart_values(d, "external")["platform"].pop("route"),
        "a valuesObject with a key the folder does not give": lambda d: chart_values(d, "internal")["platform"].update(team="x"),
        "an exposure the folder does not say": lambda d: chart_values(d, "internal")["platform"].update(exposure="external"),
        "an internal generator over the external folder": lambda d: appset(d, "internal")["spec"]["generators"][0]["git"]["files"][0].update(path="external/*/*.yaml"),
        "a missing field that is not an error": lambda d: appset(d, "external")["spec"].update(goTemplateOptions=[]),
        "a project that also writes a Service": lambda d: next(x for x in d if x["kind"] == "AppProject")["spec"]["namespaceResourceWhitelist"].append({"group": "", "kind": "Service"}),
        "a project with no whitelist": lambda d: next(x for x in d if x["kind"] == "AppProject")["spec"].pop("namespaceResourceWhitelist"),
        "Applications without the finalizer": lambda d: appset(d, "external")["spec"]["template"]["metadata"].pop("finalizers"),
        "an ApplicationSet that never deletes": lambda d: appset(d, "internal")["spec"].setdefault("syncPolicy", {}).update(applicationsSync="create-update"),
        "a sync wave before the tenants": lambda d: appset(d, "external")["metadata"]["annotations"].update({"argocd.argoproj.io/sync-wave": "4"}),
    }
    for what, edit in mutations.items():
        copied = copy.deepcopy(apps)
        edit(copied)
        report(bool(edge_problems(copied, systems_wave, reserved)), f"  the edge check catches {what}")
    report(bool(edge_problems(apps, systems_wave, [n for n in reserved if n != "edge"])), "  the edge check catches `edge` missing from the reserved System names")


# ---------------------------------------------------------------------------
# M3a phase 2: the Gateways and the two edge policies (ADR-0021 §4, §9)
# ---------------------------------------------------------------------------

ARGOCD = ("argocd-application-controller", "argocd")
NAMESPACE_CONTROLLER = ("namespace-controller", "kube-system")
GARBAGE_COLLECTOR = ("generic-garbage-collector", "kube-system")
CLOUD_GROUP_KINDS = {"sql.cnrm.cloud.google.com/*/*", "iam.cnrm.cloud.google.com/*/*", "artifactregistry.cnrm.cloud.google.com/*/*"}
# The kinds ADR-0021 §9 lists. The GKE group is listed by kind, never whole.
GKE_POLICY_KINDS = ["GCPBackendPolicy", "HealthCheckPolicy", "GCPGatewayPolicy", "GCPSessionAffinityPolicy", "GCPTrafficDistributionPolicy", "LBPolicy"]
ROUTE_KIND_SELECTORS = {"networking.k8s.io/*/Ingress", "gateway.networking.k8s.io/*/*"} | {f"networking.gke.io/*/{k}" for k in GKE_POLICY_KINDS}
LABEL_KEY = "platform.thecloudgeek.io/system"


def gateway_problems(objects, environment):
    """The platform's edge, as ADR-0021 §4 describes it."""
    env = environment["environment"]
    problems = []
    namespaces = [o for o in objects if o["kind"] == "Namespace"]
    if len(namespaces) != 1 or namespaces[0]["metadata"]["name"] != "edge":
        problems.append("expected exactly one Namespace, named edge")
    elif LABEL_KEY in namespaces[0]["metadata"].get("labels", {}):
        problems.append("the edge namespace carries the System label, which makes it a tenant namespace")
    elif namespaces[0]["metadata"]["annotations"].get("argocd.argoproj.io/sync-options") != "Prune=false":
        problems.append("the edge namespace is not Prune=false (ADR-0019 §1)")
    gateways = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Gateway"}
    if sorted(gateways) != ["external", "internal"]:
        problems.append(f"expected Gateways external and internal, found {sorted(gateways)}")
        return problems
    wants = {
        "external": ("gke-l7-global-external-managed", "HTTPS", 443, env["externalAddress"]),
        "internal": ("gke-l7-rilb", "HTTP", 80, env["internalAddress"]),
    }
    for name, (cls, protocol, port, address) in wants.items():
        gateway = gateways[name]
        spec = gateway["spec"]
        if gateway["metadata"].get("namespace") != "edge":
            problems.append(f"Gateway {name} is not in the edge namespace")
        if spec["gatewayClassName"] != cls:
            problems.append(f"Gateway {name} has class {spec['gatewayClassName']}, expected {cls}")
        if [(a.get("type"), a.get("value")) for a in spec.get("addresses", [])] != [("NamedAddress", address)]:
            problems.append(f"Gateway {name} must take the NamedAddress {address!r} from the environment")
        listeners = [(l["protocol"], l["port"]) for l in spec["listeners"]]
        if listeners != [(protocol, port)]:
            problems.append(f"Gateway {name} must have one {protocol} listener on {port}, found {listeners}")
        for listener in spec["listeners"]:
            allowed = listener.get("allowedRoutes", {}).get("namespaces", {})
            expressions = allowed.get("selector", {}).get("matchExpressions", [])
            if allowed.get("from") != "Selector" or expressions != [{"key": LABEL_KEY, "operator": "Exists"}]:
                problems.append(f"Gateway {name}'s allowedRoutes must select namespaces by {LABEL_KEY} with operator Exists, nothing else")
        certmap = gateway["metadata"].get("annotations", {}).get("networking.gke.io/certmap")
        if name == "external" and certmap != "edge-external":
            problems.append("Gateway external must attach the certificate map edge-external by annotation")
        if name == "internal" and certmap is not None:
            problems.append("Gateway internal has no TLS in M3 (ADR-0021 §4) and carries no certificate map")
    return problems


def check_gateways(environment):
    with tempfile.TemporaryDirectory() as folder:
        ok, output = render_edge(folder, environment)
    report(ok, "edge/ renders with the real environment file")
    if not ok:
        print(output)
        return
    objects = documents(output)
    found = gateway_problems(objects, environment)
    report(not found, "  the namespace and the two Gateways are as ADR-0021 §4 says" + (f": {found}" if found else ""))

    def gw(os, name):
        return next(o for o in os if o["kind"] == "Gateway" and o["metadata"]["name"] == name)

    mutations = {
        "an external Gateway of the wrong class": lambda os: gw(os, "external")["spec"].update(gatewayClassName="gke-l7-rilb"),
        "an internal Gateway with HTTPS": lambda os: gw(os, "internal")["spec"]["listeners"][0].update(protocol="HTTPS", port=443),
        "an external Gateway without the certificate map": lambda os: gw(os, "external")["metadata"]["annotations"].pop("networking.gke.io/certmap"),
        "an address that is not the environment's": lambda os: gw(os, "internal")["spec"]["addresses"][0].update(value="somewhere-else"),
        "a Gateway that lets every namespace attach": lambda os: gw(os, "external")["spec"]["listeners"][0]["allowedRoutes"]["namespaces"].update(**{"from": "All"}),
        "a selector on another key": lambda os: gw(os, "internal")["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]["selector"]["matchExpressions"][0].update(key="kubernetes.io/metadata.name"),
        "an edge namespace that carries the System label": lambda os: next(o for o in os if o["kind"] == "Namespace")["metadata"].setdefault("labels", {}).update({LABEL_KEY: "edge"}),
        "a missing Gateway": lambda os: os.remove(gw(os, "internal")),
    }
    for what, edit in mutations.items():
        copied = copy.deepcopy(objects)
        edit(copied)
        report(bool(gateway_problems(copied, environment)), f"  the Gateway check catches {what}")


def _match_blocks(rule):
    return [m["resources"] for m in rule["match"]["any"]]


def edge_policy_problems(policies):
    """The two policies of ADR-0021 §9, as that section describes them. Returns
    the problems found; none means they are right."""
    problems = []
    deny_edge = policies.get("deny-edge-objects")
    label = policies.get("require-system-label")
    if deny_edge is None or label is None:
        return ["deny-edge-objects and require-system-label must both exist in kyverno/policies"]
    for name in ("deny-edge-objects", "require-system-label"):
        spec = policies[name]["spec"]
        if spec.get("webhookConfiguration", {}).get("failurePolicy") != "Fail":
            problems.append(f"{name}: failurePolicy must be Fail")
        if spec.get("validationFailureAction") != "Enforce":
            problems.append(f"{name}: validationFailureAction must be Enforce")
        for rule in spec["rules"]:
            if set(rule) & {"mutate", "generate", "verifyImages"}:
                problems.append(f"{name}, rule {rule['name']}: Kyverno here is validate-only (apps/kyverno.yaml)")
            validate = rule.get("validate") or {}
            if "deny" not in validate:
                problems.append(f"{name}, rule {rule['name']}: must be a validate deny")
            elif validate.get("allowExistingViolations") is not False:
                problems.append(f"{name}, rule {rule['name']}: allowExistingViolations must be false")
            selector = [r.get("namespaceSelector") for r in _match_blocks(rule)]
            want = {"matchExpressions": [{"key": LABEL_KEY, "operator": "Exists"}]}
            if selector != [want]:
                problems.append(f"{name}, rule {rule['name']}: must match tenant namespaces by {LABEL_KEY} Exists")

    rules = {r["name"]: r for r in deny_edge["spec"]["rules"]}
    service = rules.get("no-load-balancer-or-nodeport-services-in-tenant-namespaces")
    create = rules.get("no-edge-objects-by-hand-in-tenant-namespaces")
    delete = rules.get("no-edge-object-deletes-by-hand-in-tenant-namespaces")
    if not (service and create and delete) or len(rules) != 3:
        return problems + ["deny-edge-objects must have exactly the service, create/update and delete rules"]

    block = _match_blocks(service)[0]
    if block["kinds"] != ["Service"] or sorted(block["operations"]) != ["CREATE", "UPDATE"]:
        problems.append("the Service rule must match Service on CREATE and UPDATE")
    if "exclude" in service:
        problems.append("the Service rule exempts nobody: the tenant's own Application is Argo CD too (ADR-0021 §9)")
    conditions = service["validate"]["deny"].get("conditions", {}).get("any", [])
    if len(conditions) != 1 or sorted(conditions[0].get("value", [])) != ["LoadBalancer", "NodePort"] or "request.object.spec.type" not in conditions[0].get("key", ""):
        problems.append("the Service rule must deny exactly type LoadBalancer and NodePort")

    for rule, operations, exempt in (
        # The garbage collector on UPDATE removes the foregroundDeletion
        # finalizer an Argo CD prune leaves; refused, a removed route would stay
        # terminating (the M3 readiness note's correction of 2026-10-27).
        (create, ["CREATE", "UPDATE"], {ARGOCD, GARBAGE_COLLECTOR}),
        (delete, ["DELETE"], {ARGOCD, NAMESPACE_CONTROLLER, GARBAGE_COLLECTOR}),
    ):
        block = _match_blocks(rule)[0]
        if set(block["kinds"]) != ROUTE_KIND_SELECTORS:
            problems.append(f"rule {rule['name']}: kinds must be Ingress, gateway.networking.k8s.io/*/* and the listed GKE kinds by name, found {sorted(block['kinds'])}")
        if any(k.startswith("networking.gke.io/") and k.rsplit("/", 1)[1] in ("*", "") for k in block["kinds"]):
            problems.append(f"rule {rule['name']}: the networking.gke.io group is listed by kind, never whole (ADR-0021 §9)")
        if sorted(block["operations"]) != operations:
            problems.append(f"rule {rule['name']}: operations must be {operations}")
        subjects = {(s["name"], s["namespace"]) for e in rule.get("exclude", {}).get("any", []) for s in e.get("subjects", [])}
        if subjects != exempt or any(s.get("kind") != "ServiceAccount" for e in rule["exclude"]["any"] for s in e["subjects"]):
            problems.append(f"rule {rule['name']}: exempt exactly {sorted(exempt)}, found {sorted(subjects)}")

    label_rules = {r["name"]: r for r in label["spec"]["rules"]}
    cloud = label_rules.get("cloud-objects-carry-the-system-label")
    routes = label_rules.get("route-objects-carry-the-system-label")
    if not (cloud and routes) or len(label_rules) != 2:
        return problems + ["require-system-label must have exactly the cloud and route rules"]
    for rule, kinds in ((cloud, CLOUD_GROUP_KINDS), (routes, ROUTE_KIND_SELECTORS)):
        block = _match_blocks(rule)[0]
        if set(block["kinds"]) != kinds:
            problems.append(f"rule {rule['name']}: kinds must be {sorted(kinds)}, found {sorted(block['kinds'])}")
        if sorted(block["operations"]) != ["CREATE", "UPDATE"]:
            problems.append(f"rule {rule['name']}: operations must be CREATE and UPDATE")
        if "exclude" in rule:
            problems.append(f"rule {rule['name']}: exempts nobody")
        conditions = rule["validate"]["deny"].get("conditions", {}).get("any", [])
        if len(conditions) != 1 or LABEL_KEY not in conditions[0].get("key", "") or conditions[0].get("operator") != "NotEquals" or conditions[0].get("value") != "{{ request.namespace }}":
            problems.append(f"rule {rule['name']}: must deny when the label {LABEL_KEY} does not equal the namespace")
    # The cloud kinds the label rule gates are the ones deny-raw gates.
    raw = policies.get("deny-raw-cloud-resources")
    if raw is not None:
        raw_kinds = set(_match_blocks(raw["spec"]["rules"][0])[0]["kinds"])
        if raw_kinds != CLOUD_GROUP_KINDS:
            problems.append(f"deny-raw-cloud-resources gates {sorted(raw_kinds)}; the label rule gates {sorted(CLOUD_GROUP_KINDS)} (ADR-0017 §7)")
    return problems


def check_edge_policies():
    policies = {}
    for path in sorted((REPO / "kyverno" / "policies").glob("*.yaml")):
        for doc in documents(path.read_text()):
            if doc["kind"] == "ClusterPolicy":
                policies[doc["metadata"]["name"]] = doc
    found = edge_policy_problems(policies)
    report(not found, "kyverno/policies: deny-edge-objects and require-system-label are as ADR-0021 §9 says" + (f": {found}" if found else ""))

    def rule(ps, policy, name):
        return next(r for r in ps[policy]["spec"]["rules"] if r["name"] == name)

    service = "no-load-balancer-or-nodeport-services-in-tenant-namespaces"
    create = "no-edge-objects-by-hand-in-tenant-namespaces"
    delete = "no-edge-object-deletes-by-hand-in-tenant-namespaces"
    cloud = "cloud-objects-carry-the-system-label"
    routes = "route-objects-carry-the-system-label"
    mutations = {
        "a Service rule that exempts Argo CD": lambda ps: rule(ps, "deny-edge-objects", service).update(exclude={"any": [{"subjects": [{"kind": "ServiceAccount", "name": "argocd-application-controller", "namespace": "argocd"}]}]}),
        "a Service rule that also lets NodePort through": lambda ps: rule(ps, "deny-edge-objects", service)["validate"]["deny"]["conditions"]["any"][0].update(value=["LoadBalancer"]),
        "a create rule that exempts the namespace controller": lambda ps: rule(ps, "deny-edge-objects", create)["exclude"]["any"][0]["subjects"].append({"kind": "ServiceAccount", "name": NAMESPACE_CONTROLLER[0], "namespace": NAMESPACE_CONTROLLER[1]}),
        "a create rule that refuses the garbage collector": lambda ps: rule(ps, "deny-edge-objects", create)["exclude"]["any"][0]["subjects"].pop(),
        "a delete rule that refuses the garbage collector": lambda ps: rule(ps, "deny-edge-objects", delete)["exclude"]["any"][0]["subjects"].pop(),
        "a whole networking.gke.io group": lambda ps: rule(ps, "deny-edge-objects", create)["match"]["any"][0]["resources"]["kinds"].append("networking.gke.io/*/*"),
        "a missing GKE kind": lambda ps: rule(ps, "deny-edge-objects", delete)["match"]["any"][0]["resources"]["kinds"].remove("networking.gke.io/*/LBPolicy"),
        "a missing Ingress": lambda ps: rule(ps, "deny-edge-objects", create)["match"]["any"][0]["resources"]["kinds"].remove("networking.k8s.io/*/Ingress"),
        "a deny that lets an edit through": lambda ps: rule(ps, "deny-edge-objects", create)["validate"].pop("allowExistingViolations"),
        "a label deny that lets an edit through": lambda ps: rule(ps, "require-system-label", routes)["validate"].update(allowExistingViolations=True),
        "a policy that fails open": lambda ps: ps["deny-edge-objects"]["spec"]["webhookConfiguration"].update(failurePolicy="Ignore"),
        "a mutating rule": lambda ps: rule(ps, "require-system-label", cloud).update(mutate={}),
        "a label rule that compares with another value": lambda ps: rule(ps, "require-system-label", cloud)["validate"]["deny"]["conditions"]["any"][0].update(value="svc-hello"),
        "a label rule that leaves out a route kind": lambda ps: rule(ps, "require-system-label", routes)["match"]["any"][0]["resources"]["kinds"].remove("gateway.networking.k8s.io/*/*"),
        "a label rule on a different set of cloud kinds": lambda ps: rule(ps, "require-system-label", cloud)["match"]["any"][0]["resources"]["kinds"].pop(),
        "a rule that reaches platform namespaces": lambda ps: rule(ps, "deny-edge-objects", delete)["match"]["any"][0]["resources"].pop("namespaceSelector"),
        "a missing policy": lambda ps: ps.pop("require-system-label"),
    }
    for what, edit in mutations.items():
        copied = copy.deepcopy(policies)
        edit(copied)
        report(bool(edge_policy_problems(copied)), f"  the policy check catches {what}")


def blacklist_problems(blacklist):
    """The git half of ADR-0021 §9: the tenant AppProject refuses the Ingress,
    every Gateway API kind and the GKE networking group."""
    want = {("networking.k8s.io", "Ingress"), ("gateway.networking.k8s.io", "*"), ("networking.gke.io", "*")}
    return [f"{g}/{k}" for g, k in sorted(want - set(blacklist))]


def check_no_edge_from_git(objects, label):
    projects = [o for o in objects if o["kind"] == "AppProject"]
    blacklist = set()
    if len(projects) == 1:
        blacklist = {(e.get("group"), e.get("kind")) for e in projects[0]["spec"].get("namespaceResourceBlacklist", [])}
    missing = blacklist_problems(blacklist)
    report(not missing, f"  {label}: its AppProject refuses Ingress, Gateway API and GKE networking kinds from git" + (f" (missing {missing})" if missing else ""))


def check_edge_git_door_can_fail():
    """The AppProject half of item 14 can fail: take each entry out of a
    blacklist that has all of them and the check must name it."""
    full = {("networking.k8s.io", "Ingress"), ("gateway.networking.k8s.io", "*"), ("networking.gke.io", "*")}
    report(not blacklist_problems(full), "the git-door check passes a blacklist that has all three entries")
    for entry in sorted(full):
        found = blacklist_problems(full - {entry})
        report(found == [f"{entry[0]}/{entry[1]}"], f"  the git-door check names {entry[0]}/{entry[1]} when it is missing")


def check_policies():
    """The reality gate refuses an edit too (ADR-0017 §7).

    On an update Kyverno judges the object twice: as asked for, and as it
    was. With allowExistingViolations on, which is its default, an update
    that was "already refused before" counts as nothing new and is let
    through. A deny with no conditions refuses every object it matches, so
    with the default every update is let through. The 2026-10-06 rehearsal
    found a team member's kubectl edit of a database admitted that way."""
    for path in sorted((REPO / "kyverno" / "policies").glob("*.yaml")):
        policy = yaml.safe_load(path.read_text())
        for rule in policy["spec"]["rules"]:
            validate = rule.get("validate") or {}
            if "deny" not in validate:
                continue
            unconditional = not (validate["deny"] or {}).get("conditions")
            if unconditional:
                report(
                    validate.get("allowExistingViolations") is False,
                    f"kyverno/policies/{path.name}, rule {rule['name']}: its deny has no conditions, so it says allowExistingViolations: false and an edit is refused like a create",
                )


def kyverno_admin_roles(values):
    """The ClusterRoles the Kyverno chart, rendered with `values`, labels
    aggregate-to-admin, or None if it does not render."""
    with tempfile.TemporaryDirectory() as folder:
        ok, output = helm_template("kyverno", "kyverno", "kyverno", [write_yaml(folder, "values.yaml", values)])
    if not ok:
        return None
    return sorted(
        o["metadata"]["name"]
        for o in documents(output)
        if o["kind"] == "ClusterRole" and (o["metadata"].get("labels") or {}).get(AGGREGATE_TO_ADMIN) == "true"
    )


def check_no_policy_from_kubectl():
    """The kubectl half of item 10. Every team holds the built-in `admin` role
    in its namespace (charts/system/templates/access.yaml), and the chart's
    default aggregates create, update and delete on Kyverno's namespaced policy
    kinds into it. apps/kyverno.yaml turns that off; this renders the chart
    with those values and finds no role aggregated into `admin`."""
    app = yaml.safe_load((REPO / "apps" / "kyverno.yaml").read_text())
    values = app["spec"]["source"]["helm"]["valuesObject"]
    default = copy.deepcopy(values)
    default["rbac"]["roles"]["aggregate"]["admin"] = True
    seen = kyverno_admin_roles(default)
    report(bool(seen), f"with the chart's default, the check finds the roles it guards against: {seen}")
    found = kyverno_admin_roles(values)
    report(found == [], f"with apps/kyverno.yaml's values, no Kyverno role aggregates into admin: {found}")


def check_lint(systems, environment):
    """helm lint, only with real value files (ADR-0017 §6). Not the gate."""
    tenant = yaml.safe_load((systems / "tenants" / "svc-hello.yaml").read_text())
    with tempfile.TemporaryDirectory() as folder:
        files = [
            write_yaml(folder, "tenant.yaml", tenant),
            write_yaml(folder, "retired.yaml", {"retired": []}),
            write_yaml(folder, "environment.yaml", environment),
        ]
        command = [HELM, "lint", str(CHARTS / "system"), "--namespace", "svc-hello"]
        for f in files:
            command += ["-f", str(f)]
        result = subprocess.run(command, capture_output=True, text=True)
        report(result.returncode == 0, "helm lint charts/system with the real value files")
        ok, output = render_system(folder, tenant, [], environment)
        vo = write_yaml(folder, "valuesobject.yaml", values_object(output, "svc-hello"))
        result = subprocess.run([HELM, "lint", str(CHARTS / "claims"), "--namespace", "svc-hello", "-f", str(vo)], capture_output=True, text=True)
        report(result.returncode == 0, "helm lint charts/claims with the real valuesObject")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--systems", type=pathlib.Path, required=True, help="a checkout of platform-factory/systems")
    parser.add_argument("--branch", help="the branch being checked (a PR's base branch); on any branch but main, no platform-config source may say main, and on main every one must")
    args = parser.parse_args()

    version = subprocess.run([HELM, "version", "--short"], capture_output=True, text=True).stdout.strip()
    report(version.startswith("v3.19."), f"Helm is {version or 'missing'}; Argo CD v3.4.6 bundles 3.19.x")

    environment = load_env()
    print("\n== the charts agree with the environment file")
    check_consistency(environment)
    check_no_off_switches()
    print("\n== every refusal refuses, and every good input renders")
    check_cases(environment)
    print("\n== every environment key is required")
    check_environment_keys(environment)
    print("\n== a claims.yaml cannot change the platform's facts (C-28 (b2))")
    check_tenant_values_change_nothing(environment)
    print("\n== the real tenant files")
    check_real_tenants(args.systems, environment)
    print("\n== svc-hello's database renders what is live")
    check_svc_hello_matches_live(args.systems, environment)
    print("\n== the root app-of-apps")
    check_apps(environment, args.branch)
    print("\n== a route renders what ADR-0021 §3 says")
    check_route_render(environment)
    print("\n== a route file cannot change the platform's facts")
    check_route_file_cannot_set_platform(environment)
    print("\n== the edge Applications")
    check_edge_apps()
    print("\n== the Gateways")
    check_gateways(environment)
    print("\n== the edge policies, and the git door")
    check_edge_policies()
    check_edge_git_door_can_fail()
    print("\n== the reality gate refuses an edit too")
    check_policies()
    print("\n== a team cannot write Kyverno policy with kubectl")
    check_no_policy_from_kubectl()
    print("\n== helm lint, with real value files")
    check_lint(args.systems, environment)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed.")
        sys.exit(1)
    print("every check passed.")


if __name__ == "__main__":
    main()
