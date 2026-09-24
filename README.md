# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

## Run it with Docker Compose

`compose.yaml` runs the API and PostgreSQL together. The database service is
named `postgres`, which is also the hostname the API uses on the Compose
network:

```bash
docker compose up --build
```

Open <http://localhost:8000/> for the token-based local dashboard. Data lives in
the `postgres-data` volume (`docker compose down -v` deletes it). Optional
environment variables (shell or a `.env` file): `POSTGRES_PASSWORD` (default
`agent_relay`), `RELAY_PORT` (host port, default `8000`), and
`RELAY_ENROLLMENT_SECRET`. `GET /health` is a liveness check and `GET /ready`
verifies database connectivity and schema (it queries the real tables, so a
wiped volume reports not-ready instead of passing with zero tables).

Inspect the stored rows directly:

```bash
docker compose exec postgres psql -U agent_relay -d agent_relay -c 'select id, status from tasks'
```

## Run it locally

Point `RELAY_DATABASE_URL` at a PostgreSQL database (default
`postgresql+psycopg://agent_relay:agent_relay@localhost:5432/agent_relay`;
plain `postgresql://` URLs are accepted too):

```bash
uv sync
RELAY_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/agent_relay uv run uvicorn main:app --reload
```

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains the SQLAlchemy models, the PostgreSQL engine, and lease
recovery. `storage.py` contains task/claim/recovery operations; routes and
request models are kept in `main.py` and `schemas.py`. Each write runs in one
transaction that locks the task row before touching its attempts. Claims select
the next queued task with `FOR UPDATE SKIP LOCKED`, so concurrent workers take
different tasks without waiting on each other.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving.

The tests need a disposable PostgreSQL server, named by
`RELAY_TEST_DATABASE_URL`. They never read `RELAY_DATABASE_URL`, and
`test_agent_relay.py` refuses to run unless the database name ends in `_test`,
because its fixture drops and recreates all tables. For example:

```bash
docker run -d --rm --name agent-relay-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_USER=relay -e POSTGRES_PASSWORD=relay -e POSTGRES_DB=agent_relay_test postgres:17-alpine
RELAY_TEST_DATABASE_URL=postgresql+psycopg://relay:relay@127.0.0.1:55432/agent_relay_test uv run --frozen pytest -q
docker rm -f agent-relay-test-pg
```

`test_api_integration.py` runs acceptance scenario 1 end to end over real
HTTP. It creates a new, uniquely named database on the
`RELAY_TEST_DATABASE_URL` server, starts its own uvicorn process on a free local
port against it, checks that the completed task is stored there, and drops the
database afterwards. It does not import the app, so it can run on its own while
the dev server is up:

```bash
uv run --frozen pytest test_api_integration.py -q
```

To run the same scenario against an already running relay instead, such as an
isolated Compose stack, set `RELAY_TEST_API_URL` to its base URL and
`RELAY_TEST_API_DATABASE_URL` to the PostgreSQL database behind it. The test
then starts no server and creates, drops or resets nothing: it registers two
agents and exchanges one task through the API, then checks the stored row in a
read-only database session. The agents and task stay in that relay, so they can
be inspected afterwards. It refuses port 8000, the development server's
default. Setting `RELAY_TEST_CREDENTIALS_FILE` saves the sender's agent token (mode 0600) for
viewing the task in that relay's dashboard; keep it outside the repository.
For example, with a separate Compose project whose override publishes the API
on port 63063 and PostgreSQL on 63064:

```bash
docker compose -p agent-relay-q2it -f compose.yaml -f compose.q2it.yaml up -d --build --wait
RELAY_TEST_API_URL=http://127.0.0.1:63063 \
RELAY_TEST_API_DATABASE_URL=postgresql+psycopg://agent_relay:agent_relay@127.0.0.1:63064/agent_relay \
  uv run --frozen pytest test_api_integration.py -q
docker compose -p agent-relay-q2it down -v
```

`compose.q2it.yaml` is not part of the repository; it only adds loopback
`ports` (`!override` for the API) and a separate `image` tag so the build does
not replace `agent-relay:local`.

## Kubernetes

`k8s/` deploys Agent Relay and PostgreSQL to a local kind cluster. The
database password comes from a Secret that you create locally; it is not in
the repository. See [k8s/README.md](k8s/README.md) for Secret setup,
deployment, port forwarding and running the integration test against the
cluster.

## Claude Code development record

The Homework 3 Claude Code session record, maintained as described in `AGENTS.md`:

- [Claude Code prompts](_docs/claude-code-prompts.md): exact user prompts
- [Claude Code conversation](_docs/claude-code-conversation.md): sanitized dialogue and tool activity
