# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
from fastapi import Header, HTTPException
from app.config import settings

async def require_gateway_key(authorization: str | None=Header(default=None)):
    expected = f'Bearer {settings.gateway_api_key}'
    if authorization != expected:
        raise HTTPException(status_code=401, detail='Invalid API key', headers={'WWW-Authenticate': 'Bearer'})
