# 📊 Ethereum Data Indexer

High-performance Ethereum blockchain data indexer with GraphQL API.

## Architecture
```
ETH Node (Infura/Alchemy) → Indexer → PostgreSQL → GraphQL API
```

## Indexed Data
- Blocks & transactions
- ERC-20/721/1155 transfers
- DeFi protocol events
- Gas price history
- MEV bundles

## Performance
- 2,000 blocks/second indexing speed
- Real-time streaming via WebSocket
- Sub-100ms GraphQL query latency
