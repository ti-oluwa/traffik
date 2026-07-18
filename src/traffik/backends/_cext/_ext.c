/*
 * _ext.c
 *
 * C extension module providing performance-critical operations for traffik:
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#if defined(_MSC_VER)
#  error "traffik extensions are not supported on Windows / MSVC."
#endif

#if !defined(__GNUC__) && !defined(__clang__)
#  error "traffik extensions require GCC or Clang atomic builtins."
#endif


/*
 * test_and_set_byte(buffer: writable-buffer, offset: int) -> int
 *
 * Atomically exchanges buffer[offset] with 1 and returns the previous
 * value. Uses acquire-release memory ordering so that all memory
 * accesses inside the critical section are correctly ordered relative
 * to this operation on every architecture.
 *
 * Provides two atomic operations on a single byte within a writable
 * buffer (e.g. multiprocessing.SharedMemory):
 *
 * Thread / process safety
 * -----------------------
 * Uses GCC / Clang built-in atomic intrinsics which map to a single
 * LOCK XCHG on x86-64, giving full cross-process safety over shared memory.
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

/*
 * fnv_32bit_hash(data: bytes) -> int
 *
 * Computes the FNV-1a 32-bit hash of the given bytes.
 * Fast, simple hash function suitable for distributed rate limiting.
 *
 * FNV-1a algorithm:
 *   hash = FNV_OFFSET_BASIS
 *   for each byte in data:
 *       hash ^= byte
 *       hash *= FNV_PRIME
 */
#define FNV_32_PRIME 16777619U
#define FNV_32_OFFSET_BASIS 2166136261U

static PyObject *
fnv_32bit_hash(PyObject *self, PyObject *args)
{
    Py_buffer view;

    if (!PyArg_ParseTuple(args, "y*", &view)) {
        return NULL;
    }

    uint32_t hash = FNV_32_OFFSET_BASIS;
    const uint8_t *data = (const uint8_t *)view.buf;

    for (Py_ssize_t i = 0; i < view.len; i++) {
        hash ^= data[i];
        hash *= FNV_32_PRIME;
    }

    PyBuffer_Release(&view);
    return PyLong_FromUnsignedLong(hash);
}


static PyMethodDef ExtMethods[] = {
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
    {
        "fnv_32bit_hash",
        fnv_32bit_hash,
        METH_VARARGS,
        "fnv_32bit_hash(data: bytes) -> int\n"
        "\n"
        "Compute FNV-1a 32-bit hash of the given bytes.\n"
        "Fast hash suitable for distributed rate limiting.\n"
    },
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef ExtModule = {
    PyModuleDef_HEAD_INIT,
    "_ext",
    "C extensions for traffik.",
    -1,
    ExtMethods
};

PyMODINIT_FUNC
PyInit__ext(void)
{
    return PyModule_Create(&ExtModule);
}
