from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from instagram_publisher.security import valid_admin_key

bearer = HTTPBearer(auto_error=False)


def require_admin(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    if credentials is None or not valid_admin_key(
        credentials.credentials, request.app.state.settings.admin_api_key.get_secret_value()
    ):
        raise HTTPException(
            401, "A valid admin Bearer token is required", headers={"WWW-Authenticate": "Bearer"}
        )
