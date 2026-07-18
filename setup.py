from setuptools import Extension, setup

setup(
    name="traffik",
    version="1.2.0",
    description="Rate limiting for Starlette applications.",
    readme="README.md",
    authors=[{"name": "tioluwa", "email": "tioluwa.dev@gmail.com"}],
    maintainers=[{"name": "tioluwa", "email": "tioluwa.dev@gmail.com"}],
    license="MIT",
    keywords=[
        "starlette",
        "fastapi",
        "throttling",
        "rate-limiting",
        "api",
        "distributed-systems",
        "multiprocess",
        "redis",
        "memcached",
        "websocket",
        "asyncio",
        "middleware",
    ],
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Framework :: FastAPI",
        "Framework :: AsyncIO",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Networking",
    ],
    python_requires=">=3.9,<3.15",
    install_requires=["starlette>=0.14.0", "annotated-types>=0.7.0"],
    extras_require={
        "all": [
            "traffik[aioredis]",
            "traffik[coredis]",
            "traffik[aiomcache]",
            "traffik[emcache]",
        ],
        "aioredis": ["pottery>=3.0.1", "redis>=5.0.0"],
        "coredis": ["coredis>=6.0;  python_version >= '3.10'"],
        "aiomcache": ["aiomcache>=0.8.2"],
        "emcache": [
            "emcache>=1.3.3; sys_platform == 'linux' or sys_platform == 'darwin'"
        ],
        # For backwards compatibility
        "redis": ["traffik[aioredis]"],
        "memcached": ["traffik[aiomcache]"],
    },
    project_urls={
        "Homepage": "https://github.com/ti-oluwa/traffik",
        "Documentation": "https://ti-oluwa.github.io/traffik/",
        "Repository": "https://github.com/ti-oluwa/traffik.git",
        "Bug Tracker": "https://github.com/ti-oluwa/traffik/issues",
        "Changelog": "https://github.com/ti-oluwa/traffik/blob/main/CHANGELOG.md",
    },
    package_dir={"": "src"},
    package_data={"traffik": ["py.typed"]},
    ext_modules=[
        Extension(
            name="traffik.backends._ext",
            sources=["src/traffik/backends/_cext/_ext.c"],
            extra_compile_args=["-O2"],
        )
    ],
)
