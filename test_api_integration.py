"""API integration test for SPEC acceptance scenario 1 (Homework 3 Question 2).

Starts a real uvicorn server in a separate process, backed by a newly created,
disposable SQLite file, and drives the task exchange over HTTP.  Unlike
``test_agent_relay.py`` this module never imports the application, so running
it cannot create or reset tables in any other database.

Run on its own:

    uv run pytest test_api_integration.py -q
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

REPO_DIR = Path(__file__).resolve().parent
DEV_SERVER_PORT = 8000
STARTUP_TIMEOUT_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 10
SECRET_FIELDS = {"token", "claim_token"}


def sqlite_path(url: str | None) -> Path | None:
    """Resolve the file behind a ``sqlite:///`` URL (relative to the repo, like the app)."""

    if not url or not url.startswith("sqlite:///") or url.endswith(":memory:"):
        return None
    raw = url[len("sqlite:///") :]
    path = Path(raw)
    return (path if path.is_absolute() else REPO_DIR / path).resolve()


def protected_databases() -> set[Path]:
    """Databases the test must never touch: the app default and anything inherited."""

    protected = {(REPO_DIR / "agent-relay.db").resolve()}
    for name in ("RELAY_DATABASE_URL", "DATABASE_URL"):
        path = sqlite_path(os.environ.get(name))
        if path is not None:
            protected.add(path)
    return protected


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


def remove_tree(path: Path) -> None:
    # Windows can hold SQLite/WAL handles briefly after the server exits.
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.25)
    shutil.rmtree(path)


def server_log(log_path: Path) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return "(no server log)"


@pytest.fixture
def relay_server() -> Iterator[str]:
    workdir = Path(tempfile.mkdtemp(prefix="agent-relay-it-")).resolve()
    db_path = workdir / "relay-it.db"
    log_path = workdir / "server.log"
    port = free_port()

    # Prove the target is a brand-new, dedicated file before the server starts.
    assert port != DEV_SERVER_PORT
    assert list(workdir.iterdir()) == []
    assert db_path not in protected_databases()
    assert not db_path.is_relative_to(REPO_DIR)

    inherited = {"DATABASE_URL", "RELAY_ENROLLMENT_SECRET", "ENROLLMENT_SECRET"}
    env = {key: value for key, value in os.environ.items() if key not in inherited}
    env["RELAY_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["PYTHONUNBUFFERED"] = "1"
    assert sqlite_path(env["RELAY_DATABASE_URL"]) == db_path

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
        # The schema now exists in the disposable file, so the server is using it.
        assert db_path.is_file()
        yield base_url
    finally:
        stop_server(process)
        log_file.close()
        # The directory holds only this test's DB, its -wal/-shm sidecars and the log.
        remove_tree(workdir)


def register(client: httpx.Client, name: str) -> tuple[str, dict[str, str]]:
    data = expect(client.post("/api/v1/agents", json={"name": name}), 201).json()
    token = data.pop("token")
    assert token.startswith("agt_")
    assert data["agent_id"].startswith("agent_")
    return data["agent_id"], {"Authorization": f"Bearer {token}"}


def test_two_agents_exchange_task_over_real_http_api(relay_server: str) -> None:
    task_input = "hello from the integration test"
    task_output = "HELLO FROM THE INTEGRATION TEST"

    with httpx.Client(base_url=relay_server, timeout=10) as client:
        sender_id, sender_headers = register(client, "it-sender")
        recipient_id, recipient_headers = register(client, "it-recipient")
        assert sender_id != recipient_id

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
