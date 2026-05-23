"C extensions for traffik."

def test_and_set_byte(buffer: memoryview, offset: int, /) -> int:
    """
    Atomic test-and-set operation on a single byte within a writable buffer.

    Atomically exchanges buf[offset] with 1 and returns the previous value.
    Uses acquire-release memory ordering to ensure correct ordering of memory
    accesses inside the critical section on all architectures.

    :param buffer: A writable bytes-like object (e.g. memoryview of a
       ` multiprocessing.SharedMemory` segment). The buffer must be writable.
    :param offset: The byte offset within the buffer. Must be within the valid
        range [0, len(buf)).
    :return: The previous value at buf[offset]. 0 indicates the lock was free
        and has been acquired; 1 indicates the lock was already held.
    :raises IndexError: If offset is out of range.
    """
    ...

def clear_byte(buffer: memoryview, offset: int, /) -> None:
    """
    Atomic store of 0 to a single byte within a writable buffer.

    Atomically stores 0 to buf[offset] with release memory ordering, ensuring
    all writes performed inside the critical section are visible to other
    processes before the lock byte is cleared.

    :param buffer: A writable bytes-like object (e.g. memoryview of a
        `multiprocessing.SharedMemory` segment). The buffer must be writable.
    :param offset: The byte offset within the buffer. Must be within the valid
        range [0, len(buf)).
    :raises IndexError: If offset is out of range.
    """
    ...

def fnv_32bit_hash(data: bytes, /) -> int:
    """
    Compute FNV-1a 32-bit hash of the given bytes. Deterministic across processes and platforms.

    Use this because Python's built-in `hash()` is randomized per-process
    since Python 3.3 and is therefore unusable for cross-process hash table
    probing.

    :param data: The bytes to hash.
    :return: The 32-bit hash value as an unsigned integer (0 to 2^32 - 1).
    """
    ...
