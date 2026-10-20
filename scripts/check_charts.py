#!/usr/bin/env python3
"""Checks for charts/system and charts/claims, run on every pull request.

Why it exists
-------------
From M2b, a tenant's cloud resources come from two Helm charts that only the
platform renders (ADR-0017). The claims chart's schema is the boundary between
a tenant and the platform, so a loose schema or a template slip is a hole in
it. The API server no longer checks a tenant's form; `helm template` does, at
Argo CD's render and here. This script checks the charts the way Argo CD will
render them, and proves each refusal actually refuses (ADR-0019 §4).

It uses `helm template` only. `helm lint` reports a failed `required` or `fail`
only as information (ADR-0017 §6), so it is run last, with the real value files,
as an extra check and never as the gate.

What it checks
--------------
  1. The two charts agree with the environment file: the same `environment`
     schema in both, every key of environments/<env>.yaml required, and no
     value in either chart's values.yaml.
  2. Nothing in charts/ or apps/ turns a gate off: no skipSchemaValidation,
     no allowEmpty (ADR-0017 §6, ADR-0019 §2).
  3. Every case in tests/charts/cases.yaml: each must-fail case is refused,
     with the text it expects; each must-pass case renders.
  4. Every environment key, deleted and then blanked, stops both charts, and
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
FIXTURE_TENANTS = REPO / "tests" / "charts" / "tenants"
HELM = os.environ.get("HELM", "helm")

CNRM_GROUP = ".cnrm.cloud.google.com/"
CNRM_ANNOTATIONS = [
    "cnrm.cloud.google.com/state-into-spec",
    "cnrm.cloud.google.com/management-conflict-prevention-policy",
    "cnrm.cloud.google.com/project-id",
]
DURABLE_KINDS = {"SQLInstance", "SQLDatabase", "SQLUser", "ArtifactRegistryRepository"}

failures = []


def report(ok, message):
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


# ---------------------------------------------------------------------------
# Rendering, the way Argo CD does it
# ---------------------------------------------------------------------------

def helm_template(chart, release, namespace, value_files):
    """Run `helm template`. Returns (ok, stdout or stderr)."""
    command = [HELM, "template", release, str(CHARTS / chart), "--namespace", namespace]
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
    system_schema = json.loads((CHARTS / "system" / "values.schema.json").read_text())
    claims_schema = json.loads((CHARTS / "claims" / "values.schema.json").read_text())
    report(
        system_schema["$defs"]["environment"] == claims_schema["$defs"]["environment"],
        "both charts carry the same environment schema",
    )
    env_schema = claims_schema["$defs"]["environment"]
    keys = set(environment["environment"])
    report(set(env_schema["required"]) == keys, "every key of environments/reference.yaml is required by the schema, and no other")
    report(set(env_schema["properties"]) == keys, "the environment schema lists exactly the environment file's keys")
    for chart in ("system", "claims"):
        values = yaml.safe_load((CHARTS / chart / "values.yaml").read_text())
        report(not values, f"charts/{chart}/values.yaml carries no value")


def check_no_off_switches():
    found = []
    for folder in (CHARTS / "system", CHARTS / "claims", REPO / "apps"):
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
        # ADR-0019 §2: no databases means an empty render, so Argo CD's guard
        # against pruning to empty applies.
        if case["chart"] == "claims" and not (case.get("claims") or {}).get("databases"):
            report(not documents(output), f"  '{case['name']}' rendered no object at all")


def check_environment_keys(environment):
    """Delete, then blank, each environment key: both charts must stop, naming it."""
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
            report(
                not ok_s and key in out_s and not ok_c and key in out_c,
                f"environment.{key} {how}: both charts refuse, naming it",
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
    spec = document["spec"]
    if document["kind"] == "ApplicationSet":
        spec = spec["template"]["spec"]
    sources = spec.get("sources") or [spec["source"]]
    return [s for s in sources if s["repoURL"].rstrip("/").endswith("/platform-config")]


def check_apps(environment, branch):
    """The root app-of-apps: the ApplicationSet's guards (ADR-0019 §1), one
    revision for every read of this repo (ADR-0017 §12), and the engine's
    service account."""
    apps = {p.name: yaml.safe_load(p.read_text()) for p in sorted((REPO / "apps").glob("*.yaml"))}
    revision = environment["environment"]["platformRevision"]

    appset = apps.get("systems.yaml", {})
    annotations = appset.get("metadata", {}).get("annotations", {})
    sync_options = {o.strip() for o in annotations.get("argocd.argoproj.io/sync-options", "").split(",")}
    report(appset.get("kind") == "ApplicationSet", "apps/systems.yaml is the tenants' ApplicationSet")
    report(appset.get("spec", {}).get("syncPolicy", {}).get("applicationsSync") == "create-update", "  it never deletes an Application (applicationsSync: create-update)")
    report({"Prune=false", "Delete=false"} <= sync_options, "  git cannot prune or delete it (Prune=false,Delete=false)")
    report("resources-finalizer.argocd.argoproj.io" in appset.get("metadata", {}).get("finalizers", []), "  a background kubectl delete keeps its Applications (its own finalizer)")
    report("missingkey=error" in appset.get("spec", {}).get("goTemplateOptions", []), "  a missing field is an error (missingkey=error)")

    for name, document in apps.items():
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
    print("\n== the reality gate refuses an edit too")
    check_policies()
    print("\n== helm lint, with real value files")
    check_lint(args.systems, environment)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed.")
        sys.exit(1)
    print("every check passed.")


if __name__ == "__main__":
    main()
