"""Optional MongoDB integration using the official PyMongo driver.

MongoDB documents are Pydantic models, separate from LenApi's SQLAlchemy
``Model`` classes and Alembic migrations.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field


class MultipleDocumentsFound(LookupError):
    """Raised when ``MongoDocument.get`` matches more than one document."""


class MongoDatabase:
    """Own sync and async PyMongo clients for one MongoDB database.

    Install with ``pip install 'lenapi[mongodb]'``. Network I/O stays lazy until
    the first query or an explicit ``ping()`` / ``aping()`` call.
    """

    def __init__(self, uri: str | None = None, database_name: str | None = None,
                 *, client=None, async_client=None, async_enabled: bool = True,
                 **client_options) -> None:
        self.uri = uri or os.getenv("MONGODB_URI")
        if not self.uri:
            raise ValueError("Pass uri or set MONGODB_URI")
        parsed_database = unquote(urlsplit(self.uri).path.lstrip("/").split("/", 1)[0])
        self.database_name = database_name or os.getenv("MONGODB_DATABASE") or parsed_database
        if not self.database_name:
            raise ValueError("The MongoDB DATABASE_URL must include a database name in its URI path")
        MongoClient = AsyncMongoClient = None
        if client is None or (async_enabled and async_client is None):
            try:
                from pymongo import AsyncMongoClient, MongoClient
            except ImportError as exc:
                raise ImportError("MongoDB support requires `pip install 'lenapi[mongodb]'`") from exc
        self.client = client if client is not None else MongoClient(self.uri, **client_options)
        self.async_enabled = async_enabled
        self.async_client = (
            async_client if async_client is not None else
            AsyncMongoClient(self.uri, **client_options) if async_enabled else None
        )
        self.database = self.client[self.database_name]
        self.async_database = self.async_client[self.database_name] if self.async_client is not None else None

    def collection(self, name: str):
        """Return a synchronous PyMongo collection."""
        return self.database[name]

    def acollection(self, name: str):
        """Return an asynchronous PyMongo collection."""
        if self.async_database is None:
            raise RuntimeError("Async MongoDB access is disabled; set ACTIVATE_ASYNC = True")
        return self.async_database[name]

    def ping(self) -> bool:
        """Check connectivity synchronously."""
        return self.client.admin.command("ping").get("ok") == 1.0

    async def aping(self) -> bool:
        """Check connectivity asynchronously."""
        if self.async_client is None:
            raise RuntimeError("Async MongoDB access is disabled; set ACTIVATE_ASYNC = True")
        result = await self.async_client.admin.command("ping")
        return result.get("ok") == 1.0

    def close(self) -> None:
        """Close the synchronous client."""
        self.client.close()

    async def aclose(self) -> None:
        """Close both clients; await this from ASGI shutdown or async code."""
        self.client.close()
        if self.async_client is not None:
            await self.async_client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()


def _object_id(value):
    if isinstance(value, str) and len(value) == 24:
        try:
            from bson import ObjectId
            if ObjectId.is_valid(value):
                return ObjectId(value)
        except ImportError:
            pass
    return value


def _mongo_filter(filters: dict[str, Any]) -> dict[str, Any]:
    def reject_operators(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str) or any(part.startswith("$") for part in key.split(".")):
                    raise ValueError(
                        "MongoDB filter operators are not accepted; use field values for exact-match filters"
                    )
                reject_operators(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                reject_operators(nested)

    reject_operators(filters)
    result = dict(filters)
    if "id" in result:
        result["_id"] = _object_id(result.pop("id"))
    if "_id" in result:
        result["_id"] = _object_id(result["_id"])
    return result


def _document_values(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    if "_id" in result:
        result["id"] = str(result.pop("_id"))
    return result


class MongoDocument(BaseModel):
    """Pydantic document base for MongoDB collections.

    Bind each subclass to a ``MongoDatabase``. The default collection is the
    lower-case class name; override ``collection_name`` to choose another.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")
    id: str | None = Field(default=None, alias="_id")
    collection_name: ClassVar[str | None] = None
    _mongo_database: ClassVar[MongoDatabase | None] = None

    @classmethod
    def bind(cls, database: MongoDatabase):
        """Bind this document class to a configured MongoDatabase."""
        cls._mongo_database = database
        return cls

    @classmethod
    def _collection_name(cls) -> str:
        return cls.collection_name or cls.__name__.lower()

    @classmethod
    def _database(cls) -> MongoDatabase:
        if cls._mongo_database is None:
            raise RuntimeError(f"Bind {cls.__name__} to MongoDatabase first: {cls.__name__}.bind(db)")
        return cls._mongo_database

    @classmethod
    def _from_document(cls, value: dict[str, Any] | None):
        return None if value is None else cls.model_validate(_document_values(value))

    def _as_document(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    def save(self):
        """Insert this document, or update its existing document by ``id``."""
        collection = type(self)._database().collection(type(self)._collection_name())
        values = self._as_document()
        if self.id is None:
            result = collection.insert_one(values)
            self.id = str(result.inserted_id)
        else:
            values.pop("_id", None)
            collection.update_one({"_id": _object_id(self.id)}, {"$set": values}, upsert=True)
        return self

    async def asave(self):
        """Async insert or update."""
        collection = type(self)._database().acollection(type(self)._collection_name())
        values = self._as_document()
        if self.id is None:
            result = await collection.insert_one(values)
            self.id = str(result.inserted_id)
        else:
            values.pop("_id", None)
            await collection.update_one({"_id": _object_id(self.id)}, {"$set": values}, upsert=True)
        return self

    @classmethod
    def create(cls, **values):
        """Validate, insert, and return a document."""
        return cls(**values).save()

    @classmethod
    async def acreate(cls, **values):
        """Async validate, insert, and return a document."""
        return await cls(**values).asave()

    @classmethod
    def get(cls, **filters):
        """Return one matching document, or ``None``; reject duplicate matches."""
        collection = cls._database().collection(cls._collection_name())
        query = _mongo_filter(filters)
        cursor = collection.find(query).limit(2)
        rows = list(cursor)
        if len(rows) > 1:
            raise MultipleDocumentsFound(f"More than one {cls.__name__} matches the query")
        return cls._from_document(rows[0]) if rows else None

    @classmethod
    async def aget(cls, **filters):
        """Async form of ``get``."""
        collection = cls._database().acollection(cls._collection_name())
        cursor = collection.find(_mongo_filter(filters)).limit(2)
        rows = await cursor.to_list(length=2)
        if len(rows) > 1:
            raise MultipleDocumentsFound(f"More than one {cls.__name__} matches the query")
        return cls._from_document(rows[0]) if rows else None

    @classmethod
    def filter(cls, **filters):
        """Return all documents matching a MongoDB query."""
        collection = cls._database().collection(cls._collection_name())
        return [cls._from_document(row) for row in collection.find(_mongo_filter(filters))]

    @classmethod
    async def afilter(cls, **filters):
        """Async form of ``filter``."""
        collection = cls._database().acollection(cls._collection_name())
        cursor = collection.find(_mongo_filter(filters))
        rows = await cursor.to_list(length=None)
        return [cls._from_document(row) for row in rows]

    @classmethod
    def all(cls):
        """Return every document in the collection."""
        return cls.filter()

    @classmethod
    async def aall(cls):
        """Async form of ``all``."""
        return await cls.afilter()

    @classmethod
    def count(cls, **filters) -> int:
        return cls._database().collection(cls._collection_name()).count_documents(_mongo_filter(filters))

    @classmethod
    async def acount(cls, **filters) -> int:
        return await cls._database().acollection(cls._collection_name()).count_documents(_mongo_filter(filters))

    def delete(self) -> bool:
        """Delete this persisted document; return whether one was removed."""
        if self.id is None:
            return False
        result = type(self)._database().collection(type(self)._collection_name()).delete_one(
            {"_id": _object_id(self.id)}
        )
        return result.deleted_count == 1

    async def adelete(self) -> bool:
        """Async form of ``delete``."""
        if self.id is None:
            return False
        result = await type(self)._database().acollection(type(self)._collection_name()).delete_one(
            {"_id": _object_id(self.id)}
        )
        return result.deleted_count == 1

    @classmethod
    def ensure_index(cls, keys, **options):
        """Create a collection index using PyMongo index key syntax."""
        return cls._database().collection(cls._collection_name()).create_index(keys, **options)

    @classmethod
    async def aensure_index(cls, keys, **options):
        """Async form of ``ensure_index``."""
        return await cls._database().acollection(cls._collection_name()).create_index(keys, **options)


__all__ = ["MongoDatabase", "MongoDocument", "MultipleDocumentsFound"]
