import json
from dataclasses import replace

import pytest
from eth_account import Account
from privana.client.errors import AccountingApiError, NetworkError
from privana.types import TransactionSubmissionResponse
from web3.exceptions import Web3RPCError

import src.clients.evm_batcher as evm_batcher
import src.clients.privana_lanes as lanes
from src.clients.privana_lanes import (
    WithdrawLane,
    submit_privana,
    submit_withdrawal,
    swap_intent,
    transfer_balance_intent,
)
from src.core.eip712 import sign_transfer
from tests.clients.fake_chain import (
    SAPPHIRE_CHAIN_ID,
    FakeAccounting,
    FakeApi,
    FakeChain,
    sapphire_sender,
)

TOKEN = "0x" + "aa" * 32
SWAP_MANAGER = "0x" + "5a" * 20
USERS = [Account.from_key("0x" + f"{i + 1:02x}" * 32) for i in range(4)]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(evm_batcher, "RECEIPT_POLL_SEC", 0)
    monkeypatch.setattr(evm_batcher, "RECEIPT_TIMEOUT_SEC", 0.05)


@pytest.fixture
def config(settings, monkeypatch):
    config = replace(settings, swap_manager_contract_address=SWAP_MANAGER)
    monkeypatch.setattr(lanes, "load_settings", lambda: config)
    return config


@pytest.fixture
def chain(config, monkeypatch):
    chain = FakeChain(SAPPHIRE_CHAIN_ID)
    chain.contracts = FakeAccounting(config, SAPPHIRE_CHAIN_ID)
    chain.auto_mine = True
    monkeypatch.setattr(lanes, "get_accounting_client", lambda: chain.contracts)
    return chain


@pytest.fixture
def lp(chain, test_db, config):
    """The LP's Sapphire sender, the one sender of the LP's transfer nonce."""
    return sapphire_sender(chain, test_db, config.liquidity_provider_secret_key)


@pytest.fixture
def batching(lp, monkeypatch):
    """The lane logic with a Sapphire batch of ten, as it runs once the node
    takes several txs of one account at a time."""
    monkeypatch.setattr(lanes, "SAPPHIRE_BATCH_SIZE", 10)
    lp.batch_size = 10


def transfers(config, *recipients, amount=1):
    return submit_privana(config.liquidity_provider_address, *(
        transfer_balance_intent(f"swap:{i}:credit", user.address, TOKEN, amount) for i, user in enumerate(recipients)))


def row(row_id):
    return dict(lanes.get_db().execute("SELECT * FROM privana_transactions WHERE id = ?", (row_id,)).fetchone())


def rounds(sender, count):
    for _ in range(count):
        sender.run_round()


def evm_rows(test_db):
    return [dict(r) for r in test_db.execute("SELECT * FROM evm_transactions ORDER BY id")]


def landed(chain):
    return [(to, nonce) for _, to, nonce in chain.contracts.landed]


def test_by_default_a_sapphire_sender_signs_and_sends_one_row_per_round(lp, chain, config):
    a, b, c = transfers(config, *USERS[:3])

    lp.run_round()

    assert [row(i)["status"] for i in (a, b, c)] == ["sent", "queued", "queued"]
    assert len(chain.sent) == 1
    rounds(lp, 4)
    assert landed(chain) == [(u.address.lower(), n) for n, u in enumerate(USERS[:3])]


def test_queued_transfers_are_signed_at_consecutive_nonces_and_land_once(lp, chain, config, test_db, batching):
    ids = transfers(config, *USERS[:3])

    rounds(lp, 3)

    assert [(row(i)["status"], row(i)["nonce"]) for i in ids] == [("landed", 0), ("landed", 1), ("landed", 2)]
    assert landed(chain) == [(u.address.lower(), n) for n, u in enumerate(USERS[:3])]
    # Only the first can be simulated: the rest need the ones before them landed.
    assert [r["simulate"] for r in evm_rows(test_db)] == [1, 0, 0]


def test_a_revert_queues_it_and_the_rows_after_it_again_and_none_lands_twice(lp, chain, config, batching):
    a, b, c, d = transfers(config, *USERS)
    chain.contracts.reverting_recipients.add(USERS[1].address.lower())
    lp.run_round()
    assert landed(chain) == [(USERS[0].address.lower(), 0)]
    chain.contracts.reverting_recipients.clear()

    rounds(lp, 3)

    assert landed(chain) == [(u.address.lower(), n) for n, u in enumerate(USERS)]
    assert [row(i)["status"] for i in (a, b, c, d)] == ["landed"] * 4


def test_a_row_whose_simulation_fails_at_its_nonce_fails_at_once_and_frees_the_lane(lp, chain, config):
    a, b, c = transfers(config, *USERS[:3])
    chain.contracts.reverting_recipients.add(USERS[1].address.lower())

    rounds(lp, 4)

    assert row(b)["status"] == "failed" and row(b)["error"].startswith("simulation failed")
    assert (row(c)["status"], row(c)["nonce"]) == ("landed", 1)
    assert landed(chain) == [(USERS[0].address.lower(), 0), (USERS[2].address.lower(), 1)]


def test_rows_behind_a_failed_simulation_are_never_sent_that_round(lp, chain, config, test_db, batching):
    a, b, c = transfers(config, *USERS[:3])
    chain.contracts.reverting_recipients.add(USERS[0].address.lower())

    lp.run_round()

    assert chain.sent == []
    assert [r["error"] for r in evm_rows(test_db)][1:] == [evm_batcher.SUPERSEDED] * 2
    rounds(lp, 3)
    assert row(a)["status"] == "failed"
    assert landed(chain) == [(USERS[1].address.lower(), 0), (USERS[2].address.lower(), 1)]


def test_a_row_reverting_once_mined_after_its_own_simulation_fails_and_frees_the_lane(lp, chain, config):
    a, b, c = transfers(config, *USERS[:3])
    chain.contracts.reverting_when_mined.add(USERS[1].address.lower())

    rounds(lp, 5)

    assert row(b)["status"] == "failed" and "reverted" in row(b)["error"]
    assert (row(c)["status"], row(c)["nonce"]) == ("landed", 1)
    assert landed(chain) == [(USERS[0].address.lower(), 0), (USERS[2].address.lower(), 1)]


def test_a_foreign_spend_fails_only_its_row_and_the_lane_keeps_signing(lp, chain, config, test_db):
    chain.auto_mine = False
    (spent,) = transfers(config, USERS[0])
    lp.run_round()
    chain.contracts.transfer_nonces[config.liquidity_provider_address.lower()] = 1
    chain.mine()
    (behind,) = submit_privana(config.liquidity_provider_address,
                               transfer_balance_intent("swap:b:credit", USERS[1].address, TOKEN, 1))
    chain.auto_mine = True

    rounds(lp, 3)

    assert (row(spent)["status"], row(spent)["error"]) == ("failed", lanes.FOREIGN_SPEND)
    assert len(evm_rows(test_db)) == 2
    assert (row(behind)["status"], row(behind)["nonce"]) == ("landed", 1)


@pytest.mark.parametrize("message", [
    "insufficient funds for gas * price + value",
    "insufficient balance to pay fees",
])
def test_a_gas_refusal_pauses_signing_without_a_strike(lp, chain, config, test_db, message):
    (tx,) = transfers(config, USERS[0])
    chain.refuse = [Web3RPCError(message)]
    rounds(lp, 3)
    assert row(tx)["status"] == "queued"
    assert len(evm_rows(test_db)) == 1
    test_db.execute("UPDATE evm_transactions SET updated_at = updated_at - ?", (lanes.GAS_REFUSAL_PAUSE_SEC + 1,))

    rounds(lp, 2)

    assert (row(tx)["status"], row(tx)["nonce"], row(tx)["attempts"]) == ("landed", 0, 2)


def test_a_lagging_counter_read_neither_lands_a_transfer_early_nor_signs_below_one(lp, chain, config, monkeypatch):
    (a,) = transfers(config, USERS[0])
    lp.run_round()
    real = chain.contracts.transfer_nonce
    reads = [0]
    monkeypatch.setattr(chain.contracts, "transfer_nonce", lambda address: reads.pop(0) if reads else real(address))
    lp.run_round()
    assert row(a)["status"] == "sent"
    lp.run_round()
    (b,) = submit_privana(config.liquidity_provider_address,
                          transfer_balance_intent("swap:b:credit", USERS[1].address, TOKEN, 1))
    reads.append(0)

    rounds(lp, 2)

    assert [(row(i)["status"], row(i)["nonce"]) for i in (a, b)] == [("landed", 0), ("landed", 1)]


def test_a_row_is_never_signed_without_its_evm_tx(lp, config, test_db, monkeypatch, batching):
    first, second = transfers(config, *USERS[:2])
    real = lanes._call
    calls = []

    def fail_on_second(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("crash while signing")
        return real(*args)

    monkeypatch.setattr(lanes, "_call", fail_on_second)

    lp.run_round()

    for i in (first, second):
        assert (row(i)["status"], row(i)["nonce"], row(i)["signature"], row(i)["attempts"]) == ("queued", None, None, 0)
    assert evm_rows(test_db) == []


def test_submitting_an_owner_again_keeps_its_one_row(config):
    (first,) = transfers(config, USERS[0])
    (again,) = transfers(config, USERS[1])

    assert again == first
    assert json.loads(row(first)["payload"])["to"] == USERS[0].address


def test_a_swap_moves_the_users_input_and_the_lps_output_together(lp, chain, config):
    user = USERS[0]
    lp_address = config.liquidity_provider_address
    input_signature = sign_transfer(user.key, SAPPHIRE_CHAIN_ID, config.accounting_contract_address,
                                    lp_address, TOKEN, 7, 0)
    (swap,) = submit_privana(lp_address, swap_intent(
        "swap:s:internal", user.address, TOKEN, 7, 0, input_signature, TOKEN, 5))

    rounds(lp, 2)

    assert row(swap)["status"] == "landed"
    assert chain.contracts.landed == [(user.address.lower(), lp_address.lower(), 0),
                                      (lp_address.lower(), user.address.lower(), 0)]


@pytest.fixture
def api(chain, config):
    return FakeApi(chain.contracts, config.liquidity_provider_address.lower())


@pytest.fixture
def withdraw_lane(api, config):
    async def client():
        return api

    return WithdrawLane(config.liquidity_provider_address.lower(), config.liquidity_provider_secret_key, client)


def withdrawal(config, owner="swap:w:withdraw", amount=5):
    return submit_withdrawal(config.liquidity_provider_address, owner, TOKEN, amount)


def age_withdrawals(test_db, seconds):
    test_db.execute("UPDATE privana_transactions SET updated_at = updated_at - ?", (seconds,))


async def test_a_withdrawal_is_saved_signed_before_it_is_sent_and_lands_by_the_nonce(withdraw_lane, api, config):
    tx = withdrawal(config)
    seen = []
    api.on_request = lambda request: seen.append((row(tx)["status"], row(tx)["nonce"], row(tx)["signature"]))

    await withdraw_lane.run_round()
    await withdraw_lane.run_round()

    assert seen == [("sent", 0, api.requests[0].signature)]
    assert row(tx)["status"] == "landed"


@pytest.mark.parametrize("reply", [
    NetworkError("Request timeout after 30s"),
    AccountingApiError("API request failed: 502 Bad Gateway", 502),
    AccountingApiError("API request failed: 409 Conflict", 409),
])
async def test_a_withdrawal_whose_outcome_is_unknown_is_resent_unchanged_until_it_lands(
        withdraw_lane, api, config, test_db, reply):
    tx = withdrawal(config)
    api.replies = [(False, reply)]
    await withdraw_lane.run_round()
    await withdraw_lane.run_round()
    assert (row(tx)["status"], len(api.requests)) == ("sent", 1)
    age_withdrawals(test_db, lanes.WITHDRAW_RESEND_SEC)

    await withdraw_lane.run_round()
    await withdraw_lane.run_round()

    first, resent = api.requests
    assert (resent.nonce, resent.signature) == (first.nonce, first.signature)
    assert row(tx)["status"] == "landed"


async def test_a_refused_resend_of_a_withdrawal_that_landed_meanwhile_is_landed(withdraw_lane, api, config, test_db):
    tx = withdrawal(config)
    api.replies = [(False, NetworkError("Request timeout after 30s")),
                   (False, AccountingApiError("API request failed: 400 Bad Request", 400, "invalid nonce"))]
    await withdraw_lane.run_round()
    age_withdrawals(test_db, lanes.WITHDRAW_RESEND_SEC)
    # The first request lands only after its resend went out.
    api.on_request = lambda _: api.contracts.withdrawal_nonces.update({api.signer: 1})

    await withdraw_lane.run_round()

    assert row(tx)["status"] == "landed"


async def test_a_refused_resend_waits_for_an_earlier_send_that_may_still_land(withdraw_lane, api, config, test_db):
    tx = withdrawal(config)
    refused = AccountingApiError("API request failed: 400 Bad Request", 400, "insufficient balance")
    api.replies = [(False, NetworkError("Request timeout after 30s"))] + [(False, refused)] * lanes.WITHDRAW_MAX_SENDS
    await withdraw_lane.run_round()
    age_withdrawals(test_db, lanes.WITHDRAW_RESEND_SEC)
    await withdraw_lane.run_round()
    assert row(tx)["status"] == "sent"

    api.contracts.withdrawal_nonces[api.signer] = 1
    await withdraw_lane.run_round()

    assert row(tx)["status"] == "landed"


async def test_a_resent_withdrawal_is_never_failed_and_its_nonce_never_reused(withdraw_lane, api, config, test_db):
    tx, after = withdrawal(config), withdrawal(config, owner="swap:w2:withdraw")
    refused = AccountingApiError("API request failed: 400 Bad Request", 400, "insufficient balance")
    api.replies = [(False, NetworkError("Request timeout after 30s"))] + [(False, refused)] * lanes.WITHDRAW_MAX_SENDS
    await withdraw_lane.run_round()
    for _ in range(lanes.WITHDRAW_MAX_SENDS + 2):
        age_withdrawals(test_db, lanes.WITHDRAW_RESEND_SEC)
        await withdraw_lane.run_round()

    # It waits for a person: no more sends, and the next is not signed at its nonce.
    assert len(api.requests) == lanes.WITHDRAW_MAX_SENDS
    assert (row(tx)["status"], row(after)["status"]) == ("sent", "queued")

    # The first send lands late after all: it is the one landed.
    api.contracts.withdrawal_nonces[api.signer] += 1
    await withdraw_lane.run_round()

    assert row(tx)["status"] == "landed"


async def test_a_refused_withdrawal_fails_and_the_next_takes_its_unspent_nonce(withdraw_lane, api, config):
    refused, behind = withdrawal(config), withdrawal(config, owner="swap:v:withdraw")
    api.replies = [(False, AccountingApiError("API request failed: 400 Bad Request", 400, "insufficient balance"))]

    await withdraw_lane.run_round()
    await withdraw_lane.run_round()
    await withdraw_lane.run_round()

    assert row(refused)["status"] == "failed" and "400" in row(refused)["error"]
    assert (row(behind)["status"], row(behind)["nonce"]) == ("landed", 0)


async def test_one_withdrawal_is_in_flight_at_a_time(withdraw_lane, api, config, test_db):
    first, second = withdrawal(config), withdrawal(config, owner="swap:v:withdraw")
    api.replies = [(False, NetworkError("Request timeout after 30s"))]
    await withdraw_lane.run_round()
    await withdraw_lane.run_round()
    assert (row(second)["status"], len(api.requests)) == ("queued", 1)
    api.contracts.withdrawal_nonces[api.signer] = 1

    for _ in range(3):
        await withdraw_lane.run_round()

    assert (row(first)["status"], row(second)["status"], row(second)["nonce"]) == ("landed", "landed", 1)


async def test_a_lagging_withdrawal_nonce_read_never_signs_below_a_landed_one(withdraw_lane, api, config, monkeypatch):
    first = withdrawal(config)
    await withdraw_lane.run_round()
    await withdraw_lane.run_round()
    second = withdrawal(config, owner="swap:v:withdraw")
    real = api.contracts.withdrawal_nonce
    reads = [0]
    monkeypatch.setattr(api.contracts, "withdrawal_nonce", lambda address: reads.pop(0) if reads else real(address))

    await withdraw_lane.run_round()
    await withdraw_lane.run_round()

    assert [(row(i)["status"], row(i)["nonce"]) for i in (first, second)] == [("landed", 0), ("landed", 1)]
    assert api.contracts.withdrawal_nonces[api.signer] == 2


async def test_a_withdrawal_the_api_answers_as_rejected_fails_on_its_first_send(withdraw_lane, api, config):
    tx = withdrawal(config)
    api.replies = [(False, TransactionSubmissionResponse(submission_id="s", status="rejected", detail="no gas price"))]

    await withdraw_lane.run_round()

    assert row(tx)["status"] == "failed" and "rejected" in row(tx)["error"]
