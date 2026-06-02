from dotenv import load_dotenv

# Integration tests run against the live Testnet. The root tests/conftest.py
# loads .env.localnet first; override it here with .env.testnet and rebuild the
# cached settings so the app uses Testnet configuration.
load_dotenv(".env.testnet", override=True)

from src.core.config import load_settings

load_settings(refresh=True)
