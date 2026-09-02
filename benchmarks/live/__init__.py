"""
Real-process, real-network benchmark harness.

Application run sthe way it runs in production, as its own OS process,
listening on a real TCP socket, driven by a real async HTTP/WebSocket
client, instead of calling the ASGI callable directly in-process.
"""
