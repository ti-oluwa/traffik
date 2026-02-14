import typing

from fastapi import APIRouter, Depends, FastAPI, Request

from traffik import HTTPThrottle, Rate
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.registry import BypassThrottleRule

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


async def global_rate(
    connection: Request, context: typing.Optional[typing.Dict[str, typing.Any]]
) -> Rate:
    """GET - 1000/minutes and POST/other - 300/minute"""
    if connection.scope["method"] == "GET":
        return Rate.parse("1000/min")
    # For POST (and other methods)
    return Rate.parse("300/min")


# Create rule to not apply global throttle to connection coming to the users router with method GET
bypass_GET_users = BypassThrottleRule(path="/api/v1/users", methods={"GET"})
# Create rule to not apply global throttle to connection coming to the orgs router with method POST
bypass_POST_orgs = BypassThrottleRule(path="/api/v1/orgs", methods={"POST"})

global_throttle = HTTPThrottle(
    "api:v1",
    rate=global_rate,
    rules={bypass_GET_users, bypass_POST_orgs},  # Add rules to global throttle
)
main_router = APIRouter(prefix="/api/v1", dependencies=[Depends(global_throttle)])


###############
# SUB ROUTERS #
###############

# -------------#
# USERS ROUTER #
# -------------#


async def get_user_id(connection: Request) -> str:
    return getattr(connection.state, "user_id", "__anon__")


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
# Add `users_throttle` a dep for the router
users_router = APIRouter(prefix="/users", dependencies=[Depends(users_throttle)])


# --------------------#
# ORGANIZATION ROUTER #
# --------------------#


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
# Add `orgs_throttle` a dep for the router
orgs_router = APIRouter(prefix="/organizations", dependencies=[Depends(orgs_throttle)])


@orgs_router.post("/")
async def create_organization(data: typing.Any):
    pass  # `orgs_router` throttle only applies


@orgs_router.get("/{org_id}")
@throttled(
    HTTPThrottle("orgs:get", rate="100/min")
)  # Hits `orgs_router` throttle + this
async def get_organization(org_id: str):
    pass


# We register the sub routers with the main router
main_router.include_router(users_router)
main_router.include_router(orgs_router)

app.include_router(main_router)
