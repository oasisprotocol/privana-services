import time
from unittest.mock import MagicMock, patch

import src.main as main_module
from src.clients.evm_batcher import STUCK_AFTER_SEC
from src.core.db import db_write


async def test_a_write_queued_past_the_stuck_window_degrades_health(test_db):
    old = int(time.time()) - STUCK_AFTER_SEC - 1
    db_write(test_db, "INSERT INTO evm_transactions (chain_id, address, owner, to_address, data, value, gas, "
             "simulate, created_at, updated_at) VALUES (1, '0xa', 'swap:x:lifi', '0xb', '0x', '0', 1, 1, ?, ?)",
             (old, old))
    sapphire = MagicMock()
    sapphire.is_connected.return_value = True

    with patch("src.clients.sapphire.get_sapphire_client", return_value=sapphire):
        report = await main_module.health()

    assert (report["status"], report["checks"]["queues"]) == ("degraded", "stalled")
