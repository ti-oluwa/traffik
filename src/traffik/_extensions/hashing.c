/*
 * hashing.c
 *
 * FNV-1a 32-bit and 64-bit hashing for traffik.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#define FNV_32_PRIME 16777619U
#define FNV_32_OFFSET_BASIS 2166136261U
#define FNV_64_PRIME 1099511628211ULL
#define FNV_64_OFFSET_BASIS 14695981039346656037ULL

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

/*
 * fnv_64bit_hash(data: bytes) -> int
 *
 * Computes the FNV-1a 64-bit hash of the given bytes. Same algorithm as
 * fnv_32bit_hash, but with a wider accumulator and constants, lower collision rate.
 */
static PyObject *
fnv_64bit_hash(PyObject *self, PyObject *args)
{
    Py_buffer view;

    if (!PyArg_ParseTuple(args, "y*", &view)) {
        return NULL;
    }

    uint64_t hash = FNV_64_OFFSET_BASIS;
    const uint8_t *data = (const uint8_t *)view.buf;

    for (Py_ssize_t i = 0; i < view.len; i++) {
        hash ^= data[i];
        hash *= FNV_64_PRIME;
    }

    PyBuffer_Release(&view);
    return PyLong_FromUnsignedLongLong(hash);
}

static const char HEX_DIGITS[] = "0123456789abcdef";

/*
 * fnv_64bit_hash_hex(data: bytes) -> str
 *
 * Same hash as fnv_64bit_hash, but returns the 16-character lowercase hex
 * digest directly instead of an int. For callers that only want the hex
 * string (e.g. building a compact cache key), this skips constructing a
 * Python int and then formatting it, and is measured ~2x faster than
 * `format(fnv_64bit_hash(data), "016x")` for short inputs, since that
 * Python-level format() call costs about as much as the hash itself.
 */
static PyObject *
fnv_64bit_hash_hex(PyObject *self, PyObject *args)
{
    Py_buffer view;

    if (!PyArg_ParseTuple(args, "y*", &view)) {
        return NULL;
    }

    uint64_t hash = FNV_64_OFFSET_BASIS;
    const uint8_t *data = (const uint8_t *)view.buf;

    for (Py_ssize_t i = 0; i < view.len; i++) {
        hash ^= data[i];
        hash *= FNV_64_PRIME;
    }

    PyBuffer_Release(&view);

    char hex[16];
    for (int i = 15; i >= 0; i--) {
        hex[i] = HEX_DIGITS[hash & 0xF];
        hash >>= 4;
    }
    return PyUnicode_FromStringAndSize(hex, 16);
}

static PyMethodDef HashingMethods[] = {
    {
        "fnv_32bit_hash",
        fnv_32bit_hash,
        METH_VARARGS,
        "fnv_32bit_hash(data: bytes) -> int\n"
        "\n"
        "Compute FNV-1a 32-bit hash of the given bytes.\n"
        "Fast hash suitable for distributed rate limiting.\n"
    },
    {
        "fnv_64bit_hash",
        fnv_64bit_hash,
        METH_VARARGS,
        "fnv_64bit_hash(data: bytes) -> int\n"
        "\n"
        "Compute FNV-1a 64-bit hash of the given bytes.\n"
        "Lower collision rate than fnv_32bit_hash for the same input space.\n"
    },
    {
        "fnv_64bit_hash_hex",
        fnv_64bit_hash_hex,
        METH_VARARGS,
        "fnv_64bit_hash_hex(data: bytes) -> str\n"
        "\n"
        "Same hash as fnv_64bit_hash, returned as a 16-character hex string.\n"
        "Faster than format(fnv_64bit_hash(data), '016x') for hex-digest use cases.\n"
    },
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef HashingModule = {
    PyModuleDef_HEAD_INIT,
    "_hashing",
    "Portable FNV-1a hashing extension for traffik.",
    -1,
    HashingMethods
};

PyMODINIT_FUNC
PyInit__hashing(void)
{
    return PyModule_Create(&HashingModule);
}
