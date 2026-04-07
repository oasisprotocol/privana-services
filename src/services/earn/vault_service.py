import logging
import time
import uuid
from typing import Optional

from src.core.db import db_write, get_db
from src.models.earn import PoolRecord, PoolStatus

logger = logging.getLogger(__name__)


class VaultService:
    def create_pool(
        self,
        token_id: str,
        strategy: str,
        pool_address: str,
    ) -> PoolRecord:
        pool_id = f"{token_id[:10]}-{strategy}"
        now = int(time.time())
        db = get_db()
        db_write(
            db,
            """INSERT INTO earn_pools
               (id, token_id, strategy, total_shares, total_assets,
                pool_address, apy_bps, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pool_id, token_id, strategy, "0", "0",
                pool_address.lower(), 0, PoolStatus.ACTIVE.value, now, now,
            ),
        )
        return self.get_pool(pool_id)

    def get_pool(self, pool_id: str) -> PoolRecord:
        db = get_db()
        row = db.execute("SELECT * FROM earn_pools WHERE id = ?", (pool_id,)).fetchone()
        if row is None:
            raise ValueError(f"Pool {pool_id} not found")
        return PoolRecord(**dict(row))

    def list_pools(self, status: Optional[str] = None) -> list[PoolRecord]:
        db = get_db()
        if status:
            rows = db.execute(
                "SELECT * FROM earn_pools WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM earn_pools ORDER BY created_at DESC"
            ).fetchall()
        return [PoolRecord(**dict(row)) for row in rows]


_service_instance: Optional[VaultService] = None


def get_vault_service() -> VaultService:
    global _service_instance
    if _service_instance is None:
        _service_instance = VaultService()
    return _service_instance
