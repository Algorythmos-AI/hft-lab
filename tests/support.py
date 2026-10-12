"""Helpers shared by the tests: the repository root and a throwaway git repository."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "hftlab"
# The configuration as it was before any decision record set a value: round placeholder figures, limits not in
# force, two pairs. Frozen here for the tests that run a command on synthetic data and assert numbers that come
# out of those figures. They say nothing about the shipped files; the tests named "shipped" read `config/`.
PLACEHOLDER = ROOT / "tests" / "placeholder"


class GitRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.root, check=check, capture_output=True, text=True)

    def write(self, path: str, data: bytes | str = "x\n") -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        return target

    def add(self, path: str, data: bytes | str = "x\n") -> None:
        self.write(path, data)
        self.git("add", "--", path)

    def commit(self, message: str = "change") -> str:
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()


# ---- reading the package's source, for the tests that scan it ------------------------------------------------------


def sources() -> list[tuple[str, ast.Module]]:
    """(path from the repository root, parsed module) for every module of the package."""
    return [(p.relative_to(ROOT).as_posix(), ast.parse(p.read_text(), str(p))) for p in sorted(SRC.rglob("*.py"))]


def _package_of(rel: str) -> list[str]:
    parts = rel.removeprefix("src/").removesuffix(".py").split("/")
    return parts[:-1]


def absolute(node: ast.ImportFrom, rel: str) -> str:
    """The absolute module an ImportFrom names, resolving `from .x import y` against the file's package."""
    if not node.level:
        return node.module or ""
    package = _package_of(rel)
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, node.module] if node.module else base)


def called_name(node: ast.Call) -> str:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""


def runs_text(node: ast.AST) -> bool:
    """Is this a call that runs code from text: eval or exec by any route, or the builtin compile?

    `re.compile` and other methods called compile are not it; only the bare builtin or `builtins.compile` is.
    """
    if not isinstance(node, ast.Call):
        return False
    name = called_name(node)
    if name in ("eval", "exec"):
        return True
    fn = node.func
    via_builtins = isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "builtins"
    return name == "compile" and (isinstance(fn, ast.Name) or via_builtins)


def imports(tree: ast.AST, rel: str) -> list[tuple[str, str]]:
    """Every module a file can reach by name, as (how, dotted name).

    `how` is "import" for `import a.b` (which also binds the package `a`), "from" for `from a import b` (listed as
    `a.b`), "dynamic" for a constant passed to `__import__` / `import_module` or used to index `sys.modules`, and
    "computed" (with an empty name) for a dynamic import whose name the scan cannot read.
    """
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [("import", alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = absolute(node, rel)
            out += [("from", f"{module}.{alias.name}" if module else alias.name) for alias in node.names]
        elif isinstance(node, ast.Call) and called_name(node) in ("__import__", "import_module"):
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out.append(("dynamic", first.value))
            else:
                out.append(("computed", ""))
        elif isinstance(node, ast.Subscript):
            holder = node.value
            name = getattr(holder, "attr", None) or getattr(holder, "id", "")
            if name == "modules" and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                out.append(("dynamic", node.slice.value))
    return out
