"""Execute contracts and preserve concrete, replayable build evidence."""

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time

from . import __version__
from .checker import Checker, JsonProcess
from .contracts import generate_sequences, load_contract
from .errors import ContractError, ProtocolError


class TargetWorker(JsonProcess):
    def __init__(self, contract, timeout):
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        # A fresh cache prefix prevents CPython from loading stale timestamp-based
        # bytecode while our evidence records the current source file's hash.
        self._pycache = tempfile.TemporaryDirectory(prefix="lawbridge-bytecode-")
        env["PYTHONPYCACHEPREFIX"] = self._pycache.name
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            super().__init__([sys.executable, "-m", "lawbridge.worker", str(contract.path)],
                             timeout, cwd=contract.project_root, env=env)
        except BaseException:
            self._pycache.cleanup()
            raise
        try:
            ready = self.read()
            if ready.get("ready") is not True:
                raise ProtocolError(f"Python worker initialization failed: {ready.get('error', ready)}")
            if not all(isinstance(ready.get(key), str) for key in ["target_file", "target_sha256"]):
                raise ProtocolError("Python worker returned invalid target metadata")
            self.target_metadata = {key: ready[key] for key in ["target_file", "target_sha256"]}
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            self._pycache.cleanup()

    def reset(self, request_id):
        response = self.exchange({"id": request_id, "action": "reset"})
        if response.get("id") != request_id or response.get("reset") is not True:
            raise ProtocolError(f"Python worker reset failed: {response.get('error', response)}")

    def call(self, request_id, inputs):
        response = self.exchange({"id": request_id, "action": "call", "inputs": inputs})
        if response.get("id") != request_id:
            raise ProtocolError("Python worker response id does not match request")
        if "error" in response:
            raise ProtocolError(f"Python worker error: {response['error']}")
        record = response.get("record")
        if not isinstance(record, dict):
            raise ProtocolError("Python worker omitted execution record")
        return record


def _base_report(contract):
    return {
        "name": contract.name, "contract": str(contract.path),
        "contract_sha256": contract.sha256, "target": contract.data["target"],
        "mode": contract.mode, "status": "running",
        "evidence": "exhaustive_within_bounds" if contract.testing["mode"] == "exhaustive" else "sampled_executions",
        "sampling_bounds": contract.data["inputs"], "domain_size_within_bounds": contract.domain_size,
        "testing": contract.testing, "cases_checked": 0, "eligible_cases": 0,
        "ineligible_cases": 0, "sequences_checked": 0, "failures": [],
        "laws": {"requires": contract.data.get("requires", []),
                 "ensures": contract.data["ensures"], "unchanged": contract.data.get("unchanged", [])},
        "implementation_proved": False,
    }


def run_contract(contract, checker, timeout=10.0, *, replay=None):
    """Run one Contract. Law violations return a failed report; setup errors raise."""
    started = time.monotonic()
    report = _base_report(contract)
    if contract.data.get("model"):
        report["model"] = contract.data["model"]
        report["model_evidence"] = "conformance_to_registered_Lean_model; model_proof_is_separate"
    with TargetWorker(contract, timeout) as worker:
        report.update(worker.target_metadata)
        if replay is not None:
            if replay.get("target_sha256") != report["target_sha256"]:
                raise ContractError("replay target source hash differs; run a fresh check")
            sequences = [replay["sequence"]]
            report["evidence"] = "replayed_execution_sequence"
        else:
            sequences = generate_sequences(contract)
        for sequence_index, sequence in enumerate(sequences):
            worker.reset(f"{contract.name}:{sequence_index}:reset")
            prefix = []
            for step_index, inputs in enumerate(sequence):
                prefix.append(copy.deepcopy(inputs))
                request_id = f"{contract.name}:{sequence_index}:{step_index}"
                record = worker.call(request_id, inputs)
                request = {"id": request_id, "requires": contract.requires,
                           "ensures": contract.ensures, "record": record}
                if contract.data.get("reject_exception") is not None:
                    request["reject_exception"] = contract.data["reject_exception"]
                if contract.data.get("model"):
                    request["model"] = contract.data["model"]
                result = checker.check(request)
                report["cases_checked"] += 1
                report["eligible_cases" if result["eligible"] else "ineligible_cases"] += 1
                if not result["passed"]:
                    report["status"] = "failed"
                    payload = {
                        "contract": str(contract.path), "contract_sha256": contract.sha256,
                        "target_sha256": report["target_sha256"], "sequence": prefix,
                        "seed": contract.testing["seed"], "expected_failure_id": request_id,
                    }
                    if contract.data.get("model"):
                        payload["model"] = contract.data["model"]
                    report["failures"].append({"id": request_id, "inputs": inputs,
                                               "record": record, "failures": result["failures"],
                                               "replay": payload})
                    report["sequences_checked"] = sequence_index + 1
                    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
                    return report
            report["sequences_checked"] += 1
        if report["cases_checked"] == 0:
            raise ContractError("zero execution cases were checked")
        if replay is None and report["eligible_cases"] == 0:
            raise ContractError("no eligible cases were checked; ensures coverage would be vacuous")
        if hashlib.sha256(Path(report["target_file"]).read_bytes()).hexdigest() != report["target_sha256"]:
            raise ContractError("target source changed during verification")
    report["status"] = "passed"
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return report


def read_replay(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ContractError(f"cannot read replay: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("replay must be an object")
    if "contracts" in data:
        candidates = [failure.get("replay") for item in data["contracts"]
                      for failure in item.get("failures", []) if isinstance(failure, dict)]
        if not candidates:
            raise ContractError("report contains no replayable law failure")
        data = candidates[0]
    elif "replay" in data:
        data = data["replay"]
    if not isinstance(data, dict):
        raise ContractError("invalid replay payload")
    required = {"contract", "contract_sha256", "target_sha256", "sequence"}
    if not required <= data.keys():
        raise ContractError("replay missing required fields")
    if not isinstance(data["sequence"], list) or not data["sequence"] or len(data["sequence"]) > 100000:
        raise ContractError("replay sequence must contain between 1 and 100000 inputs")
    if any(not isinstance(item, dict) for item in data["sequence"]):
        raise ContractError("replay sequence items must be input objects")
    contract = load_contract(data["contract"])
    if contract.sha256 != data["contract_sha256"]:
        raise ContractError("replay contract hash differs; run a fresh check")
    return contract, data


def new_report():
    return {"version": 1, "lawbridge_version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "python_version": platform.python_version(), "evidence": "execution_checks",
            "status": "running", "contracts": [], "errors": [],
            "implementation_proved": False}
