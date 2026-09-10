#!/bin/bash

set -a

# Preserve variables already in the environment e.g. injected via ROFL secrets.
preexisting_env=$(export -p)
source .env
eval "$preexisting_env"

pushd solidity
  if [ "${ACCOUNTING_CHAIN_ID}" = "23293" ]; then
    NETWORK=sapphire-localnet
  elif [ "${ACCOUNTING_CHAIN_ID}" = "23295" ]; then
    NETWORK=sapphire-testnet
  elif [ "${ACCOUNTING_CHAIN_ID}" = "23294" ]; then
    NETWORK=sapphire
  fi

  LIQUIDITY_PROVIDER_ADDRESS=`SECRET_KEY=${LIQUIDITY_PROVIDER_SECRET_KEY} npx hardhat deployer:address --network ${NETWORK}`
  EARN_POOL_ADDRESS=`SECRET_KEY=${EARN_POOL_SECRET_KEY} npx hardhat deployer:address --network ${NETWORK}`
popd

echo -n "Updating replica metadata..."
curl -X PUT --unix-socket /run/rofl-appd.sock http://localhost/rofl/v1/metadata --json @- <<EOF
{
  "EARN_MANAGER_CONTRACT_ADDRESS": "${EARN_MANAGER_CONTRACT_ADDRESS}",
  "SWAP_MANAGER_CONTRACT_ADDRESS": "${SWAP_MANAGER_CONTRACT_ADDRESS}",
  "LIQUIDITY_PROVIDER_ADDRESS": "${LIQUIDITY_PROVIDER_ADDRESS}",
  "EARN_POOL_ADDRESS": "${EARN_POOL_ADDRESS}"
}
EOF
echo "done."

uv run uvicorn src.main:app --host ${API_HOST} --port ${API_PORT}
