"""Codemod: the four-stage refactoring pipeline.

    index.py            stage 1 -- scope-aware symbol and reference graph
    plan.py             stages 2 & 3 -- rewrites as reviewable data, gated on
                        the blast radius they computed
    ast_transaction.py  stage 4 -- apply, verify, and roll back as a saga

    from agent_saga.codemod import SymbolIndex, rename_symbol, plan_transform

    index = SymbolIndex.build("myproject")
    plan = rename_symbol(index, "myproject.util.helper", "compute")
    print(plan.to_diff())                      # review before anything runs
    if not plan.requires_review:
        tree = AstTransaction(root).shadow(plan.paths)
        tree.apply(plan_transform(plan))
        await transaction.commit(ctx, tree)    # transactional, restorable
"""

from .index import (
    BlastRadius,
    Definition,
    IndexError_,
    Reference,
    Span,
    SymbolIndex,
    Unresolved,
)
from .plan import (
    Plan,
    PlanError,
    Rewrite,
    plan_transform,
    remove_unused_imports,
    rename_symbol,
)
from .ast_transaction import (
    AstTransaction,
    CodemodError,
    CodemodResult,
    RestoreIntegrityError,
    ShadowModule,
    ShadowRejected,
    ShadowTree,
    Transform,
    VerificationFailed,
    Verifier,
    restore_files,
)

__all__ = [
    "AstTransaction",
    "CodemodError",
    "CodemodResult",
    "RestoreIntegrityError",
    "ShadowModule",
    "ShadowRejected",
    "ShadowTree",
    "Transform",
    "VerificationFailed",
    "Verifier",
    "restore_files",
]
