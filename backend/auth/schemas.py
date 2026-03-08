"""Authentication schemas (Pydantic models)."""
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator


class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str
    full_name: Optional[str] = None

    @field_validator("username")
    @classmethod
    def username_alphanumeric(cls, v):
        assert v.isalnum(), "Username must be alphanumeric"
        assert len(v) >= 3, "Username must be at least 3 characters"
        return v

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        assert len(v) >= 8, "Password must be at least 8 characters"
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    full_name: Optional[str]
    is_active: bool

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(BaseModel):
    user_id: Optional[int] = None
    email: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    refresh_token: str
