"""
Create the first Admin user from ADMIN_EMAIL + ADMIN_PASSWORD env vars.
Idempotent — skips if an admin user already exists.

Usage:
    ADMIN_EMAIL=admin@cub.com ADMIN_PASSWORD=securepass python scripts/seed_admin.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select

from credit_report.database import AsyncSessionLocal, engine, Base
from credit_report.security.models import User
from credit_report.security.auth import hash_password


async def seed() -> None:
    email = os.getenv("ADMIN_EMAIL", "admin@cub.com")
    password = os.getenv("ADMIN_PASSWORD", "")

    if not password:
        print("ERROR: ADMIN_PASSWORD env var is required.", file=sys.stderr)
        sys.exit(1)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.role == "admin"))
        existing_admin = result.scalar_one_or_none()
        if existing_admin:
            print(f"Admin user already exists: {existing_admin.email} — skipping seed.")
            return

        result2 = await db.execute(select(User).where(User.email == email))
        if result2.scalar_one_or_none():
            print(f"User {email} already exists with a different role — skipping seed.")
            return

        admin = User(
            id=str(uuid.uuid4()),
            email=email,
            hashed_password=hash_password(password),
            role="admin",
            is_active=True,
        )
        db.add(admin)
        await db.commit()
        print(f"Admin user created: {email} (id={admin.id})")


if __name__ == "__main__":
    asyncio.run(seed())
