from typing import Any, Dict, List, Optional, Callable
from .base_db import BaseDBAdapter

_IMPORT_HINT = (
    "SQLAlchemyAdapter needs the 'sqlalchemy' package, which is an optional "
    "dependency.\n"
    "    pip install agent-saga[postgres]\n"
    "The core engine stays dependency-free; only this adapter needs it."
)


def _require_sqlalchemy() -> None:
    """Fail with an actionable hint -- pointing at the extra -- instead of a bare
    ModuleNotFoundError the first time the adapter touches SQLAlchemy."""
    try:
        import sqlalchemy  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise ImportError(_IMPORT_HINT) from exc


class SQLAlchemyAdapter(BaseDBAdapter):
    """SQLAlchemy ORM adapter that automatically registers compensating SQL statements
    for inserts, updates, and deletes, utilizing SQLAlchemy AsyncSession."""

    def __init__(self, session_or_factory: Any):
        """Initialize with an AsyncSession or a callable factory returning an AsyncSession."""
        _require_sqlalchemy()
        if callable(session_or_factory) and not hasattr(session_or_factory, "execute"):
            self.session_factory = session_or_factory
            self._session = None
        else:
            self.session_factory = None
            self._session = session_or_factory

    async def _get_session(self) -> Any:
        if self.session_factory is not None:
            return self.session_factory()
        return self._session

    async def insert(self, table: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insert an ORM instance and register a compensating DELETE.
        `table` should be a SQLAlchemy Declarative model class."""
        from sqlalchemy import inspect
        
        session = await self._get_session()
        should_close = self.session_factory is not None
        try:
            instance = table(**data)
            session.add(instance)
            await session.flush()
            
            mapper = inspect(table)
            pk_columns = [c.key for c in mapper.primary_key]
            pk_values = {pk: getattr(instance, pk) for pk in pk_columns}
            
            # Convert instance to dictionary for return
            result = {c.key: getattr(instance, c.key) for c in mapper.columns}
            
            # Register compensation using primary keys
            self._register_compensation(self._delete_rollback, table, pk_values)
            
            if not should_close:
                # Keep active session uncommitted, let saga controller decide or commit at scope end
                pass
            else:
                await session.commit()
            
            return result
        finally:
            if should_close:
                await session.close()

    async def update(self, table: Any, data: Dict[str, Any], where: Dict[str, Any]) -> Dict[str, Any]:
        """Update ORM instances matching `where` and register a compensating UPDATE to restore old values.
        `table` should be a SQLAlchemy Declarative model class."""
        from sqlalchemy import select, update, inspect
        
        session = await self._get_session()
        should_close = self.session_factory is not None
        try:
            mapper = inspect(table)
            pk_columns = [c.key for c in mapper.primary_key]
            
            # Fetch old records to record old values for the updated fields
            stmt = select(table)
            for k, v in where.items():
                stmt = stmt.where(getattr(table, k) == v)
            result = await session.execute(stmt)
            instances = result.scalars().all()
            
            compensations = []
            updated_results = []
            for inst in instances:
                inst_pk = {pk: getattr(inst, pk) for pk in pk_columns}
                old_data = {k: getattr(inst, k) for k in data.keys()}
                compensations.append((inst_pk, old_data))
            
            # Execute the update
            stmt_update = update(table).values(**data)
            for k, v in where.items():
                stmt_update = stmt_update.where(getattr(table, k) == v)
            await session.execute(stmt_update)
            await session.flush()
            
            # Register compensation for each updated row
            for inst_pk, old_data in compensations:
                self._register_compensation(self._update_rollback, table, old_data, inst_pk)
                
            if should_close:
                await session.commit()
                
            # Fetch updated values to return
            stmt_refetch = select(table)
            for k, v in where.items():
                stmt_refetch = stmt_refetch.where(getattr(table, k) == v)
            refetch_result = await session.execute(stmt_refetch)
            refetched_instances = refetch_result.scalars().all()
            
            return [{c.key: getattr(inst, c.key) for c in mapper.columns} for inst in refetched_instances]
        finally:
            if should_close:
                await session.close()

    async def delete(self, table: Any, where: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Delete ORM instances matching `where` and register compensating INSERTs.
        `table` should be a SQLAlchemy Declarative model class."""
        from sqlalchemy import select, delete, inspect
        
        session = await self._get_session()
        should_close = self.session_factory is not None
        try:
            mapper = inspect(table)
            
            # Select first to capture entire row state for restoration
            stmt = select(table)
            for k, v in where.items():
                stmt = stmt.where(getattr(table, k) == v)
            result = await session.execute(stmt)
            instances = result.scalars().all()
            
            deleted_records = []
            for inst in instances:
                row_dict = {c.key: getattr(inst, c.key) for c in mapper.columns}
                deleted_records.append(row_dict)
                
            # Perform the actual delete
            stmt_del = delete(table)
            for k, v in where.items():
                stmt_del = stmt_del.where(getattr(table, k) == v)
            await session.execute(stmt_del)
            await session.flush()
            
            # Register an insert compensation for each deleted row
            for record in deleted_records:
                self._register_compensation(self._insert_rollback, table, record)
                
            if should_close:
                await session.commit()
                
            return deleted_records
        finally:
            if should_close:
                await session.close()

    async def _delete_rollback(self, table: Any, where: Dict[str, Any]) -> None:
        from sqlalchemy import delete
        session = await self._get_session()
        should_close = self.session_factory is not None
        try:
            stmt = delete(table)
            for k, v in where.items():
                stmt = stmt.where(getattr(table, k) == v)
            await session.execute(stmt)
            await session.commit()
        finally:
            if should_close:
                await session.close()

    async def _update_rollback(self, table: Any, old_data: Dict[str, Any], inst_pk: Dict[str, Any]) -> None:
        from sqlalchemy import update
        session = await self._get_session()
        should_close = self.session_factory is not None
        try:
            stmt = update(table).values(**old_data)
            for k, v in inst_pk.items():
                stmt = stmt.where(getattr(table, k) == v)
            await session.execute(stmt)
            await session.commit()
        finally:
            if should_close:
                await session.close()

    async def _insert_rollback(self, table: Any, record: Dict[str, Any]) -> None:
        session = await self._get_session()
        should_close = self.session_factory is not None
        try:
            instance = table(**record)
            session.add(instance)
            await session.commit()
        finally:
            if should_close:
                await session.close()
