"""Run a trusted local Python target in an isolated, timeout-controlled process."""

import contextlib
import copy
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys

from .checker import validate_json_value
from .contracts import load_contract


def emit(value):
    validate_json_value(value)
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n")
    sys.stdout.flush()


def main():
    contract_path = sys.argv[1]
    try:
        contract = load_contract(contract_path)
        sys.path.insert(0, str(contract.project_root))
        sys.path.insert(0, str(contract.project_root / "src"))
        module_name, attribute = contract.data["target"].split(":")
        with contextlib.redirect_stdout(sys.stderr):
            module = importlib.import_module(module_name)
            target = module
            for name in attribute.split("."):
                target = getattr(target, name)
        if not callable(target):
            raise TypeError("target is not callable")
        source = inspect.getsourcefile(target) or getattr(module, "__file__", None)
        if source is None or not Path(source).is_file():
            raise TypeError("target must have an inspectable Python source file")
        source_path = Path(source).resolve()
        fingerprint = hashlib.sha256(source_path.read_bytes()).hexdigest()
        emit({"ready": True, "target_file": str(source_path), "target_sha256": fingerprint})
    except BaseException as exc:
        emit({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2
    instance = None
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request["id"]
            if hashlib.sha256(source_path.read_bytes()).hexdigest() != fingerprint:
                raise RuntimeError("target source changed during verification")
            if request["action"] == "reset":
                with contextlib.redirect_stdout(sys.stderr):
                    instance = target() if contract.mode == "stateful" else None
                    if instance is not None:
                        if not callable(getattr(instance, "snapshot", None)):
                            raise TypeError("stateful adapter requires snapshot()")
                        if not callable(getattr(instance, contract.data["method"], None)):
                            raise TypeError("stateful adapter method is not callable")
                emit({"id": request_id, "reset": True})
                continue
            if request["action"] != "call":
                raise ValueError("unknown worker action")
            supplied = request["inputs"]
            if not isinstance(supplied, dict) or set(supplied) != set(contract.data["inputs"]):
                raise ValueError("case inputs do not match contract fields")
            for name, value in supplied.items():
                if type(value) is not int:
                    raise ValueError(f"input {name} must be an integer, not {type(value).__name__}")
            inputs = copy.deepcopy(supplied)
            with contextlib.redirect_stdout(sys.stderr):
                before = copy.deepcopy(instance.snapshot()) if instance is not None else None
            validate_json_value(before, "before")
            result = None
            error = None
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    function = getattr(instance, contract.data["method"]) if instance is not None else target
                    result = function(**copy.deepcopy(supplied))
                ok = True
            except Exception as exc:
                ok = False
                error = {"type": type(exc).__name__, "message": str(exc)}
            if ok:
                # Evidence copying/serialization failures are infrastructure
                # errors, not exceptions raised by the Python implementation.
                result = copy.deepcopy(result)
            with contextlib.redirect_stdout(sys.stderr):
                after = copy.deepcopy(instance.snapshot()) if instance is not None else None
            record = {"input": inputs, "before": before, "after": after,
                      "result": result, "ok": ok, "error": error}
            validate_json_value(record, "record")
            emit({"id": request_id, "record": record})
        except BaseException as exc:
            emit({"id": request_id, "error": f"{type(exc).__name__}: {exc}"})
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
