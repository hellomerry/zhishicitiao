import hashlib
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text
from src.db.session import SessionLocal

router = APIRouter()


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
async def login(payload: LoginIn):
    async with SessionLocal() as session:
        r = await session.execute(
            text("SELECT id, name, role FROM users WHERE name = :n"),
            {"n": payload.username})
        row = r.first()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        r2 = await session.execute(
            text("SELECT password_hash FROM users WHERE name = :n"),
            {"n": payload.username})
        stored = r2.scalar()
        if stored != hash_password(payload.password):
            return {"ok": False, "error": "密码错误"}
        return {"ok": True, "name": row[1], "role": row[2]}


class RegisterIn(BaseModel):
    username: str
    password: str
    role: str


@router.post("/api/auth/register")
async def register(payload: RegisterIn):
    async with SessionLocal() as session:
        r = await session.execute(
            text("SELECT id FROM users WHERE name = :n"), {"n": payload.username})
        if r.first():
            return {"ok": False, "error": "用户名已存在"}
        await session.execute(
            text("INSERT INTO users (name, role, password_hash) VALUES (:n, :r, :p)"),
            {"n": payload.username, "r": payload.role, "p": hash_password(payload.password)})
        await session.commit()
        return {"ok": True, "name": payload.username, "role": payload.role}
