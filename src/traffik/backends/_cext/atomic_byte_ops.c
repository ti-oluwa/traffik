/*
 * atomic_byte_ops.c
 *
 * Provides two atomic operations on a single byte within a writable
 * buffer (e.g. multiprocessing.SharedMemory):
 *
 *   test_and_set_byte(buffer, offset) -> int
 *       Atomic test-and-set.  Writes 1 to buffer[offset] and returns the
 *       *old* value.  Return value 0 means the caller acquired the lock;
 *       return value 1 means the lock was already held.
 *
 *   clear_byte(buffer, offset) -> None
 *       Atomic store of 0 to buffer[offset] with release semantics.
 *       Used to release the lock.
 *
 * Thread / process safety
 * -----------------------
 * Both operations use GCC / Clang built-in atomic intrinsics which map
 * to a single LOCK XCHG (test_and_set_byte) or MOV+MFENCE (clear_byte) on
 * x86-64, giving full cross-process safety over shared memory.
 *
 * Platform support
 * ----------------
 * GCC and Clang on Linux / macOS (x86-64, arm64).
 * MSVC is explicitly rejected at compile time because this backend is
 * already unsupported on Windows.
 *
 * Buffer protocol
 * ---------------
 * Both functions accept any writable bytes-like object (Py_buffer with
 * PyBUF_WRITABLE). The buffer is pinned for the duration of the call
 * via the Py_buffer protocol, preventing the GC from moving or freeing
 * the underlying memory while the C code holds its address.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#if defined(_MSC_VER)
#  error "traffik_atomic is not supported on Windows / MSVC."
#endif

#if !defined(__GNUC__) && !defined(__clang__)
#  error "traffik_atomic requires GCC or Clang atomic builtins."
#endif


/*
 * test_and_set_byte(buffer: writable-buffer, offset: int) -> int
 *
 * Atomically exchanges buffer[offset] with 1 and returns the previous
 * value. Uses acquire-release memory ordering so that all memory
 * accesses inside the critical section are correctly ordered relative
 * to this operation on every architecture.
 *
 * Returns
 * -------
 * 0  – lock was free; caller now holds it.
 * 1  – lock was already held; caller must retry.
 */
static PyObject *
test_and_set_byte(PyObject *self, PyObject *args)
{
    Py_buffer view;
    Py_ssize_t offset;

    /* w* requires a writable buffer — rejects read-only memoryviews early */
    if (!PyArg_ParseTuple(args, "w*n", &view, &offset)) {
        return NULL;
    }

    if (offset < 0 || offset >= view.len) {
        PyBuffer_Release(&view);
        PyErr_Format(PyExc_IndexError,
                     "offset %zd out of range for buffer of length %zd",
                     offset, view.len);
        return NULL;
    }

    uint8_t *ptr = (uint8_t *)view.buf + offset;
    /*
     * __atomic_exchange_n(ptr, 1, __ATOMIC_ACQ_REL)
     *
     * ACQ_REL = acquire on the exchange (so loads after this point see
     * all stores done before the matching release) + release on the
     * store (so all our prior stores are visible before the new value
     * is written). This is the correct ordering for a mutex acquire.
     */
    uint8_t old = __atomic_exchange_n(ptr, (uint8_t)1, __ATOMIC_ACQ_REL);

    PyBuffer_Release(&view);
    return PyLong_FromLong((long)old);
}


/*
 * clear_byte(buffer: writable-buffer, offset: int) -> None
 *
 * Atomically stores 0 to buffer[offset] with release memory ordering,
 * making all writes performed inside the critical section visible to
 * other processes before the lock byte is cleared.
 */
static PyObject *
clear_byte(PyObject *self, PyObject *args)
{
    Py_buffer view;
    Py_ssize_t offset;

    if (!PyArg_ParseTuple(args, "w*n", &view, &offset)) {
        return NULL;
    }

    if (offset < 0 || offset >= view.len) {
        PyBuffer_Release(&view);
        PyErr_Format(PyExc_IndexError,
                     "offset %zd out of range for buffer of length %zd",
                     offset, view.len);
        return NULL;
    }

    uint8_t *ptr = (uint8_t *)view.buf + offset;
    __atomic_store_n(ptr, (uint8_t)0, __ATOMIC_RELEASE);

    PyBuffer_Release(&view);
    Py_RETURN_NONE;
}


static PyMethodDef AtomicByteOpsMethods[] = {
    {
        "test_and_set_byte",
        test_and_set_byte,
        METH_VARARGS,
        "test_and_set_byte(buffer, offset) -> int\n"
        "\n"
        "Atomic test-and-set on buffer[offset].  Writes 1 and returns the\n"
        "previous value.  0 = acquired, 1 = was already held.\n"
        "\n"
        "buffer must be a writable bytes-like object (e.g. the memoryview\n"
        "of a multiprocessing.SharedMemory segment).\n"
    },
    {
        "clear_byte",
        clear_byte,
        METH_VARARGS,
        "clear_byte(buffer, offset) -> None\n"
        "\n"
        "Atomic store of 0 to buffer[offset] with release memory ordering.\n"
        "Use to release a lock previously acquired with test_and_set_byte.\n"
    },
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef AtomicByteOpsModule = {
    PyModuleDef_HEAD_INIT,
    "_atomic_byte_ops",
    "Atomic byte operations for cross-process spinlocks\n"
    "stored in POSIX shared memory segments.",
    -1,
    AtomicByteOpsMethods
};

PyMODINIT_FUNC
PyInit__atomic_byte_ops(void)
{
    return PyModule_Create(&AtomicByteOpsModule);
}
