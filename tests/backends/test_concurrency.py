"""
Multithreaded and multiprocess concurrency tests for backend operations.

Tests verify that backend operations are truly atomic and safe across:
- Multiple asyncio tasks (concurrent)
- Multiple OS threads (multithreaded)
- Multiple processes (multiprocess)
"""

import asyncio
import multiprocessing as mp
import threading

import pytest

from tests.conftest import BackendsGen


# ============================================================================
# ASYNCIO CONCURRENT TESTS (High concurrency with async tasks)
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
async def test_increment_high_concurrency(backends: BackendsGen) -> None:
    """Test increment with 100 concurrent async tasks."""
    for backend in backends(namespace="concurrent_increment_100"):
        async with backend():
            key = await backend.get_key("counter")

            # 100 concurrent increments
            results = await asyncio.gather(*[backend.increment(key) for _ in range(100)])

            # All values should be unique (atomicity guarantee)
            assert len(set(results)) == 100, f"Duplicate values found: {sorted(results)}"
            assert min(results) == 1
            assert max(results) == 100

            # Final value should be 100
            final = await backend.get(key)
            assert final == "100"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
async def test_decrement_high_concurrency(backends: BackendsGen) -> None:
    """Test decrement with 100 concurrent async tasks."""
    for backend in backends(namespace="concurrent_decrement_100"):
        async with backend():
            key = await backend.get_key("counter")
            
            # Set initial value to 100
            await backend.set(key, "100")

            # 100 concurrent decrements
            results = await asyncio.gather(*[backend.decrement(key) for _ in range(100)])

            # All values should be unique
            assert len(set(results)) == 100, f"Duplicate values found: {sorted(results)}"
            assert min(results) == 0
            assert max(results) == 99

            # Final value should be 0
            final = await backend.get(key)
            assert final == "0"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
async def test_increment_with_ttl_concurrent(backends: BackendsGen) -> None:
    """Test increment_with_ttl with 50 concurrent async tasks."""
    for backend in backends(namespace="concurrent_increment_ttl"):
        async with backend():
            key = await backend.get_key("ttl_counter")

            # 50 concurrent increments with TTL
            results = await asyncio.gather(
                *[backend.increment_with_ttl(key, amount=1, ttl=60) for _ in range(50)]
            )

            # All values should be unique
            assert len(set(results)) == 50, f"Duplicate values found: {sorted(results)}"
            assert set(results) == set(range(1, 51))

            # Final value should be 50
            final = await backend.get(key)
            assert final == "50"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
async def test_mixed_operations_concurrent(backends: BackendsGen) -> None:
    """Test mixed increment/decrement operations concurrently."""
    for backend in backends(namespace="concurrent_mixed_ops"):
        async with backend():
            key = await backend.get_key("mixed_counter")
            
            # Start at 100
            await backend.set(key, "100")

            # 50 increments and 30 decrements concurrently
            increment_tasks = [backend.increment(key) for _ in range(50)]
            decrement_tasks = [backend.decrement(key) for _ in range(30)]
            
            results = await asyncio.gather(*increment_tasks, *decrement_tasks)

            # All 80 results should be unique
            assert len(set(results)) == 80, "Race condition detected!"

            # Final value should be 120 (100 + 50 - 30)
            final = int(await backend.get(key) or "0")
            assert final == 120


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
@pytest.mark.stress
async def test_stress_concurrent_operations(backends: BackendsGen) -> None:
    """Stress test with 500 concurrent operations."""
    for backend in backends(namespace="stress_concurrent"):
        async with backend():
            key = await backend.get_key("stress_counter")

            # 500 concurrent increments
            results = await asyncio.gather(*[backend.increment(key) for _ in range(500)])

            # Verify atomicity
            assert len(set(results)) == 500, "Race condition under stress!"
            assert min(results) == 1
            assert max(results) == 500

            final = await backend.get(key)
            assert final == "500"


# ============================================================================
# MULTITHREADED TESTS (Real OS threads)
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.threading
async def test_increment_multithreaded(backends: BackendsGen) -> None:
    """Test increment with 20 real OS threads."""
    for backend in backends(namespace="threaded_increment"):
        async with backend():
            key = await backend.get_key("thread_counter")
            results = []
            results_lock = threading.Lock()
            errors = []

            def thread_worker(thread_id: int):
                """Worker function running in separate thread."""
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    # Each thread does 5 increments
                    thread_results = []
                    for _ in range(5):
                        value = loop.run_until_complete(backend.increment(key))
                        thread_results.append(value)
                    
                    with results_lock:
                        results.extend(thread_results)
                except Exception as e:
                    with results_lock:
                        errors.append((thread_id, str(e)))
                finally:
                    loop.close()

            # Create and start 20 threads
            threads = []
            for i in range(20):
                thread = threading.Thread(target=thread_worker, args=(i,))
                threads.append(thread)
                thread.start()

            # Wait for all threads to complete
            for thread in threads:
                thread.join()

            # Verify no errors
            assert len(errors) == 0, f"Errors occurred: {errors}"

            # Verify atomicity: 20 threads * 5 increments = 100 unique values
            assert len(results) == 100
            assert len(set(results)) == 100, f"Race condition! Duplicates: {sorted(results)}"
            assert set(results) == set(range(1, 101))

            # Verify final value
            final = await backend.get(key)
            assert final == "100"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.threading
async def test_decrement_multithreaded(backends: BackendsGen) -> None:
    """Test decrement with 10 real OS threads."""
    for backend in backends(namespace="threaded_decrement"):
        async with backend():
            key = await backend.get_key("thread_counter")
            
            # Set initial value to 100
            await backend.set(key, "100")
            
            results = []
            results_lock = threading.Lock()

            def thread_worker():
                """Worker function running in separate thread."""
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    # Each thread does 10 decrements
                    for _ in range(10):
                        value = loop.run_until_complete(backend.decrement(key))
                        with results_lock:
                            results.append(value)
                finally:
                    loop.close()

            # Create and start 10 threads
            threads = []
            for _ in range(10):
                thread = threading.Thread(target=thread_worker)
                threads.append(thread)
                thread.start()

            # Wait for all threads
            for thread in threads:
                thread.join()

            # Verify atomicity: 10 threads * 10 decrements = 100 unique values
            assert len(results) == 100
            assert len(set(results)) == 100, "Race condition detected!"
            assert min(results) == 0
            assert max(results) == 99

            # Final value should be 0
            final = await backend.get(key)
            assert final == "0"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.threading
async def test_mixed_operations_multithreaded(backends: BackendsGen) -> None:
    """Test mixed operations with multiple threads."""
    for backend in backends(namespace="threaded_mixed"):
        async with backend():
            key = await backend.get_key("mixed_counter")
            await backend.set(key, "50")
            
            results = []
            results_lock = threading.Lock()

            def increment_worker():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    for _ in range(10):
                        value = loop.run_until_complete(backend.increment(key))
                        with results_lock:
                            results.append(("inc", value))
                finally:
                    loop.close()

            def decrement_worker():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    for _ in range(10):
                        value = loop.run_until_complete(backend.decrement(key))
                        with results_lock:
                            results.append(("dec", value))
                finally:
                    loop.close()

            # Start 5 increment threads and 3 decrement threads
            threads = []
            for _ in range(5):
                threads.append(threading.Thread(target=increment_worker))
            for _ in range(3):
                threads.append(threading.Thread(target=decrement_worker))

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # Verify all operations completed
            assert len(results) == 80  # 5*10 + 3*10

            # Verify all values are unique (no race conditions)
            values = [v for _, v in results]
            assert len(set(values)) == 80, "Race condition detected!"

            # Final value: 50 + (5*10) - (3*10) = 70
            final = await backend.get(key)
            assert final == "70"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.threading
async def test_lock_multithreaded(backends: BackendsGen) -> None:
    """Test that locks work correctly across threads."""
    for backend in backends(namespace="threaded_locks"):
        async with backend():
            key = await backend.get_key("shared_counter")
            await backend.set(key, "0")
            lock_name = "thread_lock"

            def locked_increment_worker():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    async def do_work():
                        async with await backend.lock(lock_name):
                            # Read, sleep, write pattern
                            value = int(await backend.get(key) or "0")
                            await asyncio.sleep(0.01)  # Simulate work
                            await backend.set(key, str(value + 1))
                    
                    loop.run_until_complete(do_work())
                finally:
                    loop.close()

            # Run 10 threads trying to increment
            threads = []
            for _ in range(10):
                thread = threading.Thread(target=locked_increment_worker)
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join()

            # With proper locking, final value should be exactly 10
            final = await backend.get(key)
            assert final == "10", "Lock failed to prevent race condition!"


# ============================================================================
# MULTIPROCESS TESTS (Separate OS processes)
# ============================================================================


def process_increment_worker(
    namespace: str,
    key_suffix: str,
    increment_count: int,
    process_id: int,
    results_queue: mp.Queue,
    backend_type: str,
):
    """Worker function that runs in a separate process."""
    import asyncio

    from redis.asyncio import Redis

    from traffik.backends.inmemory import InMemoryBackend
    from traffik.backends.redis import RedisBackend
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Create backend instance in this process
        if backend_type == "redis":
            redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
            backend = RedisBackend(connection=redis, namespace=namespace)
        else:
            backend = InMemoryBackend(namespace=namespace)
        
        async def do_increments():
            async with backend():
                key = await backend.get_key(key_suffix)
                process_results = []
                
                for _ in range(increment_count):
                    value = await backend.increment(key)
                    process_results.append(value)
                
                return process_results
        
        results = loop.run_until_complete(do_increments())
        results_queue.put((process_id, results))
        
    except Exception as e:
        results_queue.put((process_id, f"ERROR: {e}"))
    finally:
        loop.close()


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.multiprocess
async def test_increment_multiprocess(backends: BackendsGen) -> None:
    """Test increment across 5 separate processes."""
    for backend in backends(namespace="multiprocess_increment"):
        async with backend():
            key = await backend.get_key("process_counter")
            
            # Determine backend type
            backend_type = "redis" if "redis" in backend.__class__.__name__.lower() else "inmemory"
            
            # Skip InMemoryBackend (not shared across processes)
            if backend_type == "inmemory":
                pytest.skip("InMemoryBackend doesn't share state across processes")
            
            # Create result queue
            results_queue = mp.Queue()
            
            # Spawn 5 processes, each doing 10 increments
            processes = []
            for i in range(5):
                p = mp.Process(
                    target=process_increment_worker,
                    args=(backend.namespace, "process_counter", 10, i, results_queue, backend_type)
                )
                processes.append(p)
                p.start()
            
            # Wait for all processes
            for p in processes:
                p.join()
            
            # Collect results
            all_results = []
            while not results_queue.empty():
                process_id, results = results_queue.get()
                if isinstance(results, str) and results.startswith("ERROR"):
                    pytest.fail(f"Process {process_id} failed: {results}")
                all_results.extend(results)
            
            # Verify atomicity across processes
            assert len(all_results) == 50, f"Expected 50 results, got {len(all_results)}"
            assert len(set(all_results)) == 50, f"Race condition! Duplicates found: {sorted(all_results)}"
            assert set(all_results) == set(range(1, 51))
            
            # Verify final value
            final = await backend.get(key)
            assert final == "50"


def process_mixed_worker(
    namespace: str,
    key_suffix: str,
    operation: str,
    count: int,
    process_id: int,
    results_queue: mp.Queue,
):
    """Worker for mixed operations in separate process."""
    import asyncio

    from redis.asyncio import Redis

    from traffik.backends.redis import RedisBackend
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
        backend = RedisBackend(connection=redis, namespace=namespace)
        
        async def do_operations():
            async with backend():
                key = await backend.get_key(key_suffix)
                results = []
                
                for _ in range(count):
                    if operation == "increment":
                        value = await backend.increment(key)
                    else:
                        value = await backend.decrement(key)
                    results.append((operation, value))
                
                return results
        
        results = loop.run_until_complete(do_operations())
        results_queue.put((process_id, results))
        
    except Exception as e:
        results_queue.put((process_id, f"ERROR: {e}"))
    finally:
        loop.close()


@pytest.mark.asyncio
@pytest.mark.redis
@pytest.mark.multiprocess
async def test_mixed_operations_multiprocess(backends: BackendsGen) -> None:
    """Test mixed operations across multiple processes (Redis only)."""
    for backend in backends(namespace="multiprocess_mixed"):
        # Skip if not Redis
        if "redis" not in backend.__class__.__name__.lower():
            pytest.skip("This test requires Redis backend")
        
        async with backend():
            key = await backend.get_key("mixed_counter")
            await backend.set(key, "100")
            
            results_queue = mp.Queue()
            
            # Spawn 3 increment processes and 2 decrement processes
            processes = []
            
            # Increment processes (3 * 10 = 30 increments)
            for i in range(3):
                p = mp.Process(
                    target=process_mixed_worker,
                    args=(backend.namespace, "mixed_counter", "increment", 10, i, results_queue)
                )
                processes.append(p)
            
            # Decrement processes (2 * 10 = 20 decrements)
            for i in range(3, 5):
                p = mp.Process(
                    target=process_mixed_worker,
                    args=(backend.namespace, "mixed_counter", "decrement", 10, i, results_queue)
                )
                processes.append(p)
            
            # Start all processes
            for p in processes:
                p.start()
            
            # Wait for completion
            for p in processes:
                p.join()
            
            # Collect results
            all_results = []
            while not results_queue.empty():
                process_id, results = results_queue.get()
                if isinstance(results, str) and results.startswith("ERROR"):
                    pytest.fail(f"Process {process_id} failed: {results}")
                all_results.extend(results)
            
            # Verify all operations completed
            assert len(all_results) == 50  # 30 + 20
            
            # Verify all values are unique
            values = [v for _, v in all_results]
            assert len(set(values)) == 50, "Race condition across processes!"
            
            # Final value: 100 + 30 - 20 = 110
            final = await backend.get(key)
            assert final == "110"


@pytest.mark.asyncio
@pytest.mark.redis
@pytest.mark.multiprocess
@pytest.mark.stress
async def test_stress_multiprocess(backends: BackendsGen) -> None:
    """Stress test with 10 processes doing 50 increments each (Redis only)."""
    for backend in backends(namespace="multiprocess_stress"):
        if "redis" not in backend.__class__.__name__.lower():
            pytest.skip("This test requires Redis backend")
        
        async with backend():
            key = await backend.get_key("stress_counter")
            
            results_queue = mp.Queue()
            
            # 10 processes, each doing 50 increments = 500 total
            processes = []
            for i in range(10):
                p = mp.Process(
                    target=process_increment_worker,
                    args=(backend.namespace, "stress_counter", 50, i, results_queue, "redis")
                )
                processes.append(p)
                p.start()
            
            for p in processes:
                p.join()
            
            # Collect and verify
            all_results = []
            while not results_queue.empty():
                process_id, results = results_queue.get()
                if isinstance(results, str) and results.startswith("ERROR"):
                    pytest.fail(f"Process {process_id} failed: {results}")
                all_results.extend(results)
            
            # Verify atomicity under stress
            assert len(all_results) == 500
            assert len(set(all_results)) == 500, "Race condition under multiprocess stress!"
            assert set(all_results) == set(range(1, 501))
            
            final = await backend.get(key)
            assert final == "500"
