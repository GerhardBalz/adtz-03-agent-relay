"""API integration test for SPEC acceptance scenario 1 (Homework 3 Question 2).

Starts a real uvicorn server in a separate process, backed by a newly created,
disposable PostgreSQL database, and drives the task exchange over HTTP.  Unlike
``test_agent_relay.py`` this module never imports the application, so running
it cannot create or reset tables in any other database.

``RELAY_TEST_DATABASE_URL`` names the PostgreSQL server to use; the test
connects there only to CREATE and afterwards DROP its own uniquely named
database.  Run on its own:

    RELAY_TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest test_api_integration.py -q

To exercise an already running relay instead (for example an isolated Compose
stack), set ``RELAY_TEST_API_URL`` to its base URL and
``RELAY_TEST_API_DATABASE_URL`` to the PostgreSQL database behind it.  The test
then starts no server and creates or drops nothing: it adds its agents and task
through the API and only reads the database, in a read-only session, to check
the stored task.  ``RELAY_TEST_CREDENTIALS_FILE`` optionally saves the sender's
agent token (mode 0600) so the task can be inspected in that relay's dashboard.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import json
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

REPO_DIR = Path(__file__).resolve().parent
DEV_SERVER_PORT = 8000
STARTUP_TIMEOUT_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 10
SECRET_FIELDS = {"token", "claim_token"}


def admin_url() -> URL:
    raw = os.environ.get("RELAY_TEST_DATABASE_URL")
    if not raw:
        pytest.fail("Set RELAY_TEST_DATABASE_URL to a disposable PostgreSQL server (see README).", pytrace=False)
    url = make_url(raw)
    if url.drivername in {"postgresql", "postgres"}:
        url = url.set(drivername="postgresql+psycopg")
    return url


def run_admin(url: URL, statement: str) -> None:
    # CREATE/DROP DATABASE cannot run inside a transaction block.
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def stored_task(url: URL, task_id: str) -> dict | None:
    # Read-only session: verification must never modify the relay's database.
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, sender_id, recipient_id, input, output, status, attempt_count, finished_at "
                    "FROM tasks WHERE id = :id"
                ),
                {"id": task_id},
            ).mappings().one_or_none()
            return dict(row) if row else None
    finally:
        engine.dispose()


def save_credentials(agent_id: str, token: str) -> None:
    path = os.environ.get("RELAY_TEST_CREDENTIALS_FILE")
    if not path:
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"agent_id": agent_id, "token": token}, handle)


def count_tasks(url: URL) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT count(*) FROM tasks")).scalar_one()
    finally:
        engine.dispose()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def redacted(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        body = {key: ("<redacted>" if key in SECRET_FIELDS else value) for key, value in body.items()}
    return repr(body)


def expect(response: httpx.Response, status_code: int) -> httpx.Response:
    # Report failures without echoing agent or claim tokens.
    if response.status_code != status_code:
        raise AssertionError(
            f"{response.request.method} {response.request.url.path}: expected {status_code}, "
            f"got {response.status_code} {redacted(response)}"
        )
    return response


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)


def server_log(log_path: Path) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return "(no server log)"


def external_relay() -> tuple[str, URL] | None:
    base_url = os.environ.get("RELAY_TEST_API_URL")
    if not base_url:
        return None
    if urlsplit(base_url).port in {None, DEV_SERVER_PORT}:
        pytest.fail(f"RELAY_TEST_API_URL must name an isolated relay's explicit port, not {DEV_SERVER_PORT}.", pytrace=False)
    raw = os.environ.get("RELAY_TEST_API_DATABASE_URL")
    if not raw:
        pytest.fail("Set RELAY_TEST_API_DATABASE_URL to the database behind RELAY_TEST_API_URL.", pytrace=False)
    db_url = make_url(raw)
    if db_url.drivername in {"postgresql", "postgres"}:
        db_url = db_url.set(drivername="postgresql+psycopg")
    return base_url.rstrip("/"), db_url


@pytest.fixture
def relay_server() -> Iterator[tuple[str, URL]]:
    external = external_relay()
    if external is not None:
        base_url, db_url = external
        expect(httpx.get(f"{base_url}/ready", timeout=5), 200)
        yield external
        return

    admin = admin_url()
    # A fresh random name: CREATE DATABASE fails rather than reuse anything.
    db_name = f"relay_it_{uuid.uuid4().hex[:16]}"
    db_url = admin.set(database=db_name)
    log_path = Path(tempfile.mkdtemp(prefix="agent-relay-it-")) / "server.log"
    port = free_port()
    assert port != DEV_SERVER_PORT

    run_admin(admin, f'CREATE DATABASE "{db_name}"')
    inherited = {"DATABASE_URL", "RELAY_ENROLLMENT_SECRET", "ENROLLMENT_SECRET"}
    env = {key: value for key, value in os.environ.items() if key not in inherited}
    env["RELAY_DATABASE_URL"] = db_url.render_as_string(hide_password=False)
    env["PYTHONUNBUFFERED"] = "1"

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            if process.poll() is not None:
                pytest.fail(f"relay server exited with {process.returncode}:\n{server_log(log_path)}")
            try:
                if httpx.get(f"{base_url}/ready", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"relay server not ready after {STARTUP_TIMEOUT_SECONDS}s:\n{server_log(log_path)}")
            time.sleep(0.2)
        # The schema now exists in the disposable database, so the server is using it.
        assert count_tasks(db_url) == 0
        yield base_url, db_url
    finally:
        stop_server(process)
        log_file.close()
        log_path.unlink(missing_ok=True)
        log_path.parent.rmdir()
        run_admin(admin, f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def register(client: httpx.Client, name: str) -> tuple[str, str]:
    data = expect(client.post("/api/v1/agents", json={"name": name}), 201).json()
    token = data.pop("token")
    assert token.startswith("agt_")
    assert data["agent_id"].startswith("agent_")
    return data["agent_id"], token


def test_two_agents_exchange_task_over_real_http_api(relay_server: tuple[str, URL]) -> None:
    base_url, db_url = relay_server
    task_input = "hello from the integration test"
    task_output = "HELLO FROM THE INTEGRATION TEST"

    with httpx.Client(base_url=base_url, timeout=10) as client:
        sender_id, sender_token = register(client, "it-sender")
        recipient_id, recipient_token = register(client, "it-recipient")
        assert sender_id != recipient_id
        save_credentials(sender_id, sender_token)
        sender_headers = {"Authorization": f"Bearer {sender_token}"}
        recipient_headers = {"Authorization": f"Bearer {recipient_token}"}

        sent = expect(
            client.post("/api/v1/tasks", headers=sender_headers, json={"to": recipient_id, "input": task_input}),
            201,
        ).json()
        task_id = sent["task_id"]
        assert sent["status"] == "queued"

        claim = expect(
            client.post(
                "/api/v1/tasks/claim",
                headers=recipient_headers,
                json={"worker_id": "it-worker", "wait_seconds": 0},
            ),
            200,
        ).json()
        claim_token = claim.pop("claim_token")
        assert claim["task_id"] == task_id
        assert claim["from"] == sender_id
        assert claim["input"] == task_input
        assert claim["attempt"] == 1

        completed = expect(
            client.post(
                f"/api/v1/tasks/{task_id}/complete",
                headers=recipient_headers,
                json={"claim_token": claim_token, "output": task_output},
            ),
            200,
        ).json()
        assert completed == {"task_id": task_id, "status": "completed"}

        seen = expect(client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers), 200).json()
        assert seen["task_id"] == task_id
        assert seen["from"] == sender_id
        assert seen["to"] == recipient_id
        assert seen["input"] == task_input
        assert seen["status"] == "completed"
        assert seen["output"] == task_output
        assert seen["error"] is None
        assert seen["attempt_count"] == 1
        assert seen["finished_at"] is not None

    # The task is persisted in the server's PostgreSQL database.
    row = stored_task(db_url, task_id)
    assert row is not None
    assert (row["sender_id"], row["recipient_id"]) == (sender_id, recipient_id)
    assert (row["input"], row["output"], row["status"]) == (task_input, task_output, "completed")
    assert row["attempt_count"] == 1
    assert row["finished_at"] is not None
    print(f"completed task {task_id} from {sender_id} to {recipient_id}")
