"""Crash-tolerant, content-preserving persistence for experiment runs."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python


def json_text(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(to_jsonable_python(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write(path: Path, content: str, *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite immutable artifact: {path}")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if not overwrite and path.exists():
            raise FileExistsError(f"Refusing to overwrite immutable artifact: {path}")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_model(path: Path, model: BaseModel, *, overwrite: bool = True) -> None:
    atomic_write(path, json_text(model), overwrite=overwrite)


def read_model(path: Path, model_type: type[BaseModel]) -> Any:
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, values: Iterable[BaseModel | dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for value in values:
            data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            stream.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def ensure_relative(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"Expected a safe relative path: {path!r}")
    return candidate


class RunStore:
    """Only this layer writes experiment facts; agents receive read-only views."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.map_dir = self.root / "map"
        self.artifacts = self.root / "artifacts"

    def initialize(self) -> None:
        for directory in (self.map_dir / "nodes", self.artifacts / "patches", self.root / "iterations"):
            directory.mkdir(parents=True, exist_ok=True)

    def create_node(self, node: BaseModel) -> None:
        write_model(self.map_dir / "nodes" / f"{node.node_id}.json", node, overwrite=False)
        self.event("NodeCreated", node.node_id, {"parent_ids": node.parent_ids, "patch_id": node.patch_id})

    def save_node(self, node: BaseModel, previous: BaseModel | None = None) -> None:
        if previous is not None:
            immutable = ("node_id", "operator", "parent_ids", "base_revision", "candidate_revision",
                         "patch_id", "patch_path", "parent_patch_paths", "generation", "hypothesis", "semantic_changes",
                         "target_workloads", "ablates_change_id", "created_at")
            for field in immutable:
                if getattr(node, field) != getattr(previous, field):
                    raise ValueError(f"Immutable node field cannot change: {field}")
        write_model(self.map_dir / "nodes" / f"{node.node_id}.json", node)

    def load_node(self, node_id: str) -> Any:
        from .schemas import IterationNode
        return read_model(self.map_dir / "nodes" / f"{node_id}.json", IterationNode)

    def nodes(self, operator: str | None = None) -> list[Any]:
        from .schemas import IterationNode
        result = [read_model(path, IterationNode) for path in sorted((self.map_dir / "nodes").glob("*.json"))]
        return [node for node in result if operator is None or node.operator == operator]

    def add_edges(self, edges: Iterable[BaseModel]) -> None:
        append_jsonl(self.map_dir / "edges.jsonl", edges)

    def edges(self) -> list[Any]:
        from .schemas import IterationEdge
        return [IterationEdge.model_validate(item) for item in read_jsonl(self.map_dir / "edges.jsonl")]

    def add_evaluation(self, evaluation: BaseModel) -> None:
        path = self.map_dir / "evaluations" / f"{evaluation.evaluation_id}.json"
        write_model(path, evaluation, overwrite=False)
        self.event("EvaluationCompleted", evaluation.node_id,
                   {"evaluation_id": evaluation.evaluation_id, "verdict": evaluation.verdict})

    def evaluations(self, node_id: str | None = None) -> list[Any]:
        from .schemas import Evaluation
        result = [read_model(path, Evaluation) for path in sorted((self.map_dir / "evaluations").glob("*.json"))]
        return [item for item in result if node_id is None or item.node_id == node_id]

    def event(self, kind: str, node_id: str | None = None, data: dict[str, Any] | None = None) -> None:
        from .schemas import utc_now
        append_jsonl(self.map_dir / "events.jsonl", [{
            "event": kind, "node_id": node_id, "data": data or {}, "at": utc_now().isoformat(),
        }])

    def champion(self, operator: str) -> str | None:
        path = self.map_dir / "champions.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get(operator)

    def set_champion(self, operator: str, node_id: str) -> None:
        path = self.map_dir / "champions.json"
        champions = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        champions[operator] = node_id
        atomic_write(path, json_text(champions))
        self.event("ChampionChanged", node_id, {"operator": operator})
