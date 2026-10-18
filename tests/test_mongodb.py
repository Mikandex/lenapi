import asyncio

import pytest

from lenapi.mongodb import MongoDatabase, MongoDocument, MultipleDocumentsFound


class _Result:
    def __init__(self, inserted_id=None, deleted_count=0):
        self.inserted_id = inserted_id
        self.deleted_count = deleted_count


class _Cursor(list):
    def limit(self, value):
        return _Cursor(self[:value])

    async def to_list(self, length=None):
        return self if length is None else self[:length]


class _AsyncCursor(_Cursor):
    pass


class _Collection:
    def __init__(self):
        self.rows = []
        self.next_id = 1

    def insert_one(self, values):
        row = dict(values)
        row["_id"] = f"test-{self.next_id}"
        self.next_id += 1
        self.rows.append(row)
        return _Result(inserted_id=row["_id"])

    def find(self, query):
        return _Cursor([dict(row) for row in self.rows if _matches(row, query)])

    async def find_one(self, query):
        return next((dict(row) for row in self.rows if _matches(row, query)), None)

    def update_one(self, query, update, upsert=False):
        row = next((row for row in self.rows if _matches(row, query)), None)
        if row is None and upsert:
            row = dict(query)
            self.rows.append(row)
        if row is not None:
            row.update(update["$set"])

    def delete_one(self, query):
        row = next((row for row in self.rows if _matches(row, query)), None)
        if row is not None:
            self.rows.remove(row)
        return _Result(deleted_count=int(row is not None))

    def count_documents(self, query):
        return sum(_matches(row, query) for row in self.rows)


class _AsyncCollection:
    def __init__(self, collection):
        self.collection = collection

    def find(self, query):
        return _AsyncCursor(self.collection.find(query))

    async def insert_one(self, values):
        row = dict(values)
        row["_id"] = f"test-{self.collection.next_id}"
        self.collection.next_id += 1
        self.collection.rows.append(row)
        return _Result(inserted_id=row["_id"])

    async def update_one(self, query, update, upsert=False):
        self.collection.update_one(query, update, upsert)

    async def delete_one(self, query):
        return self.collection.delete_one(query)

    async def count_documents(self, query):
        return self.collection.count_documents(query)


def _matches(row, query):
    return all(row.get(key) == value for key, value in query.items())


class _Client:
    def __init__(self, collection):
        self.collection_value = collection
        self.admin = self

    def __getitem__(self, _name):
        return _Database(self.collection_value)

    def command(self, _command):
        return {"ok": 1.0}

    def close(self):
        pass


class _AsyncClient(_Client):
    async def command(self, _command):
        return {"ok": 1.0}

    async def close(self):
        pass


class _Database:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, _name):
        return self.collection


class Student(MongoDocument):
    name: str
    course: str
    collection_name = "students_test"


@pytest.fixture
def mongo_db():
    sync_collection = _Collection()
    async_collection = _AsyncCollection(_Collection())
    sync_client = _Client(sync_collection)
    async_client = _AsyncClient(async_collection)
    db = MongoDatabase("mongodb://mock", "test", client=sync_client, async_client=async_client)
    Student.bind(db)
    yield db
    Student.bind(None)


def test_sync_document_crud(mongo_db):
    student = Student.create(name="Ada", course="Python")
    assert student.id == "test-1"
    assert Student.get(name="Ada").course == "Python"
    assert Student.filter(course="Python")[0].name == "Ada"
    assert Student.all()[0].id == student.id
    assert Student.count(course="Python") == 1

    student.course = "MongoDB"
    student.save()
    assert Student.get(id=student.id).course == "MongoDB"
    assert student.delete() is True
    assert Student.get(id=student.id) is None


def test_sync_get_rejects_multiple_matches(mongo_db):
    Student.create(name="Ada", course="Python")
    Student.create(name="Ada", course="MongoDB")
    with pytest.raises(MultipleDocumentsFound):
        Student.get(name="Ada")


def test_async_document_crud(mongo_db):
    async def run():
        student = await Student.acreate(name="Grace", course="Python")
        assert student.id == "test-1"
        assert (await Student.aget(name="Grace")).course == "Python"
        assert (await Student.afilter(course="Python"))[0].name == "Grace"
        assert (await Student.aall())[0].id == student.id
        assert await Student.acount(course="Python") == 1

        student.course = "MongoDB"
        await student.asave()
        assert (await Student.aget(id=student.id)).course == "MongoDB"
        assert await student.adelete() is True
        assert await Student.aget(id=student.id) is None

    asyncio.run(run())


def test_mongo_ping_sync_and_async(mongo_db):
    assert mongo_db.ping() is True
    assert asyncio.run(mongo_db.aping()) is True
