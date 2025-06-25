class NoLimit(Exception):
    """
    Exception raised when throttling should not be enforced.

    This exception is caught by the throttle and the client is allowed to proceed
    without being throttled.

    In this example, the exception is raised when the request is from a trusted source

    ```python
    import fastapi
    import starlette.requests import HTTPConnection

    TRUSTED_IPS = {...,}

    def get_ip(connection: HTTPConnection):
        ...

    def untrusted_ip_identifier(connection: HTTPConnection):
        client_ip = get_ip(connection)
        if client_ip in TRUSTED_IPS:
            raise NoLimit()
        return connection.client.host

    router = fastapi.APIRouter(
        dependencies=[
            throttle(identifier=untrusted_ip_identifier)
        ]
    )
    ```
    """

    pass


class AnonymousConnection(Exception):
    """
    Exception raised when the connection identifier cannot be determined.

    This exception is raised when the throttle backend cannot generate a unique identifier
    for the connection, which is necessary for throttling to work correctly.
    """

    pass
