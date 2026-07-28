"""Stages 2 and 3 -- Plan and Gate: the change as reviewable data.

A plan is a list of exact character-span replacements, the blast radius it
computed, and everything the index refused to resolve. It is data before it is
an action: printable, diffable, gateable, and applied only by the transaction
stage. That ordering is the point -- a refactor you can read before it runs is
the difference between a tool and a gamble.

Three commitments, each of which cost a design option:

  * **Splice, never unparse.** Rewrites replace identifier spans only, so
    comments, blank lines, string contents, and idiosyncratic formatting come
    through byte-identical. A codemod that reformats a file while fixing one
    name produces a diff nobody can review, which means nobody does.

  * **Refuse rather than guess.** A rename into a name that already exists in
    an affected module, a target that is not an identifier, a symbol the index
    does not know -- all raise before a plan exists. The unresolved entries the
    index collected (star imports, `getattr`) travel with the plan so a gate
    can decline a change that cannot be proven complete.

  * **The blast radius is the gate's input, not a footnote.** `requires_review`
    is true for a public symbol, a change crossing module boundaries, or any
    unresolved reference -- which is `gate.py`'s doctrine applied to source
    code: refuse before the effect, at the only point where refusal is free.
"""

from __future__ import annotations

import ast
import difflib
import keyword
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .index import BlastRadius, IndexError_, SymbolIndex, Unresolved

__all__ = [
    "Plan",
    "PlanError",
    "Rewrite",
    "plan_transform",
    "remove_unused_imports",
    "rename_symbol",
]


class PlanError(RuntimeError):
    """A plan could not be built. Raised before anything exists to apply."""


@dataclass(frozen=True)
class Rewrite:
    """Replace `[start, end)` in `path` with `replacement`. Character offsets,
    absolute within the file."""

    path: str                      # posix, relative to the index root
    start: int
    end: int
    replacement: str
    reason: str

    def describe(self) -> dict:
        return {"path": self.path, "start": self.start, "end": self.end,
                "replacement": self.replacement, "reason": self.reason}


@dataclass(frozen=True)
class Plan:
    """A complete, reviewable change."""

    description: str
    rewrites: Tuple[Rewrite, ...]
    blast_radius: Optional[BlastRadius] = None
    unresolved: Tuple[Unresolved, ...] = ()
    _sources: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def paths(self) -> Tuple[str, ...]:
        return tuple(sorted({r.path for r in self.rewrites}))

    @property
    def is_empty(self) -> bool:
        return not self.rewrites

    @property
    def requires_review(self) -> bool:
        """Whether a human should sign this off before it is applied.

        True when the change is public API, crosses a module boundary, or
        rests on an index that could not resolve everything. Deliberately
        conservative: the cost of an unnecessary review is a minute, and the
        cost of an unreviewed cross-module rename is a broken build nobody
        expected.
        """
        if self.unresolved:
            return True
        if self.blast_radius is not None:
            return self.blast_radius.is_wide
        return len(self.paths) > 1

    def new_source(self, path: str) -> Optional[str]:
        """The post-rewrite text of one file, or None if it is untouched.

        Rewrites are applied back-to-front so that each splice cannot shift
        the offsets of the ones not yet applied -- the classic way a
        multi-edit rewriter corrupts a file.
        """
        original = self._sources.get(path)
        if original is None:
            return None
        applicable = sorted((r for r in self.rewrites if r.path == path),
                            key=lambda r: r.start, reverse=True)
        if not applicable:
            return None
        out = original
        previous_start = len(original) + 1
        for rewrite in applicable:
            if rewrite.end > previous_start:
                raise PlanError(
                    f"overlapping rewrites in {path} at {rewrite.start}-"
                    f"{rewrite.end}; refusing to apply a plan whose edits "
                    f"collide")
            out = out[:rewrite.start] + rewrite.replacement + out[rewrite.end:]
            previous_start = rewrite.start
        return out

    def to_diff(self, context: int = 2) -> str:
        """A unified diff of the whole plan -- what review actually looks at."""
        chunks: List[str] = []
        for path in self.paths:
            original = self._sources.get(path, "")
            updated = self.new_source(path) or original
            chunks.extend(difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}", n=context))
        return "".join(chunks)

    def describe(self) -> dict:
        return {
            "description": self.description,
            "paths": list(self.paths),
            "rewrites": len(self.rewrites),
            "requires_review": self.requires_review,
            "blast_radius": self.blast_radius.describe() if self.blast_radius else None,
            "unresolved": [{"module": u.module, "reason": u.reason,
                            "detail": u.detail} for u in self.unresolved],
        }

    def format_text(self) -> str:
        lines = [self.description,
                 f"  {len(self.rewrites)} edit(s) across {len(self.paths)} file(s)"]
        for path in self.paths:
            count = sum(1 for r in self.rewrites if r.path == path)
            lines.append(f"    {path}: {count}")
        if self.blast_radius:
            radius = self.blast_radius
            lines.append(f"  blast radius: {radius.reference_count} reference(s) in "
                         f"{len(radius.modules)} module(s); "
                         f"{'public' if radius.is_public else 'private'} symbol")
        for entry in self.unresolved:
            lines.append(f"  UNRESOLVED [{entry.reason}] {entry.module}: {entry.detail}")
        lines.append(f"  requires review: {self.requires_review}")
        return "\n".join(lines)


# -- transforms -------------------------------------------------------------------

def rename_symbol(index: SymbolIndex, qualname: str, new_name: str) -> Plan:
    """Rename a module-level symbol and every reference the index can prove.

    Rewrites the definition, bare-name uses, `module.attr` uses, and the name
    inside `from module import name`. An aliased import (`import name as n`)
    has its imported name rewritten and its call sites left alone, because
    they say `n` and always did.
    """
    if not new_name.isidentifier() or keyword.iskeyword(new_name):
        raise PlanError(f"{new_name!r} is not a valid Python identifier")

    definition = index.definitions.get(qualname)
    if definition is None:
        raise IndexError_(f"no definition named {qualname!r} in the index")
    if definition.name == new_name:
        raise PlanError(f"{qualname} is already named {new_name!r}")

    radius = index.blast_radius(qualname)

    # A collision check across every module the change touches. Renaming into
    # an existing name silently merges two symbols -- code that imports and
    # then compiles, and means something different.
    for module in radius.modules:
        collision = f"{module}.{new_name}"
        if collision in index.definitions:
            raise PlanError(
                f"cannot rename {qualname} to {new_name!r}: {collision} already "
                f"exists. Renaming into an existing name merges two symbols into "
                f"one, which compiles and is wrong.")

    # Deduplicated by span: a module-level `helper = 1` is BOTH the definition
    # and a Name node, so the same characters arrive twice. Two rewrites over
    # one span is an overlap the applier would refuse -- collapse them here,
    # where the reason for the duplicate is known.
    seen: Set[Tuple[str, int, int]] = set()
    rewrites: List[Rewrite] = []

    def add(path: str, start: int, end: int, reason: str) -> None:
        key = (path, start, end)
        if key in seen:
            return
        seen.add(key)
        rewrites.append(Rewrite(path=path, start=start, end=end,
                                replacement=new_name, reason=reason))

    add(_path_of(index, definition.module), definition.span.start,
        definition.span.end, f"definition of {qualname}")

    for reference in index.references_to(qualname):
        add(_path_of(index, reference.module), reference.span.start,
            reference.span.end, f"{reference.kind} reference to {qualname}")

    return Plan(
        description=f"rename {qualname} -> {new_name}",
        rewrites=tuple(rewrites),
        blast_radius=radius,
        unresolved=radius.unresolved,
        _sources=_sources_for(index, radius.modules),
    )


def remove_unused_imports(index: SymbolIndex,
                          modules: Optional[Sequence[str]] = None) -> Plan:
    """Remove imported names that the module never uses.

    Deliberately conservative, because an unused import is sometimes load
    bearing:

      * `__future__` imports are never touched -- they are compiler
        directives, not imports. `from __future__ import annotations` binds a
        name nothing reads, and removing it silently changes how every
        annotation in the file is evaluated;
      * `__init__.py` is skipped entirely -- re-exporting is its job;
      * anything named in `__all__` is kept, for the same reason;
      * `import x.y` (dotted, unaliased) is kept: it is frequently imported
        for the side effect of registering something;
      * a `try:`/`except ImportError:` guard is kept -- the import exists to
        be attempted, not to be used.

    What is left is the genuinely dead case: a name bound by an import and
    never read anywhere in the file.
    """
    targets = list(modules) if modules is not None else list(index.modules)
    rewrites: List[Rewrite] = []
    touched: Set[str] = set()

    for module in targets:
        info = index.modules.get(module)
        if info is None or info.path.name == "__init__.py":
            continue

        used = _names_used(info.tree)
        exported = _dunder_all(info.tree)
        guarded = _guarded_import_lines(info.tree)

        for node in ast.walk(info.tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if node.lineno in guarded:
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue                       # a compiler directive, not an import
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                continue

            dead = []
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if isinstance(node, ast.Import) and not alias.asname and "." in alias.name:
                    continue                       # side-effect import, kept
                if bound in used or bound in exported:
                    continue
                dead.append(alias)

            if not dead:
                continue
            names = ", ".join(a.asname or a.name for a in dead)
            if len(dead) == len(node.names):
                span = _statement_span(index, info, node)
                rewrites.append(Rewrite(
                    path=_path_of(index, module), start=span[0], end=span[1],
                    replacement="", reason=f"unused import: {names}"))
            elif node.lineno == node.end_lineno:
                # Partial removal rewrites the whole statement with the
                # survivors. Per-name spans would collide whenever two dead
                # names are adjacent -- each wants the comma between them --
                # and a plan whose own edits overlap is refused at apply time.
                survivors = [a for a in node.names if a not in dead]
                span = _statement_span(index, info, node)
                indent = info.source[span[0]:index._offset(info, node.lineno,
                                                           node.col_offset)]
                trailing = "\n" if info.source[span[1] - 1:span[1]] == "\n" else ""
                rewrites.append(Rewrite(
                    path=_path_of(index, module), start=span[0], end=span[1],
                    replacement=indent + _regenerate_import(node, survivors) + trailing,
                    reason=f"unused import: {names}"))
            else:
                # A multi-line (usually parenthesised) import would have to be
                # reformatted to edit it safely, and reformatting a file while
                # tidying one name produces a diff nobody reviews. Left alone.
                continue
            touched.add(module)

    return Plan(
        description="remove unused imports",
        rewrites=tuple(rewrites),
        blast_radius=None,
        unresolved=tuple(u for u in index.unresolved if u.module in touched),
        _sources=_sources_for(index, sorted(touched)),
    )


# -- stage 4 bridge ------------------------------------------------------------------

def plan_transform(plan: Plan) -> Callable:
    """Adapt a `Plan` into the `Transform` the `ShadowTree` expects, so a plan
    flows into the existing transactional apply stage unchanged."""

    def transform(module) -> Optional[str]:
        return plan.new_source(module.rel)

    transform.__qualname__ = f"plan[{plan.description}]"
    return transform


# -- helpers ---------------------------------------------------------------------------

def _path_of(index: SymbolIndex, module: str) -> str:
    return index.modules[module].path.relative_to(index.root).as_posix()


def _sources_for(index: SymbolIndex, modules: Sequence[str]) -> Dict[str, str]:
    return {_path_of(index, m): index.modules[m].source
            for m in modules if m in index.modules}


def _names_used(tree: ast.Module) -> Set[str]:
    """Every bare name read anywhere, plus the root of every attribute chain
    and every name mentioned in a string annotation-ish position. Erring
    toward 'used' is the safe direction for a removal transform."""
    used: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            current = node
            while isinstance(current, ast.Attribute):
                current = current.value
            if isinstance(current, ast.Name):
                used.add(current.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # `x: "Helper"` and `cast("Helper", ...)` keep an import alive.
            for token in node.value.replace("[", " ").replace("]", " ").split():
                candidate = token.strip("'\"|, ").split(".")[0]
                if candidate.isidentifier():
                    used.add(candidate)
    return used


def _dunder_all(tree: ast.Module) -> Set[str]:
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                value = node.value
                if isinstance(value, (ast.List, ast.Tuple)):
                    return {element.value for element in value.elts
                            if isinstance(element, ast.Constant)
                            and isinstance(element.value, str)}
    return set()


def _guarded_import_lines(tree: ast.Module) -> Set[int]:
    """Line numbers of imports inside a try/except -- those exist to be
    attempted, and removing one changes behaviour rather than tidying it."""
    lines: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    lines.add(child.lineno)
    return lines


def _statement_span(index: SymbolIndex, info, node: ast.AST) -> Tuple[int, int]:
    """The whole statement including its trailing newline, so removing it does
    not leave a blank line where a line used to be."""
    start = index._offset(info, node.lineno, node.col_offset)
    end = index._offset(info, node.end_lineno, node.end_col_offset)
    # take the line's indentation too
    line_start = info.source.rfind("\n", 0, start) + 1
    if info.source[line_start:start].strip() == "":
        start = line_start
    if end < len(info.source) and info.source[end] == "\n":
        end += 1
    return start, end


def _regenerate_import(node, survivors) -> str:
    """Rebuild a single-line import statement from the aliases that survive.

    Only ever called for a statement that was on one line, so no formatting
    decision is being made on the author's behalf beyond the one the removal
    already forces.
    """
    parts = ", ".join(a.name + (f" as {a.asname}" if a.asname else "")
                      for a in survivors)
    if isinstance(node, ast.ImportFrom):
        module = ("." * (node.level or 0)) + (node.module or "")
        return f"from {module} import {parts}"
    return f"import {parts}"
