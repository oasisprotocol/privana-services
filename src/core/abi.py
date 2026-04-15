import json
from functools import lru_cache
from pathlib import Path

_ABI_DIR = Path(__file__).parent.parent / "abis"


@lru_cache(maxsize=None)
def load_abi(contract_name: str) -> list:
    path = _ABI_DIR / f"{contract_name}.json"
    with path.open() as f:
        artifact = json.load(f)
    return artifact["abi"]
