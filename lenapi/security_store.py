"""Shared database or Redis storage for stateful security features."""

import hashlib
import asyncio
import time

from sqlalchemy import Integer, String, case, cast, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from .models import SECURITY_STORE_TABLE
from .project import project_object_name


class SecurityStoreUnavailable(RuntimeError):
    """The selected shared store could not complete a security operation."""


class DatabaseSecurityStore:
    """Persist security state in the application's migrated SQL database."""

    def __init__(self, engine) -> None:
        self.engine = engine

    @staticmethod
    def _key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _upsert(self, key: str, value: str | None, expires_at: float | None) -> None:
        digest = self._key(key)
        values = {"value": value, "expires_at": expires_at}
        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
            statement = dialect_insert(SECURITY_STORE_TABLE).values(key=digest, **values)
            statement = statement.on_conflict_do_update(index_elements=["key"], set_=values)
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(SECURITY_STORE_TABLE).values(key=digest, **values)
            statement = statement.on_conflict_do_update(index_elements=["key"], set_=values)
        elif dialect == "mysql":
            from sqlalchemy.dialects.mysql import insert as dialect_insert
            statement = dialect_insert(SECURITY_STORE_TABLE).values(key=digest, **values)
            statement = statement.on_duplicate_key_update(**values)
        else:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(SECURITY_STORE_TABLE).where(SECURITY_STORE_TABLE.c.key == digest).values(**values)
                )
                if result.rowcount == 0:
                    try:
                        connection.execute(insert(SECURITY_STORE_TABLE).values(key=digest, **values))
                    except IntegrityError:
                        connection.execute(
                            update(SECURITY_STORE_TABLE).where(SECURITY_STORE_TABLE.c.key == digest).values(**values)
                        )
            return
        with self.engine.begin() as connection:
            connection.execute(statement)

    def _get(self, key: str) -> str | None:
        digest = self._key(key)
        now = time.time()
        with self.engine.begin() as connection:
            row = connection.execute(
                select(SECURITY_STORE_TABLE.c.value, SECURITY_STORE_TABLE.c.expires_at)
                .where(SECURITY_STORE_TABLE.c.key == digest)
            ).first()
            if row is None:
                return None
            if row.expires_at is not None and row.expires_at <= now:
                connection.execute(delete(SECURITY_STORE_TABLE).where(SECURITY_STORE_TABLE.c.key == digest))
                return None
            return row.value

    def _set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        self._upsert(key, value, time.time() + ttl if ttl is not None else None)

    def _delete(self, key: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(delete(SECURITY_STORE_TABLE).where(
                SECURITY_STORE_TABLE.c.key == self._key(key)
            ))

    def _claim(self, key: str, ttl: int) -> int:
        if ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        digest = self._key(key)
        now = time.time()
        try:
            with self.engine.begin() as connection:
                connection.execute(delete(SECURITY_STORE_TABLE).where(
                    SECURITY_STORE_TABLE.c.key == digest,
                    SECURITY_STORE_TABLE.c.expires_at <= now,
                ))
                connection.execute(insert(SECURITY_STORE_TABLE).values(
                    key=digest, value="1", expires_at=now + ttl
                ))
            return 0
        except IntegrityError:
            with self.engine.connect() as connection:
                expires_at = connection.execute(
                    select(SECURITY_STORE_TABLE.c.expires_at).where(SECURITY_STORE_TABLE.c.key == digest)
                ).scalar_one_or_none()
            return max(1, int(expires_at - time.time() + 0.999)) if expires_at is not None else 0

    def _increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")
        digest = self._key(key)
        now = time.time()
        expires = now + window_seconds
        expired = SECURITY_STORE_TABLE.c.expires_at <= now
        updates = {
            # Keep CASE branches integer-typed for PostgreSQL, then cast the
            # result back to text for the shared store's value column.
            "value": cast(
                case(
                    (expired, 1),
                    else_=cast(SECURITY_STORE_TABLE.c.value, Integer) + 1,
                ),
                String(64),
            ),
            "expires_at": case((expired, expires), else_=SECURITY_STORE_TABLE.c.expires_at),
        }
        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
            statement = dialect_insert(SECURITY_STORE_TABLE).values(
                key=digest, value="1", expires_at=expires
            ).on_conflict_do_update(index_elements=["key"], set_=updates)
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(SECURITY_STORE_TABLE).values(
                key=digest, value="1", expires_at=expires
            ).on_conflict_do_update(index_elements=["key"], set_=updates)
        elif dialect == "mysql":
            from sqlalchemy.dialects.mysql import insert as dialect_insert
            statement = dialect_insert(SECURITY_STORE_TABLE).values(
                key=digest, value="1", expires_at=expires
            ).on_duplicate_key_update(**updates)
        else:
            raise ValueError(f"Database rate limiting is not supported for dialect '{dialect}'")
        with self.engine.begin() as connection:
            connection.execute(statement)
            row = connection.execute(
                select(SECURITY_STORE_TABLE.c.value, SECURITY_STORE_TABLE.c.expires_at)
                .where(SECURITY_STORE_TABLE.c.key == digest)
            ).one()
        return int(row.value), max(1, int(row.expires_at - time.time() + 0.999))

    async def get(self, key: str) -> str | None:
        try:
            return await asyncio.to_thread(self._get, key)
        except Exception as exc:
            raise SecurityStoreUnavailable("Database security storage is unavailable") from exc

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        try:
            await asyncio.to_thread(self._set, key, value, ttl=ttl)
        except Exception as exc:
            raise SecurityStoreUnavailable("Database security storage is unavailable") from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._delete, key)
        except Exception as exc:
            raise SecurityStoreUnavailable("Database security storage is unavailable") from exc

    async def claim(self, key: str, ttl: int) -> int:
        try:
            return await asyncio.to_thread(self._claim, key, ttl)
        except Exception as exc:
            raise SecurityStoreUnavailable("Database security storage is unavailable") from exc

    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        try:
            return await asyncio.to_thread(self._increment, key, window_seconds)
        except Exception as exc:
            raise SecurityStoreUnavailable("Database security storage is unavailable") from exc


class RedisSecurityStore:
    """Store security state in Redis with atomic expiring operations."""

    def __init__(self, url: str) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise ValueError("Redis security storage requires `pip install lenapi[redis]`") from exc
        self.client = Redis.from_url(url, decode_responses=True)

    @staticmethod
    def _key(key: str) -> str:
        return "lenapi:security:" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    async def get(self, key: str) -> str | None:
        try:
            return await self.client.get(self._key(key))
        except Exception as exc:
            raise SecurityStoreUnavailable("Redis security storage is unavailable") from exc

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        try:
            await self.client.set(self._key(key), value, ex=ttl)
        except Exception as exc:
            raise SecurityStoreUnavailable("Redis security storage is unavailable") from exc

    async def delete(self, key: str) -> None:
        try:
            await self.client.delete(self._key(key))
        except Exception as exc:
            raise SecurityStoreUnavailable("Redis security storage is unavailable") from exc

    async def claim(self, key: str, ttl: int) -> int:
        if ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        redis_key = self._key(key)
        try:
            if await self.client.set(redis_key, "1", ex=ttl, nx=True):
                return 0
            return max(1, await self.client.ttl(redis_key))
        except Exception as exc:
            raise SecurityStoreUnavailable("Redis security storage is unavailable") from exc

    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")
        script = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {count, redis.call('TTL', KEYS[1])}
"""
        try:
            count, remaining = await self.client.eval(
                script, 1, self._key(key), window_seconds
            )
            return int(count), max(1, int(remaining))
        except Exception as exc:
            raise SecurityStoreUnavailable("Redis security storage is unavailable") from exc

    async def close(self) -> None:
        await self.client.aclose()


class MongoSecurityStore:
    """Persist shared security state in a collection in the selected MongoDB."""

    def __init__(self, mongodb) -> None:
        self.mongodb = mongodb
        self.collection = mongodb.collection(project_object_name("lenapi_security_store"))

    @staticmethod
    def _key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _get(self, key: str) -> str | None:
        digest = self._key(key)
        row = self.collection.find_one({"_id": digest})
        if row is None:
            return None
        expires_at = row.get("expires_at")
        if expires_at is not None and expires_at <= time.time():
            self.collection.delete_one({"_id": digest})
            return None
        return row.get("value")

    def _set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        expires_at = time.time() + ttl if ttl is not None else None
        self.collection.update_one(
            {"_id": self._key(key)},
            {"$set": {"value": value, "expires_at": expires_at}},
            upsert=True,
        )

    def _delete(self, key: str) -> None:
        self.collection.delete_one({"_id": self._key(key)})

    def _claim(self, key: str, ttl: int) -> int:
        if ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        digest = self._key(key)
        now = time.time()
        self.collection.delete_one({"_id": digest, "expires_at": {"$lte": now}})
        try:
            self.collection.insert_one({
                "_id": digest,
                "value": "1",
                "expires_at": now + ttl,
            })
            return 0
        except Exception:
            row = self.collection.find_one({"_id": digest})
            if row is None or row.get("expires_at") is None:
                raise
            return max(1, int(row["expires_at"] - time.time() + 0.999))

    def _increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")
        now = time.time()
        expired = {"$lte": [{"$ifNull": ["$expires_at", 0]}, now]}
        self.collection.update_one(
            {"_id": self._key(key)},
            [{"$set": {
                "value": {"$toString": {"$cond": [
                    expired,
                    1,
                    {"$add": [{"$convert": {
                        "input": "$value", "to": "int", "onError": 0, "onNull": 0
                    }}, 1]},
                ]}},
                "expires_at": {"$cond": [expired, now + window_seconds, "$expires_at"]},
            }}],
            upsert=True,
        )
        row = self.collection.find_one({"_id": self._key(key)})
        if row is None:
            raise RuntimeError("MongoDB rate counter was not stored")
        return int(row["value"]), max(1, int(row["expires_at"] - time.time() + 0.999))

    async def get(self, key: str) -> str | None:
        try:
            return await asyncio.to_thread(self._get, key)
        except Exception as exc:
            raise SecurityStoreUnavailable("MongoDB security storage is unavailable") from exc

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        try:
            await asyncio.to_thread(self._set, key, value, ttl)
        except Exception as exc:
            raise SecurityStoreUnavailable("MongoDB security storage is unavailable") from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._delete, key)
        except Exception as exc:
            raise SecurityStoreUnavailable("MongoDB security storage is unavailable") from exc

    async def claim(self, key: str, ttl: int) -> int:
        try:
            return await asyncio.to_thread(self._claim, key, ttl)
        except Exception as exc:
            raise SecurityStoreUnavailable("MongoDB security storage is unavailable") from exc

    async def increment(self, key: str, window_seconds: int) -> tuple[int, int]:
        try:
            return await asyncio.to_thread(self._increment, key, window_seconds)
        except Exception as exc:
            raise SecurityStoreUnavailable("MongoDB security storage is unavailable") from exc


def create_security_store(backend: str, *, engine=None, redis_url: str | None = None, mongodb=None):
    backend = backend.lower()
    if backend == "database":
        if mongodb is not None:
            return MongoSecurityStore(mongodb)
        if engine is None:
            raise ValueError("Database security storage requires an application Database")
        return DatabaseSecurityStore(engine)
    if backend == "redis":
        if not redis_url:
            raise ValueError("Redis security storage requires REDIS_URL")
        return RedisSecurityStore(redis_url)
    if backend == "memory":
        return None
    raise ValueError("SECURITY_STORAGE must be 'database', 'redis', or 'memory'")
