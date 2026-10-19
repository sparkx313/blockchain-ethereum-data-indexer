import json
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class Transaction:
    hash: str
    block_number: int
    from_address: str
    to_address: Optional[str]
    value_wei: int
    gas_used: int
    gas_price: int

    @property
    def value_eth(self) -> float:
        return self.value_wei / 1e18

    @property
    def fee_eth(self) -> float:
        return (self.gas_used * self.gas_price) / 1e18

@dataclass
class Block:
    number: int
    hash: str
    timestamp: int
    miner: str
    transactions: List[Transaction] = field(default_factory=list)
    gas_used: int = 0
    gas_limit: int = 30_000_000

    @property
    def utilization(self) -> float:
        return self.gas_used / self.gas_limit

class EthereumIndexer:
    def __init__(self):
        self.blocks: Dict[int, Block] = {}
        self.tx_index: Dict[str, Transaction] = {}

    def index_block(self, block: Block):
        self.blocks[block.number] = block
        for tx in block.transactions:
            self.tx_index[tx.hash] = tx

    def get_address_txs(self, address: str) -> List[Transaction]:
        addr = address.lower()
        return [tx for tx in self.tx_index.values()
                if tx.from_address.lower() == addr or
                   (tx.to_address and tx.to_address.lower() == addr)]

    def get_block_stats(self, start: int, end: int) -> dict:
        blocks = [b for n, b in self.blocks.items() if start <= n <= end]
        if not blocks:
            return {}
        total_gas = sum(b.gas_used for b in blocks)
        total_txs = sum(len(b.transactions) for b in blocks)
        avg_util = sum(b.utilization for b in blocks) / len(blocks)
        return {"blocks": len(blocks), "transactions": total_txs,
                "avg_gas_utilization": f"{avg_util:.1%}",
                "total_gas_used": total_gas}

def generate_mock_block(number: int) -> Block:
    import random, hashlib
    txs = [
        Transaction(
            hash=hashlib.sha256(f"tx{number}{i}".encode()).hexdigest(),
            block_number=number,
            from_address=f"0x{'a'*40}",
            to_address=f"0x{'b'*40}",
            value_wei=random.randint(0, 10**18),
            gas_used=21000,
            gas_price=random.randint(10**9, 50*10**9),
        ) for i in range(random.randint(50, 200))
    ]
    return Block(
        number=number,
        hash=hashlib.sha256(f"block{number}".encode()).hexdigest(),
        timestamp=int(time.time()) - (19_000_000 - number) * 12,
        miner=f"0x{'c'*40}",
        transactions=txs,
        gas_used=sum(t.gas_used for t in txs),
    )

if __name__ == "__main__":
    indexer = EthereumIndexer()
    print("Indexing blocks 19_000_000 – 19_000_009...")
    for n in range(19_000_000, 19_000_010):
        indexer.index_block(generate_mock_block(n))
    stats = indexer.get_block_stats(19_000_000, 19_000_009)
    print(json.dumps(stats, indent=2))
