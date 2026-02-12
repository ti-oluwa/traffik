import typing

from fastapi import APIRouter, Depends, FastAPI, Request

from traffik import EXEMPTED, HTTPThrottle, Rate, default_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware

app = FastAPI(lifespan=InMemoryBackend().lifespan)

###############
# THE PROBLEM #
###############
"""
main_router - /api/v1 - (GET: 1000/minute, POST: 300/minute)
     - users_router: /users ( GET: 500/ minute, POST: uses main)
     - organization_router: /organization (GET: uses main_router throttle, POST: 600/minute)
         - get organization: GET Route ( 10/minute)
         - create organization: POST ( uses organization_router)
      ....
v2_router:
 ....
"""

###############
# MAIN ROUTER #
###############

# We can implement the global limit in two ways


async def global_rate(
    connection: Request, context: typing.Optional[typing.Dict[str, typing.Any]]
) -> Rate:
    """GET - 1000/minutes and POST/other - 300/minute"""
    if connection.scope["method"] == "GET":
        return Rate.parse("1000/min")
    # For POST (and other methods)
    return Rate.parse("300/min")


# 1. Use the `ThrottleMiddleware` with a predicate
global_throttle = HTTPThrottle("api:v1", rate=global_rate)


async def should_apply(connection: Request) -> bool:
    path = connection.scope["path"]
    method = connection.scope["method"]

    if path.startswith("/api/v1/users") and method == "GET":
        return False
    if path.startswith("/api/v1/organizations") and method == "POST":
        return False
    return True


middleware_throttle = MiddlewareThrottle(
    global_throttle,
    predicate=should_apply,
)
app.add_middleware(
    ThrottleMiddleware,  # type: ignore[arg-type]
    middleware_throttles=[middleware_throttle],
)
main_router = APIRouter(prefix="/api/v1")


# 2. Use a custom cost function
async def cost_func(
    connection: Request, context: typing.Optional[typing.Dict[str, typing.Any]]
) -> int:
    path = connection.scope["path"]
    method = connection.scope["method"]

    if path.startswith("/api/v1/users") and method == "GET":
        return 0  # Interpreted as "Do not throttle" so the hit is short-circuited immediately
    if path.startswith("/api/v1/organizations") and method == "POST":
        return 0
    return 1


global_throttle = HTTPThrottle(
    "api:v1",
    rate=global_rate,
    cost=cost_func,
)
main_router = APIRouter(prefix="/api/v1", dependencies=[Depends(global_throttle)])


# 3. Use a custom connection identifier
async def global_identifier(connection: Request) -> typing.Any:
    path = connection.scope["path"]
    method = connection.scope["method"]

    if path.startswith("/api/v1/users") and method == "GET":
        return EXEMPTED
    if path.startswith("/api/v1/organizations") and method == "POST":
        return EXEMPTED
    return await default_identifier(connection)


global_throttle = HTTPThrottle(
    "api:v1",
    rate=global_rate,
    identifier=global_identifier,
)
main_router = APIRouter(prefix="/api/v1", dependencies=[Depends(global_throttle)])


###############
# SUB ROUTERS #
###############

# -------------#
# USERS ROUTER #
# -------------#
# The `users` rate limit can be implemented in two ways


async def get_user_id(connection: Request) -> str:
    return connection.state.user_id


# 1. Use custom rate function
async def user_rate(
    connection: Request, context: typing.Optional[typing.Dict[str, typing.Any]]
) -> Rate:
    if connection.scope["method"] == "GET":
        return Rate.parse("500/min")
    return Rate()  # Unlimited rate (so no throttling for POST/other methods)


users_throttle = HTTPThrottle(
    "users",
    rate=user_rate,
    identifier=get_user_id,
)


# 2. Use custom cost function (more efficient)
async def user_cost(
    connection: Request, context: typing.Optional[typing.Dict[str, typing.Any]]
) -> int:
    if connection.scope["method"] == "GET":
        return 1
    return 0


users_throttle = HTTPThrottle(
    "users",
    rate="500/min",  # Applies to all methods but cost function indicates that only GET should be throttled
    cost=user_cost,
    identifier=get_user_id,
)

# Add `users_throttle` a dep for the router
users_router = APIRouter(
    prefix="/users", dependencies=[Depends(users_throttle)]
)


# --------------------#
# ORGANIZATION ROUTER #
# --------------------#
# The `organizations` rate limit can be implemented in two ways also


# 1. Use custom rate function
async def orgs_rate(
    connection: Request, context: typing.Optional[typing.Dict[str, typing.Any]]
) -> Rate:
    if connection.scope["method"] == "POST":
        return Rate.parse("600/min")
    return Rate()  # Unlimited rate (so no throttling for GET/other methods)


orgs_throttle = HTTPThrottle(
    "orgs",
    rate=orgs_rate,
    identifier=get_user_id,
)


# 2. Use custom cost function
async def orgs_cost(
    connection: Request, context: typing.Optional[typing.Dict[str, typing.Any]]
) -> int:
    if connection.scope["method"] == "POST":
        return 1
    return 0


orgs_throttle = HTTPThrottle(
    "orgs",
    rate="600/min",  # Applies to all methods but cost function indicates that only POST should be throttled
    cost=orgs_cost,
    identifier=get_user_id,
)

# Add `orgs_throttle` a dep for the router
orgs_router = APIRouter(prefix="/organizations", dependencies=[Depends(orgs_throttle)])


@orgs_router.post("/")
async def create_organization(org_id: str): ...  # `orgs_router` throttle only applies


@orgs_router.get("/{org_id}")
@throttled(
    HTTPThrottle("orgs:get", rate="100/min")
)  # Hits `orgs_router` throttle + this
async def get_organization(org_id: str): ...


# We register the sub routers with the main router
main_router.include_router(users_router)
main_router.include_router(orgs_router)

app.include_router(main_router)
