"""Strict, versioned contracts and reproducible bounded case generation."""

from dataclasses import dataclass
import hashlib
import itertools
import json
from pathlib import Path
import random
import re

from .errors import ContractError
from .expressions import compile_expression, unchanged_expression

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _integer(value, label, minimum=None, maximum=None):
    if type(value) is not int:
        raise ContractError(f"{label} must be an integer (not a Boolean)")
    if minimum is not None and value < minimum:
        raise ContractError(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{label} must be <= {maximum}")
    return value


def _string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ContractError(f"{label} must be an array of nonempty strings")
    return value


@dataclass(frozen=True)
class Contract:
    path: Path
    data: dict
    sha256: str
    requires: list
    ensures: list
    project_root: Path

    @property
    def name(self):
        return self.data["name"]

    @property
    def mode(self):
        return self.data["mode"]

    @property
    def testing(self):
        return self.data["testing"]

    @property
    def domain_size(self):
        result = 1
        for domain in self.data["inputs"].values():
            result *= domain["max"] - domain["min"] + 1
        return result


def find_project_root(path):
    for directory in (path.parent, *path.parent.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return path.parent


def load_contract(path) -> Contract:
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ContractError(f"cannot load contract {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("contract must be an object")
    allowed = {"version", "name", "target", "mode", "method", "inputs", "requires", "ensures", "unchanged", "reject_exception", "testing", "model"}
    if unknown := data.keys() - allowed:
        raise ContractError(f"unknown contract fields: {', '.join(sorted(unknown))}")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ContractError("contract version must be 1")
    for key in ["name", "target"]:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ContractError(f"{key} must be a nonempty string")
    target = data["target"].split(":")
    if len(target) != 2 or any(not part or any(not IDENTIFIER.fullmatch(x) for x in part.split(".")) for part in target):
        raise ContractError("target must have the form module.path:callable")
    mode = data.get("mode", "pure")
    if mode not in ("pure", "stateful"):
        raise ContractError("mode must be pure or stateful")
    data["mode"] = mode
    if "model" in data and data["model"] != "account.withdraw.v1":
        raise ContractError("unknown model; supported models: account.withdraw.v1")
    if "model" in data and mode != "stateful":
        raise ContractError("account.withdraw.v1 requires stateful mode")
    if mode == "stateful":
        if not isinstance(data.get("method"), str) or not IDENTIFIER.fullmatch(data["method"]):
            raise ContractError("stateful contracts require a public method name")
    elif "method" in data or data.get("unchanged"):
        raise ContractError("method and unchanged are only supported in stateful mode")
    domains = data.get("inputs")
    if not isinstance(domains, dict) or len(domains) > 20:
        raise ContractError("inputs must be an object with at most 20 fields")
    for name, domain in domains.items():
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ContractError("input names must be public Python identifiers")
        if not isinstance(domain, dict) or set(domain) != {"type", "min", "max"} or domain["type"] != "int":
            raise ContractError(f"input {name} requires exactly type=int, min, and max")
        low = _integer(domain["min"], f"{name}.min")
        high = _integer(domain["max"], f"{name}.max")
        if high < low:
            raise ContractError(f"empty input domain for {name}")
    requires = _string_list(data.get("requires", []), "requires")
    ensures = _string_list(data.get("ensures", []), "ensures")
    if not ensures:
        raise ContractError("ensures must contain at least one law")
    unchanged = _string_list(data.get("unchanged", []), "unchanged")
    reject = data.get("reject_exception")
    if reject is not None and (not isinstance(reject, str) or not IDENTIFIER.fullmatch(reject)):
        raise ContractError("reject_exception must be an exception class name")
    testing = data.get("testing", {})
    if not isinstance(testing, dict):
        raise ContractError("testing must be an object")
    if unknown := testing.keys() - {"seed", "examples", "steps", "mode", "max_cases"}:
        raise ContractError(f"unknown testing fields: {', '.join(sorted(unknown))}")
    testing = {"seed": 0, "examples": 100, "steps": 8 if mode == "stateful" else 1,
               "mode": "sample", "max_cases": 10000, **testing}
    _integer(testing["seed"], "testing.seed")
    _integer(testing["examples"], "testing.examples", 1, 100000)
    _integer(testing["steps"], "testing.steps", 1, 1000)
    _integer(testing["max_cases"], "testing.max_cases", 1, 100000)
    if testing["mode"] not in ("sample", "exhaustive"):
        raise ContractError("testing.mode must be sample or exhaustive")
    if mode == "pure" and testing["steps"] != 1:
        raise ContractError("pure contracts require testing.steps=1")
    if mode == "stateful" and testing["mode"] == "exhaustive":
        raise ContractError("exhaustive stateful histories are not supported; use sample")
    data["testing"] = testing
    contract = Contract(path, data, hashlib.sha256(raw).hexdigest(),
                        [compile_expression(x) for x in requires],
                        [compile_expression(x) for x in ensures] + [unchanged_expression(x) for x in unchanged],
                        find_project_root(path))
    if testing["mode"] == "exhaustive":
        if contract.domain_size > testing["max_cases"]:
            raise ContractError("exhaustive input domain exceeds testing.max_cases")
    elif testing["examples"] * testing["steps"] > testing["max_cases"]:
        raise ContractError("requested executions exceed testing.max_cases")
    return contract


def discover_contracts(paths):
    found = set()
    for item in paths:
        path = Path(item).resolve()
        if path.is_dir():
            found.update(path.rglob("*.laws.json"))
        elif path.is_file():
            found.add(path)
        else:
            raise ContractError(f"contract path does not exist: {path}")
    if not found:
        raise ContractError("no .laws.json contracts found")
    return sorted(found)


def generate_sequences(contract):
    """Yield bounded sequences. Only explicit exhaustive pure mode claims coverage."""
    domains = contract.data["inputs"]
    names = list(domains)
    settings = contract.testing
    if settings["mode"] == "exhaustive":
        ranges = [range(domains[name]["min"], domains[name]["max"] + 1) for name in names]
        for values in itertools.product(*ranges):
            yield [dict(zip(names, values))]
        return
    rng = random.Random(settings["seed"])
    boundaries = {}
    for name, domain in domains.items():
        lo, hi = domain["min"], domain["max"]
        candidates = [lo, hi, 0, 1, -1, lo + 1, hi - 1, (lo + hi) // 2]
        boundaries[name] = list(dict.fromkeys(x for x in candidates if lo <= x <= hi))
    for sequence_index in range(settings["examples"]):
        sequence = []
        for step_index in range(settings["steps"]):
            index = sequence_index * settings["steps"] + step_index
            values = {}
            for offset, name in enumerate(names):
                domain = domains[name]
                edge = boundaries[name]
                if index < len(edge) * max(1, len(names)):
                    values[name] = edge[(index + offset) % len(edge)]
                elif index % 3 == 0:
                    values[name] = rng.choice(edge)
                else:
                    values[name] = rng.randint(domain["min"], domain["max"])
            sequence.append(values)
        yield sequence
