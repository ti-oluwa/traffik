from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "traffik._atomic_byte_ops",
            sources=["src/traffik/ext/atomic_byte_ops.c"],
            extra_compile_args=["-O2"],
        )
    ]
)
