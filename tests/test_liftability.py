"""Can the deterministic detectors be lifted out of this project unchanged?

`detect/` claims two things about itself: that `discrimination`, `vacuity`, and `objective`
depend on nothing but the standard library, and that `adapter` is the single seam binding them
to pneuma's `Process` IR. That claim was asserted in a docstring and false at package level:
`__init__.py` imported `.adapter` eagerly, so `from pneuma.detect import probe` transitively
required `pneuma.process.ir` and therefore pydantic. The three files were pure; the package
was not, and nothing measured the difference.

This module measures it, three ways, because each catches a regression the others miss:

**AST** reads every import in each module without executing it, so it catches a `pneuma`
import added inside a function body where a smoke-test import would never reach it.

**Subprocess** copies the package to a scratch directory with no `pneuma` parent on the path
and imports it under `-S`, which drops `site-packages` and therefore every third-party
package. An import that survives that is genuinely stdlib-only. This is the check that would
have failed before the fix.

**Membership** derives which modules to check from the directory rather than from a list
written here, so adding a fourth deterministic module puts it under test automatically and
adding a pneuma-dependent one fails until it is declared a seam. A test whose subject is a
hardcoded list of names is the vacuity defect this whole package exists to catch: it keeps
passing over exactly the modules it already knew about while the property rots next door.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

DETECT = Path(__file__).resolve().parents[1] / "src" / "pneuma" / "detect"

# The seams: files allowed to import from pneuma or from third-party packages. Everything
# else under detect/ must be liftable. Named here rather than the liftable set so that a new
# module is liftable-by-default and has to be argued into this list.
SEAMS = {"adapter.py", "adversary.py"}


def liftable_modules() -> list[Path]:
    """Every module under detect/ that is not a declared seam, derived from the directory."""
    return sorted(
        path
        for path in DETECT.glob("*.py")
        if path.name != "__init__.py" and path.name not in SEAMS
    )


def imported_roots(source: str) -> set[str]:
    """Every top-level module name this source imports, including inside function bodies.

    Relative imports come back as `.`-prefixed so a caller can tell `from .vacuity import x`
    (fine, intra-package) from `from ..process.ir import x` (a pneuma dependency).
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("." * node.level + (node.module or "").split(".")[0])
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_there_is_something_to_check() -> None:
    """Guard the guard: if `liftable_modules` silently returned nothing, every AST test
    below would pass by iterating an empty list."""
    names = {path.name for path in liftable_modules()}
    assert names == {"discrimination.py", "objective.py", "vacuity.py"}, names
    assert all((DETECT / seam).is_file() for seam in SEAMS)


@pytest.mark.parametrize("module", liftable_modules(), ids=lambda p: p.name)
def test_a_liftable_module_imports_only_the_standard_library(module: Path) -> None:
    """No third-party import, at module level or inside a function."""
    third_party = {
        root
        for root in imported_roots(module.read_text())
        if not root.startswith(".") and root not in sys.stdlib_module_names
    }
    assert not third_party, f"{module.name} imports non-stdlib {sorted(third_party)}"


@pytest.mark.parametrize("module", liftable_modules(), ids=lambda p: p.name)
def test_a_liftable_module_reaches_no_further_up_than_its_own_package(module: Path) -> None:
    """`from .discrimination import x` is fine; `from ..process.ir import x` is the defect.

    Anything above one dot leaves `detect/` and so cannot travel with it.
    """
    escaping = {root for root in imported_roots(module.read_text()) if root.startswith("..")}
    assert not escaping, f"{module.name} imports from outside detect/: {sorted(escaping)}"


def test_the_package_itself_does_not_eagerly_import_a_seam() -> None:
    """`__init__.py` may name a seam, but not in an import that runs at import time.

    This is the regression that was live: `from .adapter import audit_process` at module
    level. A lazy `__getattr__` or a `TYPE_CHECKING` block is fine because neither executes
    on import.
    """
    tree = ast.parse((DETECT / "__init__.py").read_text())
    seam_modules = {seam.removesuffix(".py") for seam in SEAMS}

    eager: list[str] = []
    for node in tree.body:  # top level only: nested imports do not run on import
        if isinstance(node, ast.ImportFrom) and (node.module or "") in seam_modules:
            eager.append(node.module or "")
        elif isinstance(node, ast.Import):
            eager.extend(
                alias.name for alias in node.names if alias.name.split(".")[-1] in seam_modules
            )
    assert not eager, f"__init__.py eagerly imports seam(s) {eager}"


def _lift_to(destination: Path, *, synthesize_init: bool = False) -> Path:
    """Copy detect/ somewhere with no pneuma parent anywhere above it.

    `synthesize_init` replaces `__init__.py` with an empty one. Importing a submodule runs its
    package's `__init__` first, so without this a single bad import in `objective` fails the
    `discrimination` case too and the report points at the wrong file. Attribution matters in
    a regression test, so per-module checks synthesize a bare package root and
    `test_the_package_itself_*` covers the real `__init__` separately.
    """
    package = destination / "detect"
    shutil.copytree(DETECT, package, ignore=shutil.ignore_patterns("__pycache__"))
    if synthesize_init:
        (package / "__init__.py").write_text("")
    return package


def _import_in_isolation(scratch: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run `body` in a subprocess that can see the copied package and nothing else.

    `-S` skips `site-packages`, so pydantic, polars, and strands are all unavailable and an
    import that succeeds is genuinely stdlib-only. `-I` additionally ignores PYTHONPATH and
    the user site directory, so a developer's environment cannot make this pass. The scratch
    directory is prepended to `sys.path` inside the script instead.
    """
    script = f"import sys; sys.path.insert(0, {str(scratch)!r})\n" + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_isolation_really_is_isolated(tmp_path: Path) -> None:
    """Guard the guard again: if `-S` stopped excluding site-packages, or if `pneuma` were
    importable in the subprocess, every isolation test below would pass without the property
    holding. Assert the environment is hostile before trusting a success in it."""
    result = _import_in_isolation(
        tmp_path,
        """
        for name in ("pneuma", "pydantic", "polars"):
            try:
                __import__(name)
            except ImportError:
                print(f"{name}: absent")
            else:
                raise SystemExit(f"{name} is importable, so this test proves nothing")
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("pneuma", "pydantic", "polars"):
        assert f"{name}: absent" in result.stdout


@pytest.mark.parametrize("module", liftable_modules(), ids=lambda p: p.name)
def test_a_liftable_module_imports_with_no_pneuma_and_no_third_party(
    module: Path, tmp_path: Path
) -> None:
    """The measurement behind the claim: actually import it where pneuma does not exist."""
    _lift_to(tmp_path, synthesize_init=True)
    result = _import_in_isolation(
        tmp_path,
        f"""
        import detect.{module.stem} as lifted
        print("imported", lifted.__name__)
        """,
    )
    assert result.returncode == 0, (
        f"{module.name} does not import standalone:\n{result.stdout}{result.stderr}"
    )


def test_the_package_imports_standalone_and_still_offers_the_probe_one_liner(
    tmp_path: Path,
) -> None:
    """`from detect import probe, Domain, Space, Structure` is the documented entry point for
    a training loop, and it has to survive the lift or the lift is not useful."""
    _lift_to(tmp_path)
    result = _import_in_isolation(
        tmp_path,
        """
        from detect import Discrimination, Domain, Space, Structure, probe, probe_feedback
        report = probe(
            lambda x: x,
            [Domain(name="x", low=0.0, high=1.0)],
            space=Space.DECISION,
            structure=Structure(size=lambda point: point["x"], units="units"),
        )
        print("probed:", type(report).__name__)
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probed: Probe" in result.stdout


def test_the_seam_is_the_only_thing_that_fails_to_lift(tmp_path: Path) -> None:
    """The other half of the claim. If `adapter` imported standalone too, then either it is
    not the seam or the seam moved, and the docstring's story about what to replace is wrong.
    """
    _lift_to(tmp_path)
    result = _import_in_isolation(
        tmp_path,
        """
        try:
            import detect.adapter
        except ImportError as error:
            print("adapter needs a parent, as documented:", type(error).__name__)
        else:
            raise SystemExit("adapter imported standalone, so it is not the pneuma seam")
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "adapter needs a parent" in result.stdout


def test_the_lazy_adapter_surface_still_resolves_inside_pneuma() -> None:
    """Lazy must not mean gone: the flat one-liner in the package docstring keeps working.

    Every adapter name reachable as a package attribute, and `Sweep` deliberately not
    reachable, because `vacuity.Sweep` and `objective.Sweep` are unrelated types that
    collided on that name.
    """
    from pneuma import detect

    for name in detect.__all__:
        assert getattr(detect, name) is not None, name

    assert detect.audit_process.__module__ == "pneuma.detect.adapter"
    assert detect.witness_counts.__module__ == "pneuma.detect.adapter"

    from pneuma.detect import objective, vacuity

    assert detect.ReachabilitySweep is vacuity.Sweep
    assert vacuity.Sweep is not objective.Sweep
    with pytest.raises(AttributeError):
        detect.Sweep  # noqa: B018


def test_no_two_reexported_names_bind_different_types() -> None:
    """The `Sweep` collision generalised, so the next one fails here rather than shadowing
    silently. Both source modules are checked for every name the package re-exports."""
    from pneuma import detect
    from pneuma.detect import discrimination, objective, vacuity

    sources = {"vacuity": vacuity, "objective": objective, "discrimination": discrimination}
    collisions: dict[str, list[str]] = {}
    for name in (n for n in dir(detect) if not n.startswith("_")):
        owners = [label for label, module in sources.items() if hasattr(module, name)]
        bindings = {id(getattr(sources[label], name)) for label in owners}
        if len(owners) > 1 and len(bindings) > 1:
            collisions[name] = sorted(owners)
    assert not collisions, f"re-exported names bound to different types: {collisions}"
