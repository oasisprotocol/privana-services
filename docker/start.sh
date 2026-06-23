#!/bin/bash

set -a

source .env

echo -n "Deriving OWNER_SECRET_KEY_ID from KMS..."
SECRET_KEY=`curl -s  --json '{ "key_id": "OWNER_SECRET_KEY_ID", "kind": "secp256k1" }' --unix-socket /run/rofl-appd.sock http://localhost/rofl/v1/keys/generate | jq -r .key`
echo "done."

pushd solidity
  if [ "${ACCOUNTING_CHAIN_ID}" = "23293" ]; then
    NETWORK=sapphire-localnet
  elif [ "${ACCOUNTING_CHAIN_ID}" = "23295" ]; then
    NETWORK=sapphire-testnet
  elif [ "${ACCOUNTING_CHAIN_ID}" = "23294" ]; then
    NETWORK=sapphire
  fi

  LIQUIDITY_PROVIDER_ADDRESS=`SECRET_KEY=${LIQUIDITY_PROVIDER_SECRET_KEY} npx hardhat deployerAddress --network ${NETWORK}`
  OWNER_ADDRESS=`npx hardhat deployerAddress --network ${NETWORK}`

  echo "Deploying or upgrading privana-services contracts on NETWORK=${NETWORK}..."

  OUT=`npx hardhat deploy --network ${NETWORK} --accountingaddress ${ACCOUNTING_CONTRACT_ADDRESS} --swapmanageraddress "${SWAP_MANAGER_CONTRACT_ADDRESS}" --lpaddress ${LIQUIDITY_PROVIDER_ADDRESS} --earnmanageraddress "${EARN_MANAGER_CONTRACT_ADDRESS}"`

  EARN_MANAGER_CONTRACT_ADDRESS=`echo "$OUT" | sed -n 's/.*EarnManager proxy at: //p'`
  SWAP_MANAGER_CONTRACT_ADDRESS=`echo "$OUT" | sed -n 's/.*SwapManager at: //p'`

  echo -e "\nOWNER_ADDRESS=${OWNER_ADDRESS}, EARN_MANAGER_CONTRACT_ADDRESS=${EARN_MANAGER_CONTRACT_ADDRESS}, SWAP_MANAGER_CONTRACT_ADDRESS=${SWAP_MANAGER_CONTRACT_ADDRESS}"
popd

echo -n "Updating replica metadata for OWNER_ADDRESS..."
curl  -X POST --json "{ \"OWNER_ADDRESS\": \"${OWNER_ADDRESS}\", \"EARN_MANAGER_CONTRACT_ADDRESS\": \"${EARN_MANAGER_CONTRACT_ADDRESS}\", \"SWAP_MANAGER_CONTRACT_ADDRESS\": \"${SWAP_MANAGER_CONTRACT_ADDRESS}\" }" --unix-socket /run/rofl-appd.sock http://localhost/rofl/v1/metadata
echo "done."

uv run uvicorn src.main:app --host ${API_HOST} --port ${API_PORT}
