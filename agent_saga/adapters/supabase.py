from typing import Any, Dict, List, Optional
from .base_db import BaseDBAdapter

class SupabaseAdapter(BaseDBAdapter):
    """Supabase client adapter that automatically registers compensating operations
    for inserts, updates, and deletes performed on a Supabase database."""

    def __init__(self, client: Any, pk_mappings: Optional[Dict[str, List[str]]] = None):
        """
        Initialize with a Supabase client.
        `pk_mappings` is an optional dictionary mapping table names to list of primary key columns.
        Defaults to ['id'] if not specified.
        """
        self.client = client
        self.pk_mappings = pk_mappings or {}

    def _get_pk_where(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        pks = self.pk_mappings.get(table, ["id"])
        # Fall back to using the entire row as identity if none of the primary keys match
        where = {pk: row[pk] for pk in pks if pk in row}
        if not where:
            return row
        return where

    async def insert(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a record into a Supabase table and register a compensating DELETE."""
        # execute() is synchronous or async depending on the client. Let's make it work with both.
        res = self.client.table(table).insert(data).execute()
        inserted_row = res.data[0]
        
        # Build primary key selector
        pk_where = self._get_pk_where(table, inserted_row)
        
        # Register rollback compensation
        self._register_compensation(self._delete_rollback, table, pk_where)
        return inserted_row

    async def update(self, table: str, data: Dict[str, Any], where: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Update records in a Supabase table and register compensating UPDATEs for modified rows."""
        # Fetch old rows first to capture their previous state
        query_select = self.client.table(table).select("*")
        for k, v in where.items():
            query_select = query_select.eq(k, v)
        res_select = query_select.execute()
        old_rows = res_select.data
        
        # Perform update
        query_update = self.client.table(table).update(data)
        for k, v in where.items():
            query_update = query_update.eq(k, v)
        res_update = query_update.execute()
        updated_rows = res_update.data
        
        # Register a compensation for each row updated
        for old_row in old_rows:
            pk_where = self._get_pk_where(table, old_row)
            old_values = {k: old_row[k] for k in data.keys() if k in old_row}
            self._register_compensation(self._update_rollback, table, old_values, pk_where)
            
        return updated_rows

    async def delete(self, table: str, where: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Delete records from a Supabase table and register compensating INSERTs."""
        # Fetch records first to capture their full state for restoration
        query_select = self.client.table(table).select("*")
        for k, v in where.items():
            query_select = query_select.eq(k, v)
        res_select = query_select.execute()
        deleted_rows = res_select.data
        
        # Perform delete
        query_delete = self.client.table(table).delete()
        for k, v in where.items():
            query_delete = query_delete.eq(k, v)
        query_delete.execute()
        
        # Register compensation inserts
        for row in deleted_rows:
            self._register_compensation(self._insert_rollback, table, row)
            
        return deleted_rows

    async def _delete_rollback(self, table: str, where: Dict[str, Any]) -> None:
        query = self.client.table(table).delete()
        for k, v in where.items():
            query = query.eq(k, v)
        query.execute()

    async def _update_rollback(self, table: str, old_data: Dict[str, Any], where: Dict[str, Any]) -> None:
        query = self.client.table(table).update(old_data)
        for k, v in where.items():
            query = query.eq(k, v)
        query.execute()

    async def _insert_rollback(self, table: str, data: Dict[str, Any]) -> None:
        self.client.table(table).insert(data).execute()
