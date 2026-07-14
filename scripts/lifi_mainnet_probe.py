import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

BASE_MAINNET_CHAIN_ID = 8453
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"
STATUS_POLL_INTERVAL_SEC = 10
MAX_STATUS_POLLS = 60

USAGE = """lifi_mainnet_probe: real-money validation of the LiFi execution path.

Swaps PROBE_AMOUNT_USDC (default 1 USDC) for WETH on Base mainnet through
the LiFi router, using the wallet in PROBE_SECRET_KEY. Requires:

  PROBE_SECRET_KEY      funded Base mainnet wallet (USDC + ETH for gas)
  LIFI_API_KEY          LiFi API key (from .env)
  BASE_MAINNET_RPC_URL  Base mainnet RPC (from .env)

Optional:
  PROBE_AMOUNT_USDC     amount in base units (default 1000000 = $1)

Run: .venv/bin/python scripts/lifi_mainnet_probe.py
"""


async def main() -> int:
    probe_key = os.getenv("PROBE_SECRET_KEY")
    if not probe_key:
        print(USAGE)
        return 1

    amount = int(os.getenv("PROBE_AMOUNT_USDC", "1000000"))
    rpc_url = os.environ["BASE_MAINNET_RPC_URL"]

    from src.clients.base_evm import BaseEvmClient
    from src.clients.lifi import get_lifi_client

    evm = BaseEvmClient(rpc_url, probe_key)
    lifi = get_lifi_client()

    eth_balance = evm.w3.eth.get_balance(evm.address)
    usdc_balance = evm.erc20_balance(BASE_USDC, evm.address)
    weth_before = evm.erc20_balance(BASE_WETH, evm.address)
    print(f"wallet: {evm.address}")
    print(f"ETH: {eth_balance / 1e18:.6f}")
    print(f"USDC: {usdc_balance / 1e6:.2f}")
    print(f"WETH: {weth_before / 1e18:.8f}")

    if usdc_balance < amount:
        print(f"ABORT: wallet holds {usdc_balance} USDC units, probe needs {amount}")
        return 1
    if eth_balance == 0:
        print("ABORT: wallet has no ETH for gas")
        return 1

    quote = await lifi.get_execution_quote(
        from_chain_id=BASE_MAINNET_CHAIN_ID,
        to_chain_id=BASE_MAINNET_CHAIN_ID,
        from_token_address=BASE_USDC,
        to_token_address=BASE_WETH,
        from_amount=str(amount),
        from_address=evm.address,
    )
    estimate = quote["estimate"]
    print(f"\nroute tool: {quote.get('tool')}")
    print(f"toAmount: {int(estimate['toAmount']) / 1e18:.8f} WETH")
    print(f"toAmountMin: {int(estimate['toAmountMin']) / 1e18:.8f} WETH")
    gas_costs = estimate.get("gasCosts", [{}])
    print(f"est gas USD: {gas_costs[0].get('amountUSD', '?')}")

    confirm = input(f"\nSwap {amount / 1e6:.2f} USDC -> WETH on Base mainnet? Type YES: ")
    if confirm.strip() != "YES":
        print("aborted")
        return 1

    approval_tx = evm.ensure_allowance(BASE_USDC, estimate["approvalAddress"], amount)
    print(f"approval: {approval_tx or 'already sufficient'}")

    tx_hash = evm.send_transaction_request(quote["transactionRequest"])
    print(f"swap tx: {tx_hash}")

    for _ in range(MAX_STATUS_POLLS):
        status = await lifi.get_status(tx_hash, BASE_MAINNET_CHAIN_ID, BASE_MAINNET_CHAIN_ID)
        state = status.get("status")
        print(f"status: {state}")
        if state == "DONE":
            break
        if state == "FAILED":
            print("PROBE FAILED: lifi reports execution failure")
            return 1
        await asyncio.sleep(STATUS_POLL_INTERVAL_SEC)

    weth_after = evm.erc20_balance(BASE_WETH, evm.address)
    received = weth_after - weth_before
    print(f"\nreceived: {received / 1e18:.8f} WETH")
    print("PROBE PASSED" if received > 0 else "PROBE INCONCLUSIVE: no WETH delta")
    await lifi.close()
    return 0 if received > 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
