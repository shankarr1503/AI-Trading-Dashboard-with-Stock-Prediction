"""
Admin CLI.

    python -m backend.manage create-admin --email you@example.com --username admin
    python -m backend.manage revoke-tokens --email you@example.com
    (password is read from ADMIN_PASSWORD or prompted)
"""
import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from backend.auth.utils import get_password_hash
from backend.database.models import User
from backend.database.session import AsyncSessionLocal


async def create_admin(email: str, username: str, password: str) -> str:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(email=email, username=username, hashed_password=get_password_hash(password), is_superuser=True)
            db.add(user)
            action = "created"
        else:
            user.is_superuser = True
            user.hashed_password = get_password_hash(password)
            user.token_version = (user.token_version or 0) + 1
            action = "promoted (password reset, old sessions revoked)"
        await db.commit()
        return action


async def revoke(email: str) -> bool:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            return False
        user.token_version = (user.token_version or 0) + 1
        await db.commit()
        return True


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.manage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin")
    ca.add_argument("--email", required=True)
    ca.add_argument("--username", required=True)
    rv = sub.add_parser("revoke-tokens")
    rv.add_argument("--email", required=True)
    args = parser.parse_args()
    if args.cmd == "create-admin":
        password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
        if len(password) < 12:
            print("Password must be at least 12 characters", file=sys.stderr)
            return 1
        print(f"Admin {args.email} {asyncio.run(create_admin(args.email, args.username, password))}")
        return 0
    ok = asyncio.run(revoke(args.email))
    print("Tokens revoked" if ok else "No such user")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
