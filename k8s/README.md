# Kubernetes (kind)

Manifests for running Agent Relay and PostgreSQL on a local
[kind](https://kind.sigs.k8s.io/) cluster:

- `postgres.yaml`: headless Service `postgres` and a one-replica StatefulSet
  (`postgres:17-alpine`) with a 1Gi PersistentVolumeClaim for the data
  directory and a `pg_isready` readiness probe.
- `agent-relay.yaml`: Service `agent-relay` (port 8000) and a Deployment of
  `agent-relay:local`. An init container waits for PostgreSQL; the readiness
  probe is `GET /ready` (tables reachable) and the liveness probe `GET /health`.

## Local Secret setup

The database password is not stored in the repository. Create the
`postgres-credentials` Secret once per cluster, before applying the
manifests. Use a URL-safe value, because the app builds
`RELAY_DATABASE_URL` from it:

```bash
kubectl create secret generic postgres-credentials \
  --from-literal=POSTGRES_DB=agent_relay \
  --from-literal=POSTGRES_USER=agent_relay \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)"
```

PostgreSQL applies the password only when it initializes an empty volume. To
change it later, either `ALTER USER` inside the database and then update the
Secret, or delete the StatefulSet and its PVC (`postgres-data-postgres-0`),
which erases the data.

Optional: to require an enrollment secret for agent registration, create
`agent-relay-enrollment` with key `secret`; the Deployment reads it if it
exists:

```bash
kubectl create secret generic agent-relay-enrollment --from-literal=secret="$(openssl rand -hex 24)"
```

## Deploy

```bash
kind create cluster                      # if no cluster exists yet
docker build -t agent-relay:local .
kind load docker-image agent-relay:local
kubectl apply -f k8s/
kubectl rollout status statefulset/postgres
kubectl rollout status deployment/agent-relay
kubectl get pods
```

The Deployment uses `imagePullPolicy: Never`, so the image must be loaded into
kind first. kind pulls `postgres:17-alpine` from Docker Hub itself.
`kind load docker-image postgres:17-alpine` can fail with Docker Desktop's
containerd image store (`content digest ... not found`), so it isn't needed.

## Access

```bash
kubectl port-forward svc/agent-relay 18000:8000
```

Then open <http://127.0.0.1:18000/>. Port 8000 is left free for the Compose
stack. To run the Question 2 integration test against the cluster, also forward
PostgreSQL and pass the password from the Secret without printing it:

```bash
kubectl port-forward svc/postgres 15432:5432
PGPASS="$(kubectl get secret postgres-credentials -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)"
RELAY_TEST_API_URL=http://127.0.0.1:18000 \
RELAY_TEST_API_DATABASE_URL="postgresql+psycopg://agent_relay:${PGPASS}@127.0.0.1:15432/agent_relay" \
  uv run --frozen pytest test_api_integration.py -q
```
