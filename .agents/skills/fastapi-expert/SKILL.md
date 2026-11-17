---
name: fastapi-expert
description: "Use when building high-performance async Python APIs with FastAPI and Pydantic V2. Invoke to create REST endpoints, define Pydantic models, implement authentication flows, set up async SQLAlchemy database operations, add JWT authentication, build WebSocket endpoints, or generate OpenAPI documentation. Trigger terms: FastAPI, Pydantic, async Python, Python API, REST API Python, SQLAlchemy async, JWT authentication, OpenAPI, Swagger Python."
license: MIT
metadata:
  author: https://github.com/Jeffallan
  version: "1.1.0"
  domain: backend
  triggers: FastAPI, Pydantic, async Python, Python API, REST API Python, SQLAlchemy async, JWT authentication, OpenAPI, Swagger Python
  role: specialist
  scope: implementation
  output-format: code
  related-skills: fullstack-guardian, django-expert, test-master
---

# FastAPI Expert

Deep expertise in async Python, Pydantic V2, and production-grade API development with FastAPI.

## When to Use This Skill

- Building REST APIs with FastAPI
- Implementing Pydantic V2 validation schemas
- Setting up async database operations
- Implementing JWT authentication/authorization
- Creating WebSocket endpoints
- Optimizing API performance

## Core Workflow

1. **Analyze requirements** — Identify endpoints, data models, auth needs
2. **Design schemas** — Create Pydantic V2 models for validation
3. **Implement** — Write async endpoints with proper dependency injection
4. **Secure** — Add authentication, authorization, rate limiting
5. **Test** — Write async tests with pytest and httpx; run `pytest` after each endpoint group and verify OpenAPI docs at `/docs`

> **Checkpoint after each step:** confirm schemas validate correctly, endpoints return expected HTTP status codes, and `/docs` reflects the intended API surface before proceeding.

## Minimal Complete Example — Layered User Creation

Respect the application's existing architecture. The example below keeps FastAPI route registration
in the Router, HTTP concerns in the Handler, business rules in the Service, and SQLAlchemy access
in the Repository. Adapt dependency wiring and transaction ownership to the repository's existing
conventions rather than introducing a competing pattern.

```python
# schemas.py
from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    password: str
    name: str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    name: str | None = None
```

```python
# repositories/users.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import User

class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def create(self, *, email: str, hashed_password: str, name: str | None) -> User:
        user = User(email=email, hashed_password=hashed_password, name=name)
        self.db.add(user)
        await self.db.commit()  # Example only; preserve the project's transaction policy.
        await self.db.refresh(user)
        return user
```

```python
# services/users.py
from app.models import User
from app.repositories.users import UserRepository
from app.security import hash_password

class EmailAlreadyRegistered(Exception):
    pass

class UserService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    async def create_user(self, *, email: str, password: str, name: str | None) -> User:
        if await self.repository.get_by_email(email):
            raise EmailAlreadyRegistered()
        return await self.repository.create(
            email=email,
            hashed_password=hash_password(password),
            name=name,
        )
```

```python
# dependencies.py — use existing application dependency/session conventions
from typing import Annotated
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.repositories.users import UserRepository
from app.services.users import UserService

async def get_user_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserService:
    return UserService(UserRepository(db))
```

```python
# handlers/users.py
from typing import Annotated
from fastapi import Depends, HTTPException, status
from app.dependencies import get_user_service
from app.schemas import UserCreate, UserResponse
from app.services.users import EmailAlreadyRegistered, UserService

class UserHandler:
    async def create_user(
        self,
        payload: UserCreate,
        service: Annotated[UserService, Depends(get_user_service)],
    ) -> UserResponse:
        try:
            user = await service.create_user(
                email=payload.email, password=payload.password, name=payload.name
            )
        except EmailAlreadyRegistered as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            ) from exc
        return UserResponse.model_validate(user)
```

```python
# routers/users.py
from fastapi import APIRouter, status
from app.handlers.users import UserHandler
from app.schemas import UserResponse

router = APIRouter(prefix="/users", tags=["users"])
router.add_api_route(
    path="/",
    endpoint=UserHandler().create_user,
    methods=["POST"],
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
```

## JWT Authentication Snippet

```python
# security.py
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from typing import Annotated

SECRET_KEY = "read-from-env"  # use os.environ / settings
ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

def create_access_token(subject: str, expires_delta: timedelta = timedelta(minutes=30)) -> str:
    payload = {"sub": subject, "exp": datetime.now(timezone.utc) + expires_delta}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> str:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        subject: str | None = data.get("sub")
        if subject is None:
            raise ValueError
        return subject
    except (JWTError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

CurrentUser = Annotated[str, Depends(get_current_user)]
```

## Reference Guide

Load detailed guidance based on context:

| Topic | Reference | Load When |
|-------|-----------|-----------|
| Pydantic V2 | `references/pydantic-v2.md` | Creating schemas, validation, model_config |
| SQLAlchemy | `references/async-sqlalchemy.md` | Async database, models, CRUD operations |
| Endpoints | `references/endpoints-routing.md` | APIRouter, dependencies, routing |
| Authentication | `references/authentication.md` | JWT, OAuth2, get_current_user |
| Testing | `references/testing-async.md` | pytest-asyncio, httpx, fixtures |
| Django Migration | `references/migration-from-django.md` | Migrating from Django/DRF to FastAPI |

## Constraints

### MUST DO
- Use type hints everywhere (FastAPI requires them)
- Use Pydantic V2 syntax (`field_validator`, `model_validator`, `model_config`)
- Use `Annotated` pattern for dependency injection
- Use async/await for all I/O operations
- Use `X | None` instead of `Optional[X]`
- Return proper HTTP status codes
- Document endpoints (auto-generated OpenAPI)

### MUST NOT DO
- Use synchronous database operations
- Skip Pydantic validation
- Store passwords in plain text
- Expose sensitive data in responses
- Use Pydantic V1 syntax (`@validator`, `class Config`)
- Mix sync and async code improperly
- Hardcode configuration values

## Output Templates

When implementing FastAPI features, provide:
1. Schema file (Pydantic models)
2. Endpoint file (router with endpoints)
3. CRUD operations if database involved
4. Brief explanation of key decisions

## Knowledge Reference

FastAPI, Pydantic V2, async SQLAlchemy, Alembic migrations, JWT/OAuth2, pytest-asyncio, httpx, BackgroundTasks, WebSockets, dependency injection, OpenAPI/Swagger

[Documentation](https://jeffallan.github.io/claude-skills/skills/backend/fastapi-expert/)
