
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
