from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_runner(args: list[str]) -> None:
    module_path = Path(__file__).parent / "src" / "ir-heater" / "sequence_runner.py"
    module = _load_module("sequence_runner", module_path)
    sys.argv = [sys.argv[0], *args]
    module.main()


def _run_generator(args: list[str]) -> None:
    module_path = Path(__file__).parent / "src" / "ir-heater" / "sequence_generator.py"
    module = _load_module("sequence_generator", module_path)
    sys.argv = [sys.argv[0], *args]
    module.main()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "generate":
        _run_generator(args[1:])
        return
    if args and args[0] == "run":
        _run_runner(args[1:])
        return

    # Backward-compatible default: route directly to runner flags.
    _run_runner(args)


if __name__ == "__main__":
    main()
