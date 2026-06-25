#!/usr/bin/env python3
"""Generate a Mermaid architecture diff graph for Python changes in a git branch."""

from __future__ import annotations

import argparse
import ast
import keyword
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DEFAULT_MAX_EDGES = 450


TYPE_SUFFIXES = (
    "Service",
    "Repository",
    "Client",
    "DTO",
    "Mapper",
    "View",
    "Serializer",
    "Command",
    "Task",
    "Model",
    "Controller",
    "Handler",
)

BUILTIN_NAMES = set(dir(__builtins__)) | set(keyword.kwlist)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Entity:
    """A class or function discovered in changed Python files."""

    name: str
    kind: str
    file: Path
    lineno: int
    references: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        suffix = classify_name(self.name)
        if suffix:
            return f"{self.name} [{suffix}]"
        return self.name


def run_git(args: list[str]) -> str:
    """Run a git command and return stdout, failing with a useful message."""

    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SystemExit("git is required but was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = decode_git_output(exc.stderr)
        stdout = decode_git_output(exc.stdout)
        message = stderr.strip() or stdout.strip() or str(exc)
        raise SystemExit(f"git {' '.join(args)} failed: {message}") from exc
    return decode_git_output(result.stdout)


def decode_git_output(output: bytes | str | None) -> str:
    """Decode git output without relying on the user's terminal code page.

    On Windows, ``subprocess.run(..., text=True)`` uses the active locale by
    default (for example cp1251). Git paths and diffs are commonly UTF-8, so
    locale decoding can crash before we get a normal git error/result. Decode
    bytes ourselves and replace any invalid sequences so architecture
    generation remains best-effort.
    """

    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return output.decode("utf-8", errors="replace")


def ref_exists(ref: str) -> bool:
    """Return True when git can resolve ref as a commit."""

    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return False
    return True


def resolve_base_ref(base_ref: str | None) -> str:
    """Resolve the requested base ref, with friendly defaults for fresh repos."""

    candidates: list[str]
    if base_ref:
        candidates = [base_ref]
        if base_ref == "main":
            candidates.extend(["origin/main", "master", "origin/master"])
        elif base_ref == "master":
            candidates.extend(["origin/master", "main", "origin/main"])
    else:
        candidates = ["main", "origin/main", "master", "origin/master"]

    for candidate in candidates:
        if ref_exists(candidate):
            return candidate

    requested = base_ref or "auto-detected base ref"
    tried = ", ".join(candidates)
    raise SystemExit(
        f"Could not resolve {requested!r}. Tried: {tried}. "
        "Pass an existing branch or commit, for example `python archdiff.py master`."
    )


def changed_python_files(base_ref: str) -> list[Path]:
    """Return Python files changed between base_ref and HEAD."""

    output = run_git(["diff", "--name-only", f"{base_ref}...HEAD"])
    files = []
    for line in output.splitlines():
        path = Path(line.strip())
        if path.suffix == ".py" and path.exists():
            files.append(path)
    return files


def added_top_level_names(base_ref: str) -> set[str]:
    """Find class and function names introduced by added diff lines."""

    output = run_git(["diff", "--unified=0", f"{base_ref}...HEAD", "--", "*.py"])
    names: set[str] = set()
    for line in output.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        stripped = line[1:].lstrip()
        class_match = re.match(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\b", stripped)
        func_match = re.match(r"(?:async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\b", stripped)
        match = class_match or func_match
        if match:
            names.add(match.group(1))
    return names


def parse_entities(path: Path) -> list[Entity]:
    """Parse top-level classes and functions from a Python file."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"warning: skipped {path}: {exc}", file=sys.stderr)
        return []

    entities: list[Entity] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            entities.append(Entity(node.name, "class", path, node.lineno, collect_references(node)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entities.append(Entity(node.name, "function", path, node.lineno, collect_references(node)))
    return entities


def collect_references(node: ast.AST) -> set[str]:
    """Collect likely dependency names referenced inside an AST node."""

    refs: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            refs.add(child.id)
        elif isinstance(child, ast.Attribute):
            refs.add(child.attr)
        elif isinstance(child, ast.Call):
            called = call_name(child.func)
            if called:
                refs.add(called)
    return {ref for ref in refs if is_interesting_name(ref)}


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def is_interesting_name(name: str) -> bool:
    if name in BUILTIN_NAMES or name.startswith("__"):
        return False
    if not IDENTIFIER_RE.match(name):
        return False
    return name[:1].isupper() or classify_name(name) is not None


def classify_name(name: str) -> str | None:
    for suffix in TYPE_SUFFIXES:
        if name.endswith(suffix):
            return suffix
    return None


def node_id(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not safe or safe[0].isdigit():
        safe = f"N_{safe}"
    return safe


def mermaid_label(label: str) -> str:
    return label.replace('"', "'")


def build_graph(base_ref: str) -> tuple[list[Entity], dict[str, set[str]]]:
    files = changed_python_files(base_ref)
    added_names = added_top_level_names(base_ref)
    entities = [entity for file in files for entity in parse_entities(file)]

    if added_names:
        primary_entities = [entity for entity in entities if entity.name in added_names]
    else:
        primary_entities = entities

    known_names = {entity.name for entity in entities}
    graph: dict[str, set[str]] = {}
    for entity in primary_entities:
        refs = entity.references - {entity.name}
        internal_refs = refs & known_names
        context_refs = refs - known_names
        graph[entity.name] = internal_refs | {ref for ref in context_refs if ref[:1].isupper()}
    return primary_entities, graph


def render_markdown(
    base_ref: str,
    entities: list[Entity],
    graph: dict[str, set[str]],
    max_edges: int | None = DEFAULT_MAX_EDGES,
) -> str:
    lines = ["# Architecture diff", "", f"Base ref: `{base_ref}`", "", "```mermaid", "graph TD"]

    total_edges = sum(len(targets) for targets in graph.values())
    rendered_edges = 0
    omitted_edges = 0
    visible_sources: set[str] = set()
    visible_targets: set[str] = set()

    for source in sorted(graph):
        targets = sorted(graph[source])
        if not targets:
            lines.append(f'    {node_id(source)}["{mermaid_label(source)}"]')
            continue

        for target in targets:
            if max_edges is not None and rendered_edges >= max_edges:
                omitted_edges += 1
                continue

            visible_sources.add(source)
            visible_targets.add(target)
            rendered_edges += 1
            lines.append(
                f'    {node_id(source)}["{mermaid_label(source)}"] --> '
                f'{node_id(target)}["{mermaid_label(target)}"]'
            )

    visible_entity_names = visible_sources | visible_targets
    for entity in entities:
        if entity.name not in visible_entity_names:
            visible_entity_names.add(entity.name)
            lines.append(f'    {node_id(entity.name)}["{mermaid_label(entity.label)}"]')

    entity_names = {entity.name for entity in entities}
    added_ids = ",".join(node_id(entity.name) for entity in entities if entity.name in visible_entity_names)
    context_ids = ",".join(sorted(node_id(name) for name in visible_targets - entity_names))
    lines.extend(["", "    classDef added fill:#a5d6a7,stroke:#2e7d32,color:#000", "    classDef context fill:#fff59d,stroke:#f9a825,color:#000"])
    if added_ids:
        lines.append(f"    class {added_ids} added")
    if context_ids:
        lines.append(f"    class {context_ids} context")
    lines.extend(["```", ""])

    if omitted_edges:
        lines.extend([
            "> Note: Mermaid edge output was limited to "
            f"{rendered_edges} of {total_edges} edges so the graph can render in "
            "viewers with the default 500-edge limit.",
            "> Re-run with `--max-edges N` to choose another limit, or "
            "`--max-edges 0` to render all edges if your viewer calls "
            "`mermaid.initialize` with a higher `maxEdges` value.",
            "",
        ])

    if entities:
        lines.extend(["## Added / changed entities", ""])
        for entity in sorted(entities, key=lambda item: (str(item.file), item.lineno, item.name)):
            lines.append(f"- `{entity.name}` ({entity.kind}) - `{entity.file}:{entity.lineno}`")
        lines.append("")
    else:
        lines.extend(["No changed Python entities found.", ""])
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Mermaid architecture graph for the current git diff.")
    parser.add_argument(
        "base_ref",
        nargs="?",
        help="Base git ref to compare against. Defaults to main/origin/main/master/origin/master.",
    )
    parser.add_argument(
        "--max-edges",
        type=int,
        default=DEFAULT_MAX_EDGES,
        help=(
            "Maximum Mermaid edges to render. Defaults to 450 to stay below "
            "the common 500-edge viewer limit. Use 0 to render all edges."
        ),
    )
    args = parser.parse_args(argv)

    base_ref = resolve_base_ref(args.base_ref)
    max_edges = None if args.max_edges == 0 else args.max_edges
    if args.max_edges < 0:
        parser.error("--max-edges must be 0 or a positive integer")

    entities, graph = build_graph(base_ref)
    print(render_markdown(base_ref, entities, graph, max_edges=max_edges))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())