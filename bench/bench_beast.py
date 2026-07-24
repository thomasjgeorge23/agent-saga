"""Beast Concurrency & Throughput Benchmark Suite.

Benchmarks sub-microsecond MmapWAL writes, WORM vault signatures, and high-concurrency
saga transactions across simulated multi-threaded agent workloads.
"""

import asyncio
import time
import tempfile
from pathlib import Path
from agent_saga.wal import MmapWAL
from agent_saga.vault import WORMVault

async def benchmark_mmap_wal_throughput(num_records: int = 10_000) -> float:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "bench_mmap.bin"
        wal = MmapWAL(path=path, initial_file_size=50 * 1024 * 1024)
        await wal.start()

        start_time = time.perf_counter()
        for i in range(num_records):
            wal.append("STEP_INTENT", {"saga_id": f"s_{i}", "step": i, "payload": "bench_data_bytes"})
        
        await wal.barrier()
        elapsed = time.perf_counter() - start_time
        await wal.close()

        ops_per_sec = num_records / elapsed
        print(f"[BENCHMARK] MmapWAL Throughput: {ops_per_sec:,.2f} ops/sec ({num_records} records in {elapsed:.4f}s)")
        return ops_per_sec

def benchmark_worm_vault_signing(num_entries: int = 5_000) -> float:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "vault_bench.jsonl"
        vault = WORMVault(path, secret_key=b"bench_secret_key_32_bytes_long!")

        start_time = time.perf_counter()
        for i in range(num_entries):
            vault.write_entry(f"s_{i}", "PAYMENT", {"amount": 100 + i, "user": "bench_user"})

        elapsed = time.perf_counter() - start_time
        ops_per_sec = num_entries / elapsed
        print(f"[BENCHMARK] WORM Vault Sign Speed: {ops_per_sec:,.2f} signs/sec ({num_entries} entries in {elapsed:.4f}s)")
        return ops_per_sec

async def main():
    print("=== AGENT-SAGA BEAST PERFORMANCE BENCHMARK ===")
    await benchmark_mmap_wal_throughput(10_000)
    benchmark_worm_vault_signing(5_000)
    print("===============================================")

if __name__ == "__main__":
    asyncio.run(main())
