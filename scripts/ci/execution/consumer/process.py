"""Run child processes with live logs and deadline enforcement."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path


def _terminate(process: subprocess.Popen[str], sig: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return


def run_stream(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
    timeout_seconds: int,
) -> tuple[int, bool]:
    """Run a command, stream its output, and return ``(exit_code, timed_out)``."""
    print("+ " + " ".join(command), flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = threading.Event()
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        def terminate() -> None:
            timed_out.set()
            _terminate(process, signal.SIGTERM)
            timer = threading.Timer(
                10, lambda: _terminate(process, signal.SIGKILL)
            )
            timer.daemon = True
            timer.start()

        timer = threading.Timer(timeout_seconds, terminate)
        timer.daemon = True
        timer.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            rc = process.wait()
        except KeyboardInterrupt:
            _terminate(process, signal.SIGTERM)
            process.wait()
            raise
        finally:
            timer.cancel()
    if timed_out.is_set():
        return 124, True
    return rc, False


def timeout_budget(deadline: float, requested_seconds: int) -> tuple[int, bool]:
    """Return the timeout allowed by both the task and remaining job budget."""
    remaining = max(0, int(deadline - time.monotonic()))
    return min(requested_seconds, remaining), remaining < requested_seconds
