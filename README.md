# Ethereum JSON-RPC Indexer

Index actual Ethereum execution-layer blocks and full transaction records from an RPC endpoint into a local SQLite database. The index is persistent and resumable, checks chain IDs, verifies parent hashes, and can roll back recent reorganized blocks before continuing.

The indexer does not generate synthetic blocks or transactions. It uses the endpoint and block range you provide.

## Requirements

- Python 3.10 or newer
- An Ethereum JSON-RPC endpoint that supports `eth_chainId`, `eth_blockNumber`, and `eth_getBlockByNumber` with full transactions

The implementation uses only the Python standard library. No package installation is needed.

## Configure and index

Set your provider endpoint in the environment. This avoids placing API credentials in shell history:

```bash
# PowerShell
$env:ETHEREUM_RPC_URL = "https://your-provider.example/v1/YOUR_API_KEY"
```

Or use a local node endpoint such as `http://127.0.0.1:8545`. Start by indexing a bounded range; runs are capped at 1,000 blocks by default.

```bash
python main.py --db data/mainnet.sqlite index --from-block 19000000 --max-blocks 100
```

Choose an inclusive end block when you need an exact range:

```bash
python main.py --db data/mainnet.sqlite index --from-block 19000000 --to-block 19000099
```

Continue from the saved checkpoint later:

```bash
python main.py --db data/mainnet.sqlite index --resume --max-blocks 1000
```

`--to-block` may also be used with `--resume`. A database is pinned to the chain ID first reported by its endpoint; using a different chain with the same database is rejected. On resume, parent hashes are checked. If the recent chain changed, rows after the last common ancestor are removed and re-indexed. The default rollback search covers 64 blocks; adjust `--reorg-lookback` for networks with different finality expectations.

## Query the local index

Summarize stored block and gas data:

```bash
python main.py --db data/mainnet.sqlite stats --from-block 19000000 --to-block 19000099
```

List the most recent indexed transactions sent or received by an address:

```bash
python main.py --db data/mainnet.sqlite address --address 0x0000000000000000000000000000000000000000 --limit 100
```

The database stores block number/hash/parent, timestamp, miner, gas totals, base fee, transaction count, transaction hash, sender/recipient, value in wei, gas limit, and gas price when supplied by the node. Wei values are stored as decimal text so they do not overflow SQLite's integer type.

## Scope and limitations

- This indexes block bodies and transaction fields only. It does not fetch receipts or logs, decode ERC transfers, index DeFi events, estimate actual per-transaction fees, provide GraphQL/WebSocket APIs, or claim a throughput benchmark.
- Index one chain per SQLite database. Start with a bounded range so a typo does not trigger an unintended full-chain backfill.
- Use a trusted RPC provider and protect its credentials. Public endpoints may rate-limit requests; this tool makes one block request at a time.
