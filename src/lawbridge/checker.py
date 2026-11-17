"""Persistent JSON-lines subprocesses with bounded reads and writes."""

from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import threading
import time

from .errors import ProtocolError

MAX_LINE = 4 * 1024 * 1024


def validate_json_value(value, path="value", depth=0):
    if depth > 64:
        raise ProtocolError(f"{path}: nesting exceeds 64")
    if value is None or type(value) in (bool, int, str):
        return
    if isinstance(value, list):
        for i, child in enumerate(value):
            validate_json_value(child, f"{path}[{i}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProtocolError(f"{path}: object keys must be strings")
            validate_json_value(child, f"{path}.{key}", depth + 1)
        return
    raise ProtocolError(f"{path}: unsupported value type {type(value).__name__}; floats are not supported")


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


class JsonProcess:
    def __init__(self, command, timeout=10.0, *, cwd=None, env=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ProtocolError("timeout must be a positive finite number")
        self.command = list(command)
        self.timeout = timeout
        self.buffer = b""
        self.stderr_tail = deque(maxlen=40)
        self.closed = False
        try:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, cwd=cwd, env=env, bufsize=0)
        except OSError as exc:
            raise ProtocolError(f"cannot start {self.command[0]!r}: {exc}") from exc
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

        def drain_stderr():
            while True:
                try:
                    chunk = self.process.stderr.read(4096)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                self.stderr_tail.append(chunk.decode("utf-8", errors="replace"))

        self._stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _failure(self, message):
        tail = "".join(self.stderr_tail)[-4000:].strip()
        return ProtocolError(message + (f"; stderr: {tail}" if tail else ""))

    def _read(self, deadline):
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            while True:
                if b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    try:
                        result = json.loads(line, parse_constant=_reject_constant)
                    except (ValueError, UnicodeError) as exc:
                        raise self._failure(f"malformed JSON response: {exc}") from exc
                    if not isinstance(result, dict):
                        raise self._failure("protocol response must be a JSON object")
                    validate_json_value(result, "response")
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise self._failure(f"process response timed out after {self.timeout:g}s")
                try:
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise self._failure(f"process exited before a complete response (exit={self.process.poll()})")
                self.buffer += chunk
                if len(self.buffer) > MAX_LINE:
                    raise self._failure("protocol response exceeds 4 MiB")
        finally:
            selector.close()

    def read(self):
        return self._read(time.monotonic() + self.timeout)

    def exchange(self, payload):
        if self.closed:
            raise ProtocolError("process already closed")
        validate_json_value(payload, "request")
        try:
            data = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError(f"cannot encode request: {exc}") from exc
        if len(data) > MAX_LINE:
            raise ProtocolError("protocol request exceeds 4 MiB")
        deadline = time.monotonic() + self.timeout
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdin, selectors.EVENT_WRITE)
        try:
            offset = 0
            while offset < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise self._failure(f"process write timed out after {self.timeout:g}s")
                try:
                    offset += os.write(self.process.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError) as exc:
                    raise self._failure(f"process input closed: {exc}") from exc
            return self._read(deadline)
        finally:
            selector.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.process.stdout.close()
        self._stderr_thread.join(timeout=0.2)
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Checker(JsonProcess):
    def __init__(self, command=None, timeout=10.0):
        if command is None:
            override = os.environ.get("LAWBRIDGE_LEAN_CHECKER")
            if override:
                command = shlex.split(override)
            else:
                root = Path(__file__).resolve().parents[2]
                command = [str(root / "lean/.lake/build/bin/lawcheck")]
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            raise ProtocolError("Lean checker command is empty")
        super().__init__(command, timeout)

    def check(self, request):
        response = self.exchange(request)
        if response.get("id") != request["id"]:
            raise ProtocolError("checker response id does not match request")
        if "error" in response:
            raise ProtocolError(f"Lean checker error: {response['error']}")
        if type(response.get("passed")) is not bool or type(response.get("eligible")) is not bool:
            raise ProtocolError("checker passed and eligible must be Booleans")
        failures = response.get("failures")
        if not isinstance(failures, list) or any(not isinstance(x, str) for x in failures):
            raise ProtocolError("checker failures must be an array of strings")
        if response["passed"] == bool(failures):
            raise ProtocolError("checker passed flag and failures are inconsistent")
        return response

    def metadata(self):
        fingerprints = {}
        for argument in self.command:
            path = Path(argument)
            if path.is_file():
                fingerprints[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"command": self.command, "files_sha256": fingerprints}
