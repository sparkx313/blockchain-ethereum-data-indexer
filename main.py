"""Index Ethereum block headers and transactions from an execution-node RPC."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class JsonRpcClient:
    def __init__(self, endpoint: str, timeout: float = 30.0):
        if not endpoint or not endpoint.startswith(("https://", "http://")):
            raise ValueError("Provide an HTTP(S) JSON-RPC endpoint with --rpc-url or ETHEREUM_RPC_URL.")
        self.endpoint = endpoint
        self.timeout = timeout
        self.request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ValueError(f"JSON-RPC HTTP request failed with status {exc.code}.") from exc
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", None)
            detail = f" ({reason})" if reason else ""
            raise ValueError(f"JSON-RPC request failed{detail}.") from exc
        if not isinstance(payload, dict) or payload.get("id") != self.request_id:
            raise ValueError("JSON-RPC returned an invalid response envelope.")
        if "error" in payload:
            error = payload["error"]
            if isinstance(error, dict):
                raise ValueError(f"JSON-RPC error {error.get('code')}: {error.get('message', 'unknown error')}")
            raise ValueError("JSON-RPC returned an invalid error response.")
        if "result" not in payload:
            raise ValueError("JSON-RPC response is missing its result.")
        return payload["result"]

    def quantity(self, method: str, params: list[Any]) -> int:
        value = self.call(method, params)
        if not isinstance(value, str) or not value.startswith("0x"):
            raise ValueError(f"JSON-RPC method {method} did not return a hex quantity.")
        try:
            return int(value, 16)
        except ValueError as exc:
            raise ValueError(f"JSON-RPC method {method} returned an invalid hex quantity.") from exc

    def block(self, number: int, full_transactions: bool) -> dict[str, Any]:
        result = self.call("eth_getBlockByNumber", [hex(number), full_transactions])
        if result is None:
            raise ValueError(f"RPC endpoint did not return block {number}.")
        if not isinstance(result, dict):
            raise ValueError(f"RPC endpoint returned an invalid block {number}.")
        return result


def parse_quantity(value: Any, field: str, block_number: int) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"Block {block_number} has an invalid {field} quantity.")
    try:
        quantity = int(value, 16)
    except ValueError as exc:
        raise ValueError(f"Block {block_number} has an invalid {field} quantity.") from exc
    if quantity < 0:
        raise ValueError(f"Block {block_number} has a negative {field} quantity.")
    return quantity


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blocks (
            number INTEGER PRIMARY KEY,
            block_hash TEXT NOT NULL UNIQUE,
            parent_hash TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            miner TEXT NOT NULL,
            gas_used INTEGER NOT NULL,
            gas_limit INTEGER NOT NULL,
            base_fee_wei TEXT,
            transaction_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            tx_hash TEXT PRIMARY KEY,
            block_number INTEGER NOT NULL REFERENCES blocks(number) ON DELETE CASCADE,
            from_address TEXT NOT NULL,
            to_address TEXT,
            value_wei TEXT NOT NULL,
            gas_limit INTEGER NOT NULL,
            gas_price_wei TEXT
        );
        CREATE INDEX IF NOT EXISTS transactions_from_idx ON transactions(from_address, block_number);
        CREATE INDEX IF NOT EXISTS transactions_to_idx ON transactions(to_address, block_number);
        """
    )
    return connection


def metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def stored_hash(connection: sqlite3.Connection, number: int) -> str | None:
    row = connection.execute("SELECT block_hash FROM blocks WHERE number = ?", (number,)).fetchone()
    return row["block_hash"] if row else None


def normalize_hash(value: Any, field: str, block_number: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError(f"Block {block_number} has an invalid {field}.")
    return value.lower()


def normalize_address(value: Any, field: str, block_number: int, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError(f"Block {block_number} has a transaction with an invalid {field} address.")
    return value.lower()


def insert_block(connection: sqlite3.Connection, block: dict[str, Any], expected_number: int) -> None:
    number = parse_quantity(block.get("number"), "number", expected_number)
    if number != expected_number:
        raise ValueError(f"RPC returned block {number} while block {expected_number} was requested.")
    block_hash = normalize_hash(block.get("hash"), "hash", number)
    parent_hash = normalize_hash(block.get("parentHash"), "parentHash", number)
    timestamp = parse_quantity(block.get("timestamp"), "timestamp", number)
    gas_used = parse_quantity(block.get("gasUsed"), "gasUsed", number)
    gas_limit = parse_quantity(block.get("gasLimit"), "gasLimit", number)
    if gas_used > gas_limit:
        raise ValueError(f"Block {number} reports gasUsed greater than gasLimit.")
    miner_value = block.get("miner", block.get("author", "0x" + "0" * 40))
    miner = normalize_address(miner_value, "miner", number)
    base_fee = block.get("baseFeePerGas")
    base_fee_wei = (
        str(parse_quantity(base_fee, "baseFeePerGas", number)) if base_fee is not None else None
    )
    transactions = block.get("transactions")
    if not isinstance(transactions, list):
        raise ValueError(f"Block {number} is missing its transaction list.")

    connection.execute("DELETE FROM blocks WHERE number = ?", (number,))
    connection.execute(
        "INSERT INTO blocks(number, block_hash, parent_hash, timestamp, miner, gas_used, gas_limit, "
        "base_fee_wei, transaction_count) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (number, block_hash, parent_hash, timestamp, miner, gas_used, gas_limit, base_fee_wei, len(transactions)),
    )
    for transaction in transactions:
        if not isinstance(transaction, dict):
            raise ValueError(f"Block {number} contains a non-object transaction.")
        tx_hash = normalize_hash(transaction.get("hash"), "transaction hash", number)
        sender = normalize_address(transaction.get("from"), "sender", number)
        recipient = normalize_address(transaction.get("to"), "recipient", number, allow_none=True)
        value_wei = str(parse_quantity(transaction.get("value"), "transaction value", number))
        gas_limit_tx = parse_quantity(transaction.get("gas"), "transaction gas", number)
        gas_price = transaction.get("gasPrice")
        gas_price_wei = (
            str(parse_quantity(gas_price, "transaction gasPrice", number)) if gas_price is not None else None
        )
        connection.execute(
            "INSERT INTO transactions(tx_hash, block_number, from_address, to_address, value_wei, "
            "gas_limit, gas_price_wei) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (tx_hash, number, sender, recipient, value_wei, gas_limit_tx, gas_price_wei),
        )


def common_ancestor(
    rpc: JsonRpcClient,
    connection: sqlite3.Connection,
    before_block: int,
    lookback: int,
) -> int | None:
    lower_bound = max(0, before_block - lookback)
    for number in range(before_block, lower_bound - 1, -1):
        local_hash = stored_hash(connection, number)
        if local_hash is None:
            continue
        canonical_block = rpc.block(number, full_transactions=False)
        canonical_hash = normalize_hash(canonical_block.get("hash"), "hash", number)
        if local_hash == canonical_hash:
            return number
    return None


def run_index(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    endpoint = args.rpc_url or os.environ.get("ETHEREUM_RPC_URL")
    rpc = JsonRpcClient(endpoint)
    chain_id = rpc.quantity("eth_chainId", [])
    saved_chain_id = metadata(connection, "chain_id")
    if saved_chain_id is not None and int(saved_chain_id) != chain_id:
        raise ValueError(
            f"Database belongs to chain ID {saved_chain_id}; RPC endpoint is on chain ID {chain_id}."
        )
    set_metadata(connection, "chain_id", str(chain_id))

    saved_next = metadata(connection, "next_block")
    resume_tip_verified = False
    if args.resume:
        if saved_next is None:
            raise ValueError("This database has no checkpoint to resume. Start with --from-block.")
        start = int(saved_next)
    else:
        if saved_next is not None:
            raise ValueError("Database already has an indexing checkpoint; continue with --resume.")
        start = args.from_block
        if start < 0:
            raise ValueError("Start block must be zero or greater.")

    head = rpc.quantity("eth_blockNumber", [])
    if args.resume and start > 0 and start - 1 <= head:
        local_tip_hash = stored_hash(connection, start - 1)
        if local_tip_hash is not None:
            canonical_tip = rpc.block(start - 1, full_transactions=False)
            canonical_tip_hash = normalize_hash(canonical_tip.get("hash"), "hash", start - 1)
            resume_tip_verified = local_tip_hash == canonical_tip_hash
            if local_tip_hash != canonical_tip_hash:
                ancestor = common_ancestor(rpc, connection, start - 2, args.reorg_lookback)
                if ancestor is None:
                    raise ValueError(
                        f"No common ancestor found in the saved index within {args.reorg_lookback} blocks. "
                        "Increase --reorg-lookback or restart from an earlier block."
                    )
                with connection:
                    connection.execute("DELETE FROM blocks WHERE number > ?", (ancestor,))
                    set_metadata(connection, "next_block", str(ancestor + 1))
                print(f"Reorganization detected; rolled the index back to block {ancestor}.")
                start = ancestor + 1
    if start > head:
        if args.resume and start == head + 1 and resume_tip_verified:
            print(f"Index is already current through block {head}.")
            return
        raise ValueError(f"Next block {start} is above the node's current head {head}.")
    if args.to_block is not None:
        if args.to_block < start:
            raise ValueError("End block must be at least the start block.")
        if args.to_block > head:
            raise ValueError(f"End block {args.to_block} is above the node's current head {head}.")
        end = args.to_block
    else:
        end = min(head, start + args.max_blocks - 1)
    if end - start + 1 > args.max_blocks:
        raise ValueError("Requested range exceeds --max-blocks; raise the limit to continue.")
    if not args.resume:
        set_metadata(connection, "next_block", str(start))
        connection.commit()

    print(f"Indexing chain {chain_id}, blocks {start} through {end} into {args.db}.")
    indexed = 0
    next_block = start
    while next_block <= end:
        block = rpc.block(next_block, full_transactions=True)
        block_number = parse_quantity(block.get("number"), "number", next_block)
        if block_number != next_block:
            raise ValueError(f"RPC returned block {block_number} while block {next_block} was requested.")

        parent_hash = normalize_hash(block.get("parentHash"), "parentHash", next_block)
        previous_hash = stored_hash(connection, next_block - 1) if next_block > 0 else None
        if previous_hash is not None and parent_hash != previous_hash:
            ancestor = common_ancestor(rpc, connection, next_block - 2, args.reorg_lookback)
            if ancestor is None:
                raise ValueError(
                    f"No common ancestor found in the saved index within {args.reorg_lookback} blocks. "
                    "Increase --reorg-lookback or restart from an earlier block."
                )
            with connection:
                connection.execute("DELETE FROM blocks WHERE number > ?", (ancestor,))
                set_metadata(connection, "next_block", str(ancestor + 1))
            print(f"Reorganization detected; rolled the index back to block {ancestor}.")
            next_block = ancestor + 1
            continue

        with connection:
            insert_block(connection, block, next_block)
            next_block += 1
            set_metadata(connection, "next_block", str(next_block))
        indexed += 1
        if indexed % 100 == 0 or next_block > end:
            print(f"Indexed through block {next_block - 1} ({indexed} new blocks this run).")


def show_stats(connection: sqlite3.Connection, start: int | None, end: int | None) -> None:
    clauses: list[str] = []
    values: list[int] = []
    if start is not None:
        clauses.append("number >= ?")
        values.append(start)
    if end is not None:
        clauses.append("number <= ?")
        values.append(end)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    row = connection.execute(
        "SELECT COUNT(*) AS blocks, COALESCE(SUM(transaction_count), 0) AS transactions, "
        "COALESCE(SUM(gas_used), 0) AS gas_used, COALESCE(SUM(gas_limit), 0) AS gas_limit, "
        "MIN(number) AS first_block, MAX(number) AS last_block FROM blocks" + where,
        values,
    ).fetchone()
    utilization = row["gas_used"] / row["gas_limit"] if row["gas_limit"] else 0.0
    print(json.dumps({
        "chain_id": metadata(connection, "chain_id"),
        "first_block": row["first_block"],
        "last_block": row["last_block"],
        "blocks": row["blocks"],
        "transactions": row["transactions"],
        "gas_used": row["gas_used"],
        "gas_utilization": round(utilization, 6),
        "next_block": metadata(connection, "next_block"),
    }, indent=2))


def show_address(connection: sqlite3.Connection, address: str, limit: int) -> None:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("Address must be a 20-byte hex value prefixed with 0x.")
    rows = connection.execute(
        "SELECT t.tx_hash, t.block_number, b.timestamp, t.from_address, t.to_address, "
        "t.value_wei, t.gas_limit, t.gas_price_wei FROM transactions AS t "
        "JOIN blocks AS b ON b.number = t.block_number "
        "WHERE t.from_address = ? OR t.to_address = ? "
        "ORDER BY t.block_number DESC, t.tx_hash LIMIT ?",
        (address.lower(), address.lower(), limit),
    ).fetchall()
    print(json.dumps([dict(row) for row in rows], indent=2))


def positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Value must be a positive integer.") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return result


def nonnegative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Value must be a non-negative integer.") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("Value must be a non-negative integer.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index real Ethereum blocks from a JSON-RPC node into SQLite.")
    parser.add_argument("--db", type=Path, default=Path("ethereum.sqlite"), help="SQLite database path.")
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("index", help="Index or resume a bounded range of blocks.")
    index.add_argument("--rpc-url", help="JSON-RPC endpoint (or set ETHEREUM_RPC_URL).")
    start_group = index.add_mutually_exclusive_group(required=True)
    start_group.add_argument("--from-block", type=nonnegative_int, help="Initial block for a new database.")
    start_group.add_argument("--resume", action="store_true", help="Resume from the stored checkpoint.")
    index.add_argument("--to-block", type=nonnegative_int, help="Optional inclusive end block.")
    index.add_argument("--max-blocks", type=positive_int, default=1000, help="Maximum blocks to index per run.")
    index.add_argument("--reorg-lookback", type=nonnegative_int, default=64, help="Maximum canonicality lookback.")

    stats = commands.add_parser("stats", help="Summarize blocks already stored in SQLite.")
    stats.add_argument("--from-block", type=nonnegative_int)
    stats.add_argument("--to-block", type=nonnegative_int)

    address = commands.add_parser("address", help="List stored transactions involving an address.")
    address.add_argument("--address", required=True)
    address.add_argument("--limit", type=positive_int, default=100)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "stats" and args.from_block is not None and args.to_block is not None:
            if args.from_block > args.to_block:
                raise ValueError("Start block must not be greater than end block.")
        connection = open_database(args.db)
        try:
            if args.command == "index":
                run_index(args, connection)
            elif args.command == "stats":
                show_stats(connection, args.from_block, args.to_block)
            elif args.command == "address":
                show_address(connection, args.address, args.limit)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
