"""Command-line interface for experiment initialization, iteration, and maps."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from .ablation import plan_ablations
from .driver import ExperimentDriver, SimulationAgent
from .map import IterationMap
from .runners.simulated import SimulatedRunner
from .schemas import RunConfig
from .storage import RunStore, json_text
from .workspace import git


def project_root() -> Path:
    for candidate in (Path.cwd(), Path(__file__).resolve().parents[3]):
        if (candidate / "examples" / "sim_kernel").exists():
            return candidate
    raise FileNotFoundError("Cannot locate examples/sim_kernel; run from the project directory")


def load_config(filename: str) -> RunConfig:
    config_path = Path(filename).resolve()
    config = RunConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    root = config_path.parent
    repository = (root / config.repository).resolve()
    if not repository.exists():
        source = project_root() / "examples" / "sim_kernel"
        if (source / "kernel.py").exists():
            initialize_fixture(repository)
    config.repository = repository
    if config.private_holdout:
        config.private_holdout = (root / config.private_holdout).resolve()
    return config


def initialize_fixture(repository: Path) -> None:
    """Create a disposable source repository, leaving the real FlagGems untouched."""
    if repository.exists():
        raise ValueError(f"Refusing to overwrite fixture repository: {repository}")
    repository.mkdir(parents=True)
    source = project_root() / "examples" / "sim_kernel" / "kernel.py"
    shutil.copyfile(source, repository / "kernel.py")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "kernel.py"], check=True)
    subprocess.run([
        "git", "-C", str(repository), "-c", "user.name=RSI Fixture",
        "-c", "user.email=fixture@localhost", "commit", "-qm", "Initial simulation kernel",
    ], check=True)


def driver_for(config: RunConfig, run: Path, *, offline: bool = False) -> ExperimentDriver:
    if offline:
        agent = SimulationAgent(config, RunStore(run))
    else:
        from .agent import ClaudeResearchAgent
        store = RunStore(run)
        agent = ClaudeResearchAgent(config, run, IterationMap(store), SimulatedRunner(config, run))
    return ExperimentDriver(config, run, agent=agent)


def add_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flaggems-rsi", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize a run and immutable baseline")
    init.add_argument("--config", required=True)
    init.add_argument("--run", type=Path)
    for name in ("run", "resume"):
        item = commands.add_parser(name, help="Run or resume single-candidate iterations")
        add_run(item)
        item.add_argument("--max-iterations", type=int, default=None)
        item.add_argument("--offline", action="store_true", help="Use deterministic fixture agent; no API calls")
        item.add_argument("--auto-promote", action="store_true", help="Explicitly enable automatic Champion promotion")
    show = commands.add_parser("map", help="Inspect the immutable lineage DAG")
    map_commands = show.add_subparsers(dest="map_command", required=True)
    for name in ("show", "path", "diff"):
        item = map_commands.add_parser(name)
        add_run(item)
        if name == "show":
            item.add_argument("--operator")
        elif name == "path":
            item.add_argument("--node", required=True)
        else:
            item.add_argument("--left", required=True)
            item.add_argument("--right", required=True)
    ablation = commands.add_parser("ablation", help="Plan or execute explicit fixture ablations")
    ablation_commands = ablation.add_subparsers(dest="ablation_command", required=True)
    for name in ("plan", "run"):
        item = ablation_commands.add_parser(name)
        add_run(item)
        item.add_argument("--node", required=True)
    promote = commands.add_parser("promote", help="Manually promote a candidate with an ACCEPT evaluation")
    add_run(promote)
    promote.add_argument("--node", required=True)
    return parser


async def execute(args: argparse.Namespace) -> int:
    if args.command == "init":
        config = load_config(args.config)
        run = (args.run or Path("runs") / config.run_id).resolve()
        if run.exists():
            raise ValueError(f"Refusing to overwrite existing run: {run}")
        run.mkdir(parents=True)
        if config.mode == "simulated" and not config.repository.exists():
            initialize_fixture(config.repository)
        driver = ExperimentDriver(config, run)
        baseline = driver.initialize()
        print(json_text({"run": str(run), "baseline": baseline.node_id}), end="")
        return 0
    if args.command in {"run", "resume"}:
        config = RunConfig.model_validate_json((args.run.resolve() / "config.json").read_text())
        if args.auto_promote:
            config.agent.auto_promote = True
        driver = driver_for(config, args.run.resolve(), offline=args.offline)
        nodes = await driver.run(args.max_iterations)
        print(json_text({"nodes": nodes}), end="")
        return 0
    config = RunConfig.model_validate_json((args.run.resolve() / "config.json").read_text())
    store = RunStore(args.run.resolve())
    iteration_map = IterationMap(store)
    if args.command == "map":
        if args.map_command == "show":
            print(iteration_map.render(args.operator or config.operator))
        elif args.map_command == "path":
            print(json_text(iteration_map.path(args.node)), end="")
        else:
            print(iteration_map.diff(args.left, args.right))
        return 0
    if args.command == "ablation":
        plans = plan_ablations(config, iteration_map, args.node)
        print(json_text(plans), end="")
        if args.ablation_command == "run":
            driver = ExperimentDriver(config, args.run.resolve())
            nodes = await driver.run_ablation(args.node)
            print(json_text({"ablation_nodes": nodes}), end="")
        return 0
    if args.command == "promote":
        node = store.load_node(args.node)
        evaluations = store.evaluations(args.node)
        accepted = next((item for item in reversed(evaluations) if item.verdict == "ACCEPT"), None)
        if accepted is None:
            raise ValueError("No ACCEPT evaluation exists for this candidate")
        promoted = iteration_map.promote(node, accepted.evaluation_id)
        print(json_text(promoted), end="")
        return 0
    return 2


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(execute(args)))
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
