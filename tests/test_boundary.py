"""Does the library half actually depend on nothing in the application half?

The split is `detect/`, `process/`, `memory/`, `method.py`, `model.py` as a library against
`casestudy/` and `demo/` as one application built on it. That claim is only worth stating if
something enforces the direction, because a single `from ..casestudy import eventlog` added
inside a function body makes the library unshippable while every test still passes.

Two properties, and the second is the one a reader would not guess:

**No library module imports an application module**, at module level or inside a function.
AST rather than import, so a lazy import in a rarely-taken branch is caught.

**No library module imports `polars`, `libsql`, or `pm4py`**, which is the measurable form of
"the library needs no dataframe engine and no process-mining package". Those three are the
evidence the split is real rather than tidy: all of `polars`, `libsql`, and `pm4py` are
reached only from `casestudy/`, so a library that acquires one has quietly moved the boundary.

Membership is derived from the source tree rather than listed here, so a new package under
`src/pneuma/` fails until it is declared on one side or the other. A test whose subject is a
hardcoded list keeps passing over exactly the modules it already knew about while the property
rots next door, which is the defect class `detect/` exists to catch.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from paths import SRC

PNEUMA = SRC / "pneuma"

# Declared by hand because this is the boundary itself: a new top-level module has to be
# argued onto one side, and `test_every_module_is_declared` fails until it is.
LIBRARY = {"detect", "process", "memory", "method", "model"}
APPLICATION = {"casestudy", "demo"}

# Reached only from casestudy/, so a library module importing one has moved the boundary.
APPLICATION_ONLY_PACKAGES = {"polars", "libsql", "pm4py"}


def top_level_modules() -> dict[str, Path]:
    """Every top-level name under `src/pneuma/`, mapped to its package dir or module file."""
    found: dict[str, Path] = {}
    for path in PNEUMA.iterdir():
        if path.name.startswith("_") or path.name == "__pycache__":
            continue
        if path.is_dir() and (path / "__init__.py").is_file():
            found[path.name] = path
        elif path.suffix == ".py":
            found[path.stem] = path
    return found


def library_modules() -> list[Path]:
    """Every `.py` file on the library side, derived from `LIBRARY` rather than globbed flat."""
    modules = top_level_modules()
    files: list[Path] = []
    for name in sorted(LIBRARY):
        path = modules.get(name)
        if path is None:
            continue
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


def imports_of(source: str) -> set[str]:
    """Every module this source imports, absolute and relative, including in function bodies.

    Relative imports are resolved against the file's own package so that `from ..casestudy
    import x` reports `casestudy` rather than a dot-prefixed string a caller has to decode.
    Callers pass the file's package path in via `resolve_relative`.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * (node.level or 0) + (node.module or ""))
    return names


def resolve_relative(name: str, module: Path) -> str:
    """Turn a possibly-relative import into a top-level `pneuma` name, or return it unchanged.

    `from ..casestudy import x` inside `detect/adapter.py` resolves to `casestudy`; a plain
    `math` resolves to itself. Anything that walks above `pneuma` resolves to the empty
    string, which no side claims, so it cannot silently pass a check.
    """
    if not name.startswith("."):
        return name.split(".")[0]

    level = len(name) - len(name.lstrip("."))
    target = name.lstrip(".")
    package = module.parent
    for _ in range(level - 1):
        package = package.parent
    resolved = package / target.split(".")[0] if target else package
    try:
        relative = resolved.relative_to(PNEUMA)
    except ValueError:
        return ""
    return relative.parts[0] if relative.parts else ""


def test_there_is_something_to_check() -> None:
    """Guard the guard: an empty `library_modules()` would pass every check below by
    iterating nothing, and an empty parametrisation is reported as nothing rather than as a
    failure."""
    files = library_modules()
    assert len(files) >= 10, f"only found {len(files)} library modules under {PNEUMA}"
    assert {path.name for path in files} >= {"objective.py", "ir.py", "method.py"}


def test_every_module_is_declared_on_one_side_of_the_boundary() -> None:
    """A new top-level module is neither library nor application until someone says so, and
    an undeclared one is invisible to every other check in this file."""
    undeclared = set(top_level_modules()) - LIBRARY - APPLICATION
    assert not undeclared, f"undeclared top-level modules: {sorted(undeclared)}"


@pytest.mark.parametrize("module", library_modules(), ids=lambda p: str(p.name))
def test_a_library_module_does_not_import_the_application(module: Path) -> None:
    reached = {resolve_relative(name, module) for name in imports_of(module.read_text())}
    crossings = reached & APPLICATION
    assert not crossings, f"{module.name} imports application package(s) {sorted(crossings)}"


@pytest.mark.parametrize("module", library_modules(), ids=lambda p: str(p.name))
def test_a_library_module_needs_no_dataframe_engine(module: Path) -> None:
    """`polars`, `libsql`, and `pm4py` are the measurable evidence for the split."""
    reached = {name.split(".")[0] for name in imports_of(module.read_text())}
    heavy = reached & APPLICATION_ONLY_PACKAGES
    assert not heavy, f"{module.name} imports {sorted(heavy)}, which only casestudy/ needs"


def importable_library_modules() -> list[str]:
    """Dotted names for every library module, for the runtime half of the check."""
    names: list[str] = []
    for path in library_modules():
        relative = path.relative_to(PNEUMA)
        parts = list(relative.parts)
        parts[-1] = parts[-1].removesuffix(".py")
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(["pneuma", *parts]))
    return sorted(set(names))


def blocking_import(dotted: str) -> subprocess.CompletedProcess[str]:
    """Import `dotted` in a subprocess where the application-only packages cannot be found.

    The hook is `find_spec`. `find_module` was removed from the import protocol in 3.12, so a
    blocker written against it is never consulted and every module appears to import cleanly —
    which is why `test_the_blocker_really_does_block` exists rather than being redundant.

    A subprocess per module, so one module's success cannot be another's cached `sys.modules`
    entry.
    """
    script = textwrap.dedent(f"""
        import importlib
        import sys

        BLOCKED = {sorted(APPLICATION_ONLY_PACKAGES)!r}

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ImportError(f"{{name}} is blocked: the library must not need it")
                return None

        sys.meta_path.insert(0, Blocker())
        importlib.import_module({dotted!r})
        print("imported {dotted}")
    """)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )


@pytest.mark.parametrize("dotted", importable_library_modules())
def test_a_library_module_imports_with_the_dataframe_engine_absent(dotted: str) -> None:
    """The runtime half, which the AST check can only infer.

    Reading imports proves no library file *names* `polars`. It cannot prove the module
    imports without it, because a dependency reached through another library module appears in
    neither file's own import list.
    """
    result = blocking_import(dotted)
    assert result.returncode == 0, (
        f"{dotted} needs one of {sorted(APPLICATION_ONLY_PACKAGES)}:\n"
        f"{result.stdout}{result.stderr}"
    )
    assert f"imported {dotted}" in result.stdout


def test_the_blocker_really_does_block() -> None:
    """Guard the guard: a blocker that blocks nothing passes every case above without the
    property holding, so an application module must fail under the same harness."""
    result = blocking_import("pneuma.casestudy.eventlog")

    assert result.returncode != 0, "casestudy.eventlog imported with its engine blocked"
    assert "is blocked" in result.stderr


def test_the_application_really_does_depend_on_the_library() -> None:
    """The other half of the claim. If nothing on the application side imported the library,
    the boundary would hold trivially and these checks would prove nothing about a real
    dependency direction."""
    modules = top_level_modules()
    reaching: set[str] = set()
    for name in sorted(APPLICATION):
        path = modules[name]
        for source in sorted(path.rglob("*.py")):
            reached = {resolve_relative(n, source) for n in imports_of(source.read_text())}
            reaching |= reached & LIBRARY
    assert reaching, "no application module imports the library, so the direction is untested"
    assert {"detect", "process"} <= reaching, sorted(reaching)


def test_the_application_only_packages_are_reached_from_the_application() -> None:
    """`polars`, `libsql`, and `pm4py` have to be genuinely in use, or the check above is
    forbidding imports nobody was going to write."""
    modules = top_level_modules()
    reached: set[str] = set()
    for name in sorted(APPLICATION):
        for source in sorted(modules[name].rglob("*.py")):
            reached |= {n.split(".")[0] for n in imports_of(source.read_text())}
    missing = APPLICATION_ONLY_PACKAGES - reached
    assert not missing, f"{sorted(missing)} is forbidden in the library but unused in the app"
