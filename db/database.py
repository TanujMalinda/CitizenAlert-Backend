import asyncpg
from app.core.config import settings

_pool = None


async def connect_db():
    global _pool
    try:
        _pool = await asyncpg.create_pool(
            settings.DATABASE_URL,
            min_size=2,
            max_size=20,
            command_timeout=60,
        )
        print("[DB] PostgreSQL connected successfully")
    except Exception as e:
        print(f"[DB] Connection failed: {e}")
        print("[DB] Running with mock data fallback")
        _pool = None


async def disconnect_db():
    global _pool
    if _pool:
        await _pool.close()


def get_pool():
    return _pool


async def fetch(query: str, *args):
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args):
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def execute(query: str, *args):
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        return await conn.execute(query, *args)


async def fetchval(query: str, *args):
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        return await conn.fetchval(query, *args)