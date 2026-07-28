"""Stage 1 -- Index: a scope-aware symbol and reference graph.

`ast_transaction.py` is the *apply* stage: it can write a change safely. This
is the stage that works out **what to change**, and it is the one where being
clever is dangerous. A rename that catches a local variable of the same name,
or a string, or an unrelated object's attribute, is not a refactor -- it is
corruption that compiles.

So the index resolves names the way Python does, and refuses when it cannot:

  * **Scope-correct resolution.** A `Name` is matched to a module-level symbol
    only if no enclosing function scope binds it. Python's rule -- a name
    assigned anywhere in a function is local to that whole function -- is what
    makes this decidable without executing anything.
  * **`global` and `nonlocal` are honoured**, because a function that declares
    `global helper` genuinely is referring to the module-level symbol.
  * **Class bodies are not enclosing scopes for nested functions**, matching
    Python: a method's body does not see a bare class attribute.
  * **Cross-module references are resolved through the import graph**, so
    `from pkg.util import helper` and `import pkg.util; pkg.util.helper()` are
    both found -- and `import ... as alias` is recorded as an alias so the
    rename can rewrite the import without touching the aliased call sites.
  * **Everything it cannot prove, it declines.** Dynamic access (`getattr`,
    `globals()[...]`), star imports, and shadowed bindings become
    `Unresolved` entries reported to the caller rather than silent misses. A
    plan built on an index with unresolved entries can be refused by the gate
    instead of applied hopefully.

Stdlib `ast` only. `libcst` would preserve formatting through an unparse, but
this pipeline never unparses: it splices exact identifier spans, so comments,
blank lines and quirky formatting are preserved byte-for-byte by construction,
and the package keeps its zero required dependencies.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "BlastRadius",
    "Definition",
    "IndexError_",
    "ModuleInfo",
    "Reference",
    "Span",
    "SymbolIndex",
    "Unresolved",
]

_DEF_KINDS = {ast.FunctionDef: "function", ast.AsyncFunctionDef: "function",
              ast.ClassDef: "class"}


class IndexError_(RuntimeError):
    """The index could not be built. Named with a trailing underscore so it
    cannot be confused with the builtin `IndexError`."""


@dataclass(frozen=True)
class Span:
    """An exact character range in one file. Character offsets, not (line,
    col): a rewrite is a string splice, and every conversion between the two
    is a chance to be off by one."""

    module: str
    start: int
    end: int

    def describe(self) -> dict:
        return {"module": self.module, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class Definition:
    qualname: str                  # "pkg.util.helper"
    name: str                      # "helper"
    kind: str                      # function | class | variable
    module: str                    # "pkg.util"
    span: Span                     # the identifier itself, not the whole node

    @property
    def is_public(self) -> bool:
        """A leading underscore is the language's own statement that a symbol
        is not part of the public surface. The gate treats renaming a public
        symbol as a wider blast radius than a private one."""
        return not self.name.startswith("_")


@dataclass(frozen=True)
class Reference:
    """One use site. `kind` matters to the rewriter:

    name           a bare `helper` that resolves to the definition
    attribute      `util.helper` -- the attr identifier only
    import_name    the `helper` inside `from pkg.util import helper`
    import_alias   `from pkg.util import helper as h` -- rewrite the imported
                   name, never the call sites, which use `h`
    """

    target: str                    # qualname of the definition
    kind: str
    module: str
    span: Span


@dataclass(frozen=True)
class Unresolved:
    """Something the index saw but refuses to reason about. Carried into the
    plan so a caller can gate on it rather than discover it in a diff."""

    module: str
    reason: str
    detail: str
    related_module: Optional[str] = None
    """For a star import, the module it imports FROM. A blast radius must
    widen for `from target import *` even though that module has no *resolved*
    reference -- the absence of a reference is exactly what cannot be trusted
    there."""


@dataclass(frozen=True)
class BlastRadius:
    """How far a change to one symbol reaches. `unresolved` is part of the
    radius on purpose: a star import in a module is a reason to widen the
    review, not a detail to omit."""

    qualname: str
    modules: Tuple[str, ...]
    files: Tuple[str, ...]
    reference_count: int
    is_public: bool
    unresolved: Tuple[Unresolved, ...] = ()

    @property
    def is_wide(self) -> bool:
        """A useful default for gating: more than one module touched, or a
        public symbol, or anything the index could not resolve."""
        return len(self.modules) > 1 or self.is_public or bool(self.unresolved)

    def describe(self) -> dict:
        return {"qualname": self.qualname, "modules": list(self.modules),
                "files": list(self.files), "reference_count": self.reference_count,
                "is_public": self.is_public, "is_wide": self.is_wide,
                "unresolved": [{"module": u.module, "reason": u.reason,
                                "detail": u.detail} for u in self.unresolved]}


@dataclass
class ModuleInfo:
    name: str                      # dotted module name
    path: Path
    source: str
    encoding: str
    tree: ast.Module
    offsets: List[int] = field(default_factory=list)   # char offset of each line start


class SymbolIndex:
    """Definitions, references, and the import graph for a set of modules."""

    def __init__(self, root: Path):
        self.root = root
        self.modules: Dict[str, ModuleInfo] = {}
        self.definitions: Dict[str, Definition] = {}
        self.references: List[Reference] = []
        self.imports: Dict[str, Set[str]] = {}
        self.unresolved: List[Unresolved] = []

    # -- construction ------------------------------------------------------------

    @classmethod
    def build(cls, root, paths: Optional[Iterable] = None) -> "SymbolIndex":
        """Parse every module under `root` (or just `paths`) and index it.

        A file that does not parse is an `IndexError_`, not a skipped file:
        an index that quietly omits a module would compute a blast radius that
        is too small, which is the one direction that matters.
        """
        root_p = Path(root).resolve()
        index = cls(root_p)

        if paths is None:
            candidates = [p for p in sorted(root_p.rglob("*.py"))
                          if not _excluded(p, root_p)]
        else:
            candidates = [Path(p) if Path(p).is_absolute() else root_p / p
                          for p in paths]

        for path in candidates:
            path = path.resolve()
            raw = path.read_bytes()
            try:
                encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
                source = raw.decode(encoding)
                tree = ast.parse(source, filename=str(path))
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                raise IndexError_(
                    f"{path.relative_to(root_p).as_posix()}: {exc!r}. The index "
                    f"refuses to skip an unparseable module, because a blast "
                    f"radius computed without it would be too small.") from exc

            module = _module_name(path, root_p)
            info = ModuleInfo(name=module, path=path, source=source,
                              encoding=encoding, tree=tree,
                              offsets=_line_offsets(source))
            index.modules[module] = info

        for info in index.modules.values():
            index._collect_definitions(info)
        for info in index.modules.values():
            index._collect_references(info)
        return index

    # -- stage 1a: definitions ------------------------------------------------------

    def _collect_definitions(self, info: ModuleInfo) -> None:
        """Module-level definitions only. Nested functions and methods are not
        addressable by a dotted qualname without ambiguity, and renaming them
        is a local edit that does not need a cross-file index."""
        for node in info.tree.body:
            kind = _DEF_KINDS.get(type(node))
            if kind:
                name = node.name
                span = self._identifier_span(info, node, name)
                if span:
                    self._add_definition(info, name, kind, span)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._add_definition(
                            info, target.id, "variable", self._node_span(info, target))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                self._add_definition(
                    info, node.target.id, "variable", self._node_span(info, node.target))

    def _add_definition(self, info: ModuleInfo, name: str, kind: str,
                        span: Span) -> None:
        qualname = f"{info.name}.{name}"
        # A redefinition at module level (a conditional def, a re-assignment)
        # keeps the first: the rename rewrites every binding site anyway, and
        # the first is the one a reader thinks of as the definition.
        self.definitions.setdefault(
            qualname, Definition(qualname=qualname, name=name, kind=kind,
                                 module=info.name, span=span))

    # -- stage 1b: references --------------------------------------------------------

    def _collect_references(self, info: ModuleInfo) -> None:
        visitor = _ReferenceVisitor(self, info)
        visitor.visit(info.tree)
        self.imports[info.name] = visitor.imported_modules

    # -- queries ------------------------------------------------------------------------

    def references_to(self, qualname: str) -> List[Reference]:
        return [r for r in self.references if r.target == qualname]

    def blast_radius(self, qualname: str) -> BlastRadius:
        definition = self.definitions.get(qualname)
        if definition is None:
            raise IndexError_(
                f"no definition named {qualname!r} in the index. Known symbols "
                f"in that module: "
                f"{sorted(n.split('.')[-1] for n in self.definitions if n.rsplit('.', 1)[0] == qualname.rsplit('.', 1)[0])[:10]}")

        references = self.references_to(qualname)
        modules = {definition.module} | {r.module for r in references}
        # Unresolved entries in ANY touched module widen the radius: a star
        # import somewhere in the affected set may hide a use site.
        relevant = tuple(u for u in self.unresolved
                         if u.module in modules
                         or u.related_module == definition.module)
        return BlastRadius(
            qualname=qualname,
            modules=tuple(sorted(modules)),
            files=tuple(sorted(self.modules[m].path.relative_to(self.root).as_posix()
                               for m in modules if m in self.modules)),
            reference_count=len(references),
            is_public=definition.is_public,
            unresolved=relevant,
        )

    def source_of(self, module: str) -> str:
        return self.modules[module].source

    # -- span helpers ---------------------------------------------------------------------

    def _offset(self, info: ModuleInfo, lineno: int, col: int) -> int:
        """Convert ast's (1-based line, UTF-8 *byte* column) to a character
        offset. The byte/character distinction is not pedantry: a line with a
        non-ASCII string literal before the identifier shifts every column."""
        line_start = info.offsets[lineno - 1]
        line_end = (info.offsets[lineno] if lineno < len(info.offsets)
                    else len(info.source))
        line = info.source[line_start:line_end]
        prefix = line.encode("utf-8")[:col].decode("utf-8", errors="ignore")
        return line_start + len(prefix)

    def _node_span(self, info: ModuleInfo, node: ast.AST) -> Span:
        start = self._offset(info, node.lineno, node.col_offset)
        end = self._offset(info, node.end_lineno, node.end_col_offset)
        return Span(module=info.name, start=start, end=end)

    def _identifier_span(self, info: ModuleInfo, node: ast.AST,
                         name: str) -> Optional[Span]:
        """`def foo(...)` -- ast points at `def`, so find the identifier after
        the keyword rather than assuming a fixed offset (decorators, async,
        and unusual whitespace all move it)."""
        start = self._offset(info, node.lineno, node.col_offset)
        window = info.source[start:start + len(name) + 64]
        found = window.find(name)
        if found < 0:
            return None
        absolute = start + found
        return Span(module=info.name, start=absolute, end=absolute + len(name))

    def _attribute_span(self, info: ModuleInfo, node: ast.Attribute) -> Span:
        """`obj.attr` -- ast gives no position for `attr` itself, but the node
        ends exactly at its last character, so the identifier is the final
        len(attr) characters. Exact, and independent of whitespace around the
        dot."""
        end = self._offset(info, node.end_lineno, node.end_col_offset)
        return Span(module=info.name, start=end - len(node.attr), end=end)


class _ReferenceVisitor(ast.NodeVisitor):
    """Resolves names to module-level definitions, scope-correctly.

    The scope stack holds one entry per binding scope. Each entry records the
    names bound in that scope and whether they were declared `global` or
    `nonlocal`, which is enough to answer the only question this class asks:
    *does this `Name` refer to a module-level definition, or to something
    closer?*
    """

    def __init__(self, index: SymbolIndex, info: ModuleInfo):
        self.index = index
        self.info = info
        self.imported_modules: Set[str] = set()
        #: module -> {local name: qualname} for `from x import y [as z]`
        self._from_imports: Dict[str, str] = {}
        #: local alias -> module for `import x [as y]`
        self._module_aliases: Dict[str, str] = {}
        #: stack of (kind, bound names, global names, nonlocal names)
        self._scopes: List[Tuple[str, Set[str], Set[str], Set[str]]] = []

    # -- scope tracking -----------------------------------------------------------------

    def _push(self, kind: str, node: ast.AST) -> None:
        bound, globals_, nonlocals = _bindings_in_scope(node)
        self._scopes.append((kind, bound, globals_, nonlocals))

    def _pop(self) -> None:
        self._scopes.pop()

    def _is_module_level(self, name: str) -> bool:
        """True when a bare `name` here refers to module scope.

        Walks outward. Class scopes are skipped for the shadowing question
        because Python does not make a class body an enclosing scope for the
        functions inside it -- and a class-body reference is still checked
        against its own bindings by the first iteration.
        """
        for depth, (kind, bound, globals_, nonlocals) in enumerate(reversed(self._scopes)):
            if kind == "class" and depth > 0:
                continue                      # not an enclosing scope for nested defs
            if name in globals_:
                return True                   # `global name` -- explicitly module scope
            if name in nonlocals:
                return False                  # explicitly an enclosing function's
            if name in bound:
                return False                  # shadowed by a local binding
        return True

    # -- imports --------------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imported_modules.add(alias.name)
            if alias.asname:
                self._module_aliases[alias.asname] = alias.name
            else:
                # `import pkg.util` binds the name `pkg`, NOT `pkg.util`. The
                # dotted path is then rebuilt by walking the Attribute chain,
                # so `pkg.util.helper` resolves. Mapping the full dotted name
                # here instead would silently miss every such reference.
                root = alias.name.split(".")[0]
                self._module_aliases[root] = root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _absolute_module(node, self.info.name)
        if module:
            self.imported_modules.add(module)
        for alias in node.names:
            if alias.name == "*":
                self.index.unresolved.append(Unresolved(
                    module=self.info.name, reason="star-import",
                    detail=f"`from {module or '.'} import *` hides which names "
                           f"this module uses; a rename cannot be proven complete here",
                    related_module=module))
                continue
            if not module:
                continue
            qualname = f"{module}.{alias.name}"
            if qualname in self.index.definitions:
                span = self.index._identifier_span(self.info, alias, alias.name)
                if span:
                    self.index.references.append(Reference(
                        target=qualname,
                        kind="import_alias" if alias.asname else "import_name",
                        module=self.info.name, span=span))
                # An aliased import rebinds the symbol locally: `helper as h`
                # means call sites say `h`, and renaming them would be wrong.
                if not alias.asname:
                    self._from_imports[alias.name] = qualname
        self.generic_visit(node)

    # -- scopes ---------------------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            self.visit(default)             # defaults evaluate in the OUTER scope
        self._push("function", node)
        for statement in node.body:
            self.visit(statement)
        self._pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        self._push("class", node)
        for statement in node.body:
            self.visit(statement)
        self._pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            self.visit(default)
        self._push("function", node)
        self.visit(node.body)
        self._pop()

    # Comprehensions are their own scope in Python 3 -- `[x for x in ...]` does
    # not leak `x`, and a comprehension target of the same name shadows a
    # module symbol exactly like a local does.
    def visit_ListComp(self, node) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node) -> None:
        self._visit_comprehension(node)

    def _visit_comprehension(self, node) -> None:
        # The FIRST iterable is evaluated in the enclosing scope; everything
        # else belongs to the comprehension. Getting this backwards would miss
        # a real reference in `[helper() for x in helper_source()]`.
        first = node.generators[0]
        self.visit(first.iter)

        bound: Set[str] = set()
        for generator in node.generators:
            bound |= _names_in_target(generator.target)
        self._scopes.append(("function", bound, set(), set()))

        for generator in node.generators:
            if generator is not first:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for child in ("elt", "key", "value"):
            element = getattr(node, child, None)
            if element is not None:
                self.visit(element)
        self._pop()

    def visit_Global(self, node: ast.Global) -> None:
        self._declared_names(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        # `nonlocal x` never names a module-level symbol, so nothing is
        # recorded -- but the statement is still visited so a future change
        # here cannot silently skip it.
        return

    def _declared_names(self, node) -> None:
        """`global helper` stores its names as bare strings with no position
        info, so the span is found by scanning the statement's own source.
        Without this the declaration keeps the old name while every use of it
        is renamed -- code that imports and then fails at runtime."""
        statement = self.index._node_span(self.info, node)
        text = self.info.source[statement.start:statement.end]
        for name in node.names:
            qualname = f"{self.info.name}.{name}"
            if qualname not in self.index.definitions:
                continue
            for offset in _word_offsets(text, name):
                self.index.references.append(Reference(
                    target=qualname, kind="name", module=self.info.name,
                    span=Span(module=self.info.name,
                              start=statement.start + offset,
                              end=statement.start + offset + len(name))))

    # -- the resolution itself ---------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        name = node.id
        target: Optional[str] = None

        if name in self._from_imports and self._is_module_level(name):
            target = self._from_imports[name]
        else:
            local = f"{self.info.name}.{name}"
            if local in self.index.definitions and self._is_module_level(name):
                target = local

        if target:
            self.index.references.append(Reference(
                target=target, kind="name", module=self.info.name,
                span=self.index._node_span(self.info, node)))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """`pkg.util.helper` and `util.helper` -- only when the value resolves
        to a module this file actually imported. An attribute on an arbitrary
        object that happens to share the name is never touched."""
        module = self._resolve_module_expression(node.value)
        if module:
            qualname = f"{module}.{node.attr}"
            if qualname in self.index.definitions:
                self.index.references.append(Reference(
                    target=qualname, kind="attribute", module=self.info.name,
                    span=self.index._attribute_span(self.info, node)))
        self.generic_visit(node)

    def _resolve_module_expression(self, node: ast.AST) -> Optional[str]:
        """Turn `pkg.util` (nested Attributes over a Name) into a module name,
        but only if the root Name is a module this file imported."""
        parts: List[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return None
        root = current.id
        if root not in self._module_aliases:
            return None
        if not self._is_module_level(root):
            return None                      # a local variable shadows the import
        parts.append(self._module_aliases[root])
        return ".".join(reversed(parts))

    def visit_Call(self, node: ast.Call) -> None:
        """Dynamic access defeats static resolution. Record it rather than let
        a plan claim completeness it cannot have."""
        function = node.func
        dynamic = (isinstance(function, ast.Name)
                   and function.id in ("getattr", "setattr", "globals", "vars", "eval", "exec"))
        if dynamic:
            self.index.unresolved.append(Unresolved(
                module=self.info.name, reason=f"dynamic-{function.id}",
                detail=f"`{function.id}(...)` at line {node.lineno} can reach a "
                       f"symbol by a name no static index can see"))
        self.generic_visit(node)


# -- helpers ---------------------------------------------------------------------------------

def _bindings_in_scope(node: ast.AST) -> Tuple[Set[str], Set[str], Set[str]]:
    """Names bound directly in this scope, plus its `global`/`nonlocal`
    declarations. Nested function and class bodies are not descended into --
    their bindings belong to their own scopes."""
    bound: Set[str] = set()
    globals_: Set[str] = set()
    nonlocals: Set[str] = set()

    args = getattr(node, "args", None)
    if isinstance(args, ast.arguments):
        for arg in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                    + [a for a in (args.vararg, args.kwarg) if a]):
            bound.add(arg.arg)

    body = node.body if isinstance(node.body, list) else [node.body]
    for statement in body:
        _walk_bindings(statement, bound, globals_, nonlocals)
    return bound, globals_, nonlocals


def _walk_bindings(node: ast.AST, bound: Set[str], globals_: Set[str],
                   nonlocals: Set[str]) -> None:
    if isinstance(node, ast.Global):
        globals_.update(node.names)
        return
    if isinstance(node, ast.Nonlocal):
        nonlocals.update(node.names)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        bound.add(node.name)
        return                                # its body is a different scope
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            if alias.name != "*":
                bound.add(alias.asname or alias.name.split(".")[0])
        return
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For,
                         ast.AsyncFor, ast.With, ast.AsyncWith, ast.NamedExpr)):
        for target in _assignment_targets(node):
            bound.update(_names_in_target(target))
    if isinstance(node, ast.ExceptHandler) and node.name:
        bound.add(node.name)

    for child in ast.iter_child_nodes(node):
        _walk_bindings(child, bound, globals_, nonlocals)


def _assignment_targets(node: ast.AST) -> List[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
        return [node.target]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.target]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.optional_vars for item in node.items if item.optional_vars]
    return []


def _names_in_target(target: ast.AST) -> Set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: Set[str] = set()
        for element in target.elts:
            names |= _names_in_target(element)
        return names
    if isinstance(target, ast.Starred):
        return _names_in_target(target.value)
    return set()                              # Attribute/Subscript bind nothing local


def _absolute_module(node: ast.ImportFrom, current: str) -> Optional[str]:
    """Resolve `from . import x` / `from ..pkg import y` against the importing
    module's own package."""
    if not node.level:
        return node.module
    parts = current.split(".")
    base = parts[:-node.level] if node.level <= len(parts) else []
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base) if base else None


def _word_offsets(text: str, word: str) -> List[int]:
    """Offsets of `word` in `text` where it is a whole identifier, so `helper`
    never matches inside `helper_two`."""
    found: List[int] = []
    start = 0
    while True:
        at = text.find(word, start)
        if at < 0:
            return found
        before_ok = at == 0 or not (text[at - 1].isalnum() or text[at - 1] == "_")
        after = at + len(word)
        after_ok = after >= len(text) or not (text[after].isalnum() or text[after] == "_")
        if before_ok and after_ok:
            found.append(at)
        start = at + 1


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts) if parts else relative.stem


def _line_offsets(source: str) -> List[int]:
    offsets = [0]
    for index, char in enumerate(source):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


_EXCLUDED_DIRS = {".git", "__pycache__", ".agent_saga_snapshots", ".venv", "venv",
                  "node_modules", ".pytest_cache", "build", "dist"}


def _excluded(path: Path, root: Path) -> bool:
    return bool(_EXCLUDED_DIRS & set(path.relative_to(root).parts))
