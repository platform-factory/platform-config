{{/*
The two Jobs of one claim, as named templates. Each Job's name ends in a hash
of its own spec (see databases.yaml), because a Job's pod template cannot be
changed in place: any change to what is below produces a Job with a new name,
Argo CD prunes the old, finished one, and the new one runs. Both scripts are
safe to run again.

Both Jobs use the postgres image already pulled through the platform's Docker
Hub remote (ADR-0010), and nothing else. Its busybox wget makes HTTPS calls and
checks certificates against the image's trust store [C, docker run of
postgres:16-alpine, 2026-09-27: a self-signed and a wrong-host certificate are
both refused].
*/}}

{{/*
The seed Job's spec. Called with a dict: claim, secret, image.

WHY IT EXISTS. The instance's superuser password has to be generated once and
then kept. A template cannot do that: every render would mint a new one
(ADR-0017 §8). So this Job creates the <claim>-admin Secret if it is absent and
leaves it alone if it is present. Argo CD does not track a Secret it did not
apply, so it neither prunes it nor reports it as drift.

It talks to the Kubernetes API with its own service account token.
`database-seed` is created by charts/system in every System's namespace, with
permission to get and create Secrets there and nothing else. The API server's
certificate is signed by the cluster's own CA, so SSL_CERT_FILE points the
image's trust store at the CA file that comes with the token [C, run as a Job
on a local k3s cluster, 2026-09-27: created on the first run, left unchanged on
the second].

THE KNOWN LIMIT (ADR-0016 §4, carried by ADR-0017 §8). The Secret lives in the
cluster, and the cluster is rebuilt between sessions. After a rebuild this Job
mints a new password, and nothing tells Cloud SQL, so the Secret's value no
longer opens the postgres role on an instance that already existed. The GRANT
Job below is built to need the password only when its grant is genuinely
missing, which it is not on a rebuilt cluster.
*/}}
{{- define "claims.seedJobSpec" -}}
backoffLimit: 6
template:
  metadata:
    labels:
      {{- include "claims.labels" (list .system .claim) | nindent 6 }}
  spec:
    restartPolicy: OnFailure
    serviceAccountName: database-seed
    containers:
      - name: seed
        image: {{ .image | quote }}
        command: ["/bin/sh", "-c"]
        args:
          - |
            set -u
            SA=/var/run/secrets/kubernetes.io/serviceaccount
            TOKEN="$(cat "$SA/token")"
            SECRETS="https://kubernetes.default.svc/api/v1/namespaces/${SYSTEM}/secrets"
            export SSL_CERT_FILE="$SA/ca.crt"

            # The HTTP status of a `wget -S` call, from its response headers.
            http_status() { sed -n 's/^  HTTP\/1\.[01] \([0-9][0-9][0-9]\).*/\1/p' | tail -n 1; }

            n=0
            while [ "$n" -lt 20 ]; do
              n=$((n + 1))
              code="$(wget -S -q -O /dev/null --header "Authorization: Bearer ${TOKEN}" "${SECRETS}/${SECRET}" 2>&1 | http_status)"
              if [ "$code" = "200" ]; then
                echo "${SECRET} already exists; left unchanged"
                exit 0
              fi
              if [ "$code" = "404" ]; then
                PASSWORD="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)"
                BODY="{\"apiVersion\":\"v1\",\"kind\":\"Secret\",\"metadata\":{\"name\":\"${SECRET}\",\"labels\":{\"platform.thecloudgeek.io/system\":\"${SYSTEM}\",\"platform.thecloudgeek.io/database\":\"${CLAIM}\"}},\"type\":\"Opaque\",\"stringData\":{\"password\":\"${PASSWORD}\"}}"
                code="$(wget -S -q -O /dev/null --header "Authorization: Bearer ${TOKEN}" --header "Content-Type: application/json" --post-data "${BODY}" "${SECRETS}" 2>&1 | http_status)"
                # 409: another run created it first, which is just as good.
                if [ "$code" = "201" ] || [ "$code" = "409" ]; then
                  echo "${SECRET} created (HTTP ${code})"
                  exit 0
                fi
              fi
              echo "attempt ${n}/20: HTTP ${code:-none}; retrying in 15s"
              sleep 15
            done
            echo "could not create ${SECRET}" >&2
            exit 1
        env:
          - name: SYSTEM
            valueFrom:
              fieldRef:
                fieldPath: metadata.namespace
          - name: CLAIM
            value: {{ .claim | quote }}
          - name: SECRET
            value: {{ .secret | quote }}
        resources:
          # Limits are mandatory: the System's ResourceQuota names limits.cpu
          # and limits.memory, and a container without them is refused.
          requests:
            cpu: 20m
            memory: 32Mi
          limits:
            cpu: 100m
            memory: 64Mi
{{- end -}}

{{/*
The GRANT Job's spec. Called with a dict: claim, secret, image, project,
instance, iamUser, database.

WHY IT EXISTS. Cloud SQL creates an IAM database user with no privileges at
all, and on a fresh instance only the built-in postgres role can grant them
(ADR-0013). This Job runs that GRANT.

WHAT CHANGED FROM THE CROSSPLANE RECIPE. The old Job had the instance's
private IP written into it from the engine's observed state. A render from git
has no observed state, so the Job looks the address up itself, from the Cloud
SQL Admin API, with the token its identity already gets from the metadata
server (ADR-0017 §8). It runs as the System's own service account, whose
Google identity holds roles/cloudsql.client; that the role's instances.get is
enough for the lookup is [I until the first cluster session].

ORDER OF ATTEMPTS, unchanged from M2: first ask the application's own identity
whether the grant is already in place (an IAM login, which needs no password
and survives a rebuild), and only then spend the postgres password.
*/}}
{{- define "claims.grantJobSpec" -}}
# Twelve pod attempts, each retrying for about ten minutes, because a new
# instance takes a while to exist and a Job marked Failed is never retried.
backoffLimit: 12
template:
  metadata:
    labels:
      {{- include "claims.labels" (list .system .claim) | nindent 6 }}
  spec:
    restartPolicy: OnFailure
    # The System's Kubernetes service account, which Workload Identity maps to
    # the System's Google service account (charts/system).
    serviceAccountName: {{ .system | quote }}
    containers:
      - name: grant
        image: {{ .image | quote }}
        command: ["/bin/sh", "-c"]
        args:
          - |
            set -u
            TOKEN_URL='http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'

            # An OAuth2 access token for this pod's Google identity.
            token() {
              wget -qO- --header='Metadata-Flavor: Google' "$TOKEN_URL" 2>/dev/null \
                | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p'
            }

            # The instance's private IP, from the Cloud SQL Admin API. The
            # response lists each address as an object with an ipAddress and a
            # type; split on "}" so each address is one line, whatever order
            # its keys come in, and keep the PRIVATE one.
            private_ip() {
              wget -qO- --header "Authorization: Bearer $1" \
                "https://sqladmin.googleapis.com/v1/projects/${PROJECT}/instances/${INSTANCE}" 2>/dev/null \
                | tr -d '\n\t ' | tr '}' '\n' | grep '"type":"PRIVATE"' \
                | sed -n 's/.*"ipAddress":"\([0-9.]*\)".*/\1/p' | head -n 1
            }

            # Postgres 15+ removes CREATE on schema public from PUBLIC, so this
            # tests whether the second GRANT below has run, not merely whether
            # the user can connect.
            already_granted() {
              PGPASSWORD="$2" psql "$1 user=${IAM_USER}" -tAc \
                "SELECT has_schema_privilege('public', 'CREATE')" 2>/dev/null | grep -qx t
            }

            # Both statements are idempotent, so a re-run is safe.
            do_grant() {
              PGPASSWORD="${ADMIN_PASSWORD}" psql "$1 user=postgres" --no-password --set=ON_ERROR_STOP=1 \
                -c "GRANT ALL PRIVILEGES ON DATABASE \"${DB}\" TO \"${IAM_USER}\";" \
                -c "GRANT ALL ON SCHEMA public TO \"${IAM_USER}\";"
            }

            n=0
            while [ "$n" -lt 40 ]; do
              n=$((n + 1))
              tok="$(token)"
              ip=""
              [ -n "$tok" ] && ip="$(private_ip "$tok")"
              if [ -n "$ip" ]; then
                # sslmode=require: Cloud SQL requires TLS for an IAM login, and
                # it keeps the postgres password off the wire in the clear.
                BASE="host=${ip} port=5432 dbname=${DB} sslmode=require"
                if already_granted "$BASE" "$tok"; then
                  echo "grant already in place for ${IAM_USER} on \"${DB}\""
                  exit 0
                fi
                if do_grant "$BASE"; then
                  echo "granted ${IAM_USER} on \"${DB}\""
                  exit 0
                fi
              else
                echo "no private IP for ${INSTANCE} yet (the instance may still be creating)"
              fi
              echo "attempt ${n}/40 did not succeed; retrying in 15s"
              sleep 15
            done
            echo "could not grant after 40 attempts." >&2
            echo "If psql said 'password authentication failed for user \"postgres\"', the ${SECRET} Secret no longer matches Cloud SQL; see the chart README, 'After a rebuild'." >&2
            exit 1
        env:
          - name: PROJECT
            value: {{ .project | quote }}
          - name: INSTANCE
            value: {{ .instance | quote }}
          - name: IAM_USER
            value: {{ .iamUser | quote }}
          - name: DB
            value: {{ .database | quote }}
          - name: SECRET
            value: {{ .secret | quote }}
          # Not PGPASSWORD: step one logs in with a token, step two with this.
          - name: ADMIN_PASSWORD
            valueFrom:
              secretKeyRef:
                name: {{ .secret | quote }}
                key: password
        resources:
          # Limits are mandatory: see the seed Job above.
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 200m
            memory: 128Mi
{{- end -}}
