"""Django-inspired model base and field helpers backed by SQLAlchemy."""

from typing import Any
from contextvars import ContextVar
import uuid

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, Numeric, String, Table, Text, Uuid, and_, delete, func, inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

from .response import HTTPException


_current_session: ContextVar[Any | None] = ContextVar("lenapi_current_session", default=None)
_current_async_session: ContextVar[Any | None] = ContextVar("lenapi_current_async_session", default=None)


class Aggregate:
    """Description of a SQL aggregate to be used with Model.aggregate()."""

    def __init__(self, function: str, field: str = "*"):
        if function not in {"count", "sum", "min", "max", "avg"}:
            raise ValueError(f"Unsupported aggregate '{function}'")
        self.function = function
        self.field = field


def Count(field: str = "*") -> Aggregate:
    return Aggregate("count", field)


def Sum(field: str) -> Aggregate:
    return Aggregate("sum", field)


def Min(field: str) -> Aggregate:
    return Aggregate("min", field)


def Max(field: str) -> Aggregate:
    return Aggregate("max", field)


def Avg(field: str) -> Aggregate:
    return Aggregate("avg", field)

CASCADE = "CASCADE"
SET_NULL = "SET_NULL"
PROTECT = "PROTECT"


class _CascadePolicy:
    delete = CASCADE


cascade = _CascadePolicy()
null = SET_NULL
protected = PROTECT


def set_current_session(session):
    return _current_session.set(session)


def reset_current_session(token) -> None:
    _current_session.reset(token)


def set_current_async_session(session):
    return _current_async_session.set(session)


def reset_current_async_session(token) -> None:
    _current_async_session.reset(token)


def _session():
    session = _current_session.get()
    if session is None:
        raise RuntimeError("Model queries require an active lenApi request with a configured database")
    return session


def _async_session(session=None):
    session = session or _current_async_session.get()
    if session is None:
        raise RuntimeError(
            "Async model queries require an active lenApi request with an AsyncDatabase, "
            "or an explicit AsyncSession argument"
        )
    return session


class _QueryResults(list):
    """List of model rows with bulk delete support."""

    def __init__(self, model, rows):
        super().__init__(rows)
        self.model = model

    def delete(self) -> int:
        session = _session()
        if any(not isinstance(row, self.model) for row in self):
            raise ValueError("Delete model results, not projected values")
        for row in self:
            session.delete(row)
        session.commit()
        return len(self)


class _QuerySet:
    _FILTER_OPERATORS = {
        "exact", "iexact", "ne", "gt", "gte", "lt", "lte", "in", "notin",
        "contains", "icontains", "startswith", "istartswith", "endswith", "iendswith", "isnull",
    }

    def __init__(self, model, *, search=False, search_any=False, columns=None, ordering=None, limit=None, offset=0, **filters):
        self.model = model
        self.filters = filters
        self.search = search
        self.search_any = search_any
        self.columns = list(columns) if columns is not None else None
        self.ordering = list(ordering or [])
        self._limit = limit
        self._offset = offset
        self._validate_pagination()

    def _validate_pagination(self):
        if self._limit is not None and (isinstance(self._limit, bool) or not isinstance(self._limit, int) or self._limit < 0):
            raise HTTPException(422, "limit must be a non-negative integer")
        if isinstance(self._offset, bool) or not isinstance(self._offset, int) or self._offset < 0:
            raise HTTPException(422, "offset must be a non-negative integer")

    def _column(self, field):
        field = "id" if field == "_id" else field
        if field not in self.model.__table__.columns:
            raise ValueError(f"Unknown field '{field}' for {self.model.__name__}")
        return getattr(self.model, field)

    def _statement(self):
        selected = [self._column(field) for field in self.columns] if self.columns else [self.model]
        statement = select(*selected)
        if self.search or self.search_any:
            conditions = []
            for field, value in self.filters.items():
                column = self._column(field)
                self._require_string_field(field, column)
                conditions.append(column.ilike(f"%{value}%"))
            if conditions:
                statement = statement.where(or_(*conditions) if self.search_any else and_(*conditions))
        else:
            conditions = self._filter_conditions()
            if conditions:
                statement = statement.where(and_(*conditions))
        for field, descending in self.ordering:
            column = self._column(field)
            statement = statement.order_by(column.desc() if descending else column.asc())
        if self._limit is not None:
            statement = statement.limit(self._limit)
        if self._offset:
            statement = statement.offset(self._offset)
        return statement

    def _require_string_field(self, field, column):
        if not isinstance(column.property.columns[0].type, String):
            raise ValueError(f"Search field '{field}' on {self.model.__name__} must be a string")

    def _filter_conditions(self):
        conditions = []
        for lookup, value in self.filters.items():
            field, separator, operator = lookup.rpartition("__")
            if not separator or operator not in self._FILTER_OPERATORS:
                field, operator = lookup, "exact"
            column = self._column(field)
            if operator == "exact":
                condition = column == value
            elif operator == "iexact":
                self._require_string_field(field, column)
                condition = column.ilike(value)
            elif operator == "ne":
                condition = column != value
            elif operator == "gt":
                condition = column > value
            elif operator == "gte":
                condition = column >= value
            elif operator == "lt":
                condition = column < value
            elif operator == "lte":
                condition = column <= value
            elif operator == "in":
                if not isinstance(value, (list, tuple, set, frozenset)):
                    raise ValueError(f"Filter '{lookup}' requires a list, tuple, or set")
                condition = column.in_(value)
            elif operator == "notin":
                if not isinstance(value, (list, tuple, set, frozenset)):
                    raise ValueError(f"Filter '{lookup}' requires a list, tuple, or set")
                condition = column.not_in(value)
            elif operator == "isnull":
                if not isinstance(value, bool):
                    raise ValueError(f"Filter '{lookup}' requires a boolean")
                condition = column.is_(None) if value else column.is_not(None)
            else:
                self._require_string_field(field, column)
                if operator == "contains":
                    condition = column.contains(value)
                elif operator == "icontains":
                    condition = column.ilike(f"%{value}%")
                elif operator == "startswith":
                    condition = column.startswith(value)
                elif operator == "istartswith":
                    condition = column.ilike(f"{value}%")
                elif operator == "endswith":
                    condition = column.endswith(value)
                else:  # iendswith
                    condition = column.ilike(f"%{value}")
            conditions.append(condition)
        return conditions

    def _aggregate_statement(self, group_by, aggregates):
        if not aggregates:
            raise ValueError("Provide at least one named aggregate, such as total=Sum('amount')")
        group_fields = (group_by,) if isinstance(group_by, str) else tuple(group_by or ())
        group_columns = [self._column(field) for field in group_fields]
        selections = list(group_columns)
        for name, aggregate in aggregates.items():
            if not isinstance(aggregate, Aggregate):
                raise TypeError(f"Aggregate '{name}' must use Count(), Sum(), Min(), Max(), or Avg()")
            expression = (
                func.count() if aggregate.function == "count" and aggregate.field == "*"
                else getattr(func, aggregate.function)(self._column(aggregate.field))
            )
            selections.append(expression.label(name))
        statement = select(*selections).select_from(self.model)
        if self.search or self.search_any:
            conditions = []
            for field, value in self.filters.items():
                column = self._column(field)
                self._require_string_field(field, column)
                conditions.append(column.ilike(f"%{value}%"))
            if conditions:
                statement = statement.where(or_(*conditions) if self.search_any else and_(*conditions))
        else:
            conditions = self._filter_conditions()
            if conditions:
                statement = statement.where(and_(*conditions))
        if group_columns:
            statement = statement.group_by(*group_columns)
        return statement

    def aggregate(self, *, group_by=None, **aggregates):
        """Return aggregate values, or one dictionary per group when grouped."""
        result = _session().execute(self._aggregate_statement(group_by, aggregates))
        if group_by is not None:
            return [dict(row._mapping) for row in result]
        row = result.mappings().one()
        return dict(row)

    def count(self, field: str = "*") -> int:
        """Count rows matching this query."""
        return self.aggregate(count=Count(field))["count"]

    def all(self, *, limit=None, offset=None):
        if limit is not None:
            self.limit(limit)
        if offset is not None:
            self.offset(offset)
        result = _session().execute(self._statement())
        if not self.columns:
            return _QueryResults(self.model, result.scalars().all())
        if len(self.columns) == 1:
            return _QueryResults(self.model, result.scalars().all())
        return _QueryResults(self.model, [dict(row._mapping) for row in result])

    def first(self):
        result = _session().execute(self._statement())
        if not self.columns or len(self.columns) == 1:
            return result.scalars().first()
        row = result.mappings().first()
        return None if row is None else dict(row)

    def one_or_none(self):
        """Return the sole matching row, or raise if the lookup is ambiguous."""
        result = _session().execute(self._statement())
        if not self.columns:
            return result.scalars().one_or_none()
        if len(self.columns) == 1:
            return result.scalars().one_or_none()
        row = result.mappings().one_or_none()
        return None if row is None else dict(row)

    def get(self, **filters):
        """Return the sole matching model instance, or None if no row matches."""
        query = self._clone_with_filters(filters)
        return query.one_or_none()

    def _clone_with_filters(self, filters):
        return _QuerySet(
            self.model, search=self.search, search_any=self.search_any,
            columns=self.columns, ordering=self.ordering,
            limit=self._limit, offset=self._offset,
            **(self.filters | filters),
        )

    def limit(self, value: int | None):
        """Limit the number of rows returned by the next query execution."""
        self._limit = value
        self._validate_pagination()
        return self

    def offset(self, value: int):
        """Skip this many rows before returning query results."""
        self._offset = value
        self._validate_pagination()
        return self

    def values(self, *columns):
        """Select the requested columns, defaulting to all model columns."""
        selected = columns or tuple(self.model.__table__.columns.keys())
        for field in selected:
            self._column(field)
        self.columns = list(selected)
        return self

    def order(self, field):
        descending = field.startswith("-")
        name = field[1:] if descending else field
        self._column(name)
        self.ordering.append((name, descending))
        return self

    def delete(self) -> int:
        """Delete rows matching this query and return the number removed."""
        if self._limit is not None or self._offset:
            raise ValueError("Cannot delete a paginated query; remove limit and offset first")
        session = _session()
        statement = delete(self.model)
        conditions = (
            [self._column(field).ilike(f"%{value}%") for field, value in self.filters.items()]
            if self.search or self.search_any else self._filter_conditions()
        )
        if conditions:
            statement = statement.where(or_(*conditions) if self.search_any else and_(*conditions))
        result = session.execute(statement)
        session.commit()
        return result.rowcount or 0

    def value(self, field: str | None = None):
        rows = self.all()
        if field is None:
            return rows
        if field not in self.model.__table__.columns:
            raise ValueError(f"Unknown field '{field}' for {self.model.__name__}")
        return [getattr(row, field) for row in rows]


class _AsyncQuerySet(_QuerySet):
    """Async SQLAlchemy query builder for the ``a*`` model operations."""

    def _clone_with_filters(self, filters):
        return _AsyncQuerySet(
            self.model, search=self.search, search_any=self.search_any,
            columns=self.columns, ordering=self.ordering,
            limit=self._limit, offset=self._offset, **(self.filters | filters),
        )

    async def all(self, *, limit=None, offset=None, session=None):
        if limit is not None:
            self.limit(limit)
        if offset is not None:
            self.offset(offset)
        result = await _async_session(session).execute(self._statement())
        if not self.columns or len(self.columns) == 1:
            return result.scalars().all()
        return [dict(row._mapping) for row in result]

    async def aggregate(self, *, group_by=None, session=None, **aggregates):
        result = await _async_session(session).execute(self._aggregate_statement(group_by, aggregates))
        if group_by is not None:
            return [dict(row._mapping) for row in result]
        row = result.mappings().one()
        return dict(row)

    async def count(self, field: str = "*", *, session=None) -> int:
        values = await self.aggregate(count=Count(field), session=session)
        return values["count"]

    async def first(self, *, session=None):
        result = await _async_session(session).execute(self._statement())
        if not self.columns or len(self.columns) == 1:
            return result.scalars().first()
        row = result.mappings().first()
        return None if row is None else dict(row)

    async def one_or_none(self, *, session=None):
        result = await _async_session(session).execute(self._statement())
        if not self.columns or len(self.columns) == 1:
            return result.scalars().one_or_none()
        row = result.mappings().one_or_none()
        return None if row is None else dict(row)

    async def get(self, *, session=None, **filters):
        return await self._clone_with_filters(filters).one_or_none(session=session)

    async def exists(self, *, session=None) -> bool:
        return await self.one_or_none(session=session) is not None

    async def delete(self, *, session=None) -> int:
        if self._limit is not None or self._offset:
            raise ValueError("Cannot delete a paginated query; remove limit and offset first")
        active_session = _async_session(session)
        statement = delete(self.model)
        conditions = (
            [self._column(field).ilike(f"%{value}%") for field, value in self.filters.items()]
            if self.search else self._filter_conditions()
        )
        if conditions:
            statement = statement.where(and_(*conditions))
        try:
            result = await active_session.execute(statement)
            await active_session.commit()
        except IntegrityError as exc:
            await active_session.rollback()
            raise HTTPException(409, "Cannot delete records; protected records still reference them") from exc
        return result.rowcount or 0


class _ModelManager:
    def __init__(self, model):
        self.model = model

    def all(self, *, limit=None, offset=0):
        return _QuerySet(self.model, limit=limit, offset=offset).all()

    def __call__(self, *columns, **filters):
        return _SingleQuery(self.model, columns=columns or None, **filters)

    def unique(self, *columns):
        return _SingleQuery(self.model, columns=columns or None)

    def __getattr__(self, name: str):
        if name == self.model.__name__.lower():
            return lambda **filters: _SingleQuery(self.model, **filters)
        raise AttributeError(name)


class _SingleQuery(_QuerySet):
    """A unique model lookup that can check existence or return its row."""

    _query_state = {"model", "filters", "search", "search_any", "columns", "ordering", "_limit", "_offset"}

    def _instance(self):
        instance = self.one_or_none()
        if instance is None:
            raise LookupError(f"No {self.model.__name__} row matched this lookup")
        if not isinstance(instance, self.model):
            raise ValueError("This operation requires a full model lookup, not selected columns")
        return instance

    def value(self, field: str | None = None):
        if field is not None:
            self.values(field)
        return self.one_or_none()

    def get(self, **filters):
        return self._clone_with_filters(filters).one_or_none()

    def exist(self, **filters) -> bool:
        return self._clone_with_filters(filters).one_or_none() is not None

    def exists(self, **filters) -> bool:
        return self.exist(**filters)

    def save(self):
        """Save changes made through this lookup and return the model instance."""
        return self._instance().save()

    def delete(self) -> bool:
        """Delete the uniquely matching row, returning whether one was removed."""
        session = _session()
        instance = self.one_or_none()
        if instance is None:
            return False
        if not isinstance(instance, self.model):
            raise ValueError("Delete a model lookup, not a projected-column lookup")
        session.delete(instance)
        session.commit()
        return True

    def delet(self) -> bool:
        """Alias for delete(), preserving the shorter spelling."""
        return self.delete()

    def special(self, foreign_key_name: str):
        """Resolve a declared foreign key and expose its related model by field name."""
        instance = self.one_or_none()
        if instance is None:
            return None
        foreign_key_column = self.model.__table__.columns.get(foreign_key_name)
        if foreign_key_column is None or not foreign_key_column.foreign_keys:
            raise ValueError(
                f"'{foreign_key_name}' is not a foreign key field on {self.model.__name__}"
            )
        foreign_key = next(iter(foreign_key_column.foreign_keys))
        target_column = foreign_key.column
        target_model = next(
            (
                mapper.class_
                for mapper in Model.registry.mappers
                if mapper.local_table is target_column.table
            ),
            None,
        )
        if target_model is None:
            raise ValueError(
                f"No lenApi model is registered for foreign key target '{target_column.table.fullname}'"
            )
        foreign_value = getattr(instance, foreign_key_name)
        related = None
        if foreign_value is not None:
            related = _session().scalar(
                select(target_model).where(target_column == foreign_value)
            )
        return _SpecialResult(instance, foreign_key_name, related)

    def __getattr__(self, name):
        return getattr(self._instance(), name)

    def __setattr__(self, name, value):
        if name in self._query_state or name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._instance(), name, value)


class _SpecialResult:
    """Proxy for a model row with one foreign-key field resolved to its model."""

    def __init__(self, instance, foreign_key_name: str, related):
        self._instance = instance
        self._foreign_key_name = foreign_key_name
        self._related = related

    def __getattr__(self, name):
        if name == self._foreign_key_name:
            return self._related
        return getattr(self._instance, name)

    def __repr__(self):
        return repr(self._instance)


class _TableQuery(_QuerySet):
    """Full-table query whose delete operation drops the model table."""

    def delete(self) -> bool:
        session = _session()
        connection = session.connection()
        if not inspect(connection).has_table(self.model.__tablename__):
            return False
        self.model.__table__.drop(bind=connection)
        session.commit()
        return True


class _ModelManagerDescriptor:
    def __get__(self, instance, owner):
        return _ModelManager(owner)


class _AsyncDeleteDescriptor:
    """Support both ``await Model.adelete(**filters)`` and instance deletion."""

    def __get__(self, instance, owner):
        if instance is None:
            async def delete_matching(*, session=None, **filters):
                if not filters:
                    raise ValueError("Model.adelete requires filters; use afilter(...).delete() for bulk deletion")
                return await _AsyncQuerySet(owner, **filters).delete(session=session)
            return delete_matching

        async def delete_instance(*, session=None):
            active_session = _async_session(session)
            if not inspect(instance).persistent:
                return False
            await active_session.delete(instance)
            try:
                await active_session.commit()
            except IntegrityError as exc:
                await active_session.rollback()
                raise HTTPException(
                    409,
                    f"Cannot delete {owner.__name__}; protected records still reference it",
                ) from exc
            return True
        return delete_instance


class Model(DeclarativeBase):
    """Base class for lenApi ORM models."""

    __abstract__ = True

    get = _ModelManagerDescriptor()
    adelete = _AsyncDeleteDescriptor()

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    @declared_attr.directive
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    @classmethod
    def create(cls, **values):
        instance = cls(**values)
        instance.save()
        return instance

    @classmethod
    async def acreate(cls, *, session=None, **values):
        """Create and persist a row using the request's AsyncSession."""
        instance = cls(**values)
        await instance.asave(session=session)
        return instance

    @classmethod
    async def aget(cls, *, session=None, **filters):
        """Return the sole matching row asynchronously, or None if it is absent."""
        return await _AsyncQuerySet(cls, **filters).one_or_none(session=session)

    @classmethod
    def afilter(cls, **filters):
        """Build an async query; execute it with ``await query.all()``."""
        return _AsyncQuerySet(cls, **filters)

    @classmethod
    def aggregate(cls, *, group_by=None, **aggregates):
        """Aggregate all rows, optionally returning one result per group."""
        return _QuerySet(cls).aggregate(group_by=group_by, **aggregates)

    @classmethod
    async def aaggregate(cls, *, group_by=None, session=None, **aggregates):
        """Async counterpart of aggregate()."""
        return await _AsyncQuerySet(cls).aggregate(
            group_by=group_by, session=session, **aggregates
        )

    @classmethod
    def count(cls) -> int:
        """Count all records in this model's table."""
        return _QuerySet(cls).count()

    @classmethod
    async def acount(cls, *, session=None) -> int:
        """Async count of all records in this model's table."""
        return await _AsyncQuerySet(cls).count(session=session)

    @classmethod
    def get_or_404(cls, **filters):
        """Return the single matching row, or raise an HTTP 404 exception."""
        instance = cls.get(**filters).get()
        if instance is None:
            raise HTTPException(404, f"{cls.__name__} not found")
        return instance

    @classmethod
    def create_update(cls, defaults: dict | None = None, **lookup):
        """Create a row when lookup misses; otherwise apply defaults and save."""
        if not lookup:
            raise ValueError("create_update requires at least one lookup field")
        instance = cls.get(**lookup).get()
        if instance is None:
            return cls.create(**(lookup | (defaults or {})))
        for field, value in (defaults or {}).items():
            setattr(instance, field, value)
        return instance.save()

    @classmethod
    def update_or_404(cls, defaults: dict | None = None, **lookup):
        """Update a matching row, or raise an HTTP 404 exception."""
        if not lookup:
            raise ValueError("update_or_404 requires at least one lookup field")
        instance = cls.get_or_404(**lookup)
        for field, value in (defaults or {}).items():
            setattr(instance, field, value)
        return instance.save()


    @classmethod
    def delete_or_404(cls, **filters) -> bool:
        """Delete exactly one matching row, or raise an HTTP 404 exception."""
        if not filters:
            raise ValueError("delete_or_404 requires at least one lookup field")
        instance = cls.get_or_404(**filters)
        instance.delete()
        return True

    @classmethod
    def get_or_create(cls, defaults: dict | None = None, **lookup):
        """Return (instance, created), following Django's get_or_create convention."""
        if not lookup:
            raise ValueError("get_or_create requires at least one lookup field")
        instance = cls.get(**lookup).get()
        if instance is not None:
            return instance, False
        return cls.create(**(lookup | (defaults or {}))), True

    @classmethod
    def all(cls, *, limit=None, offset=0):
        """Build a full-table query; calling delete() on it drops the table."""
        return _TableQuery(cls, limit=limit, offset=offset)

    def save(self):
        """Insert or update this model instance, commit, and refresh it."""
        session = _session()
        mapper = inspect(type(self))
        state = inspect(self)
        identity = state.identity
        primary_key = mapper.primary_key[0]
        current_id = identity[0] if identity else getattr(self, primary_key.key, None)
        for column in self.__table__.columns:
            value = getattr(self, column.key)
            if not column.unique or value is None:
                continue
            if identity is not None and not state.attrs[column.key].history.has_changes():
                continue
            statement = select(self.__class__).where(getattr(self.__class__, column.key) == value)
            if current_id is not None:
                statement = statement.where(primary_key != current_id)
            if session.scalar(statement) is not None:
                raise HTTPException(
                    409,
                    f"Double entries are not allowed: {self.__class__.__name__}.{column.key}",
                )
        session.add(self)
        session.commit()
        session.refresh(self)
        return self

    async def asave(self, *, session=None):
        """Insert or update this model instance asynchronously and refresh it."""
        active_session = _async_session(session)
        mapper = inspect(type(self))
        state = inspect(self)
        identity = state.identity
        primary_key = mapper.primary_key[0]
        current_id = identity[0] if identity else getattr(self, primary_key.key, None)
        for column in self.__table__.columns:
            value = getattr(self, column.key)
            if not column.unique or value is None:
                continue
            if identity is not None and not state.attrs[column.key].history.has_changes():
                continue
            statement = select(self.__class__).where(getattr(self.__class__, column.key) == value)
            if current_id is not None:
                statement = statement.where(primary_key != current_id)
            if (await active_session.scalar(statement)) is not None:
                raise HTTPException(
                    409,
                    f"Double entries are not allowed: {self.__class__.__name__}.{column.key}",
                )
        active_session.add(self)
        try:
            await active_session.commit()
            await active_session.refresh(self)
        except IntegrityError as exc:
            await active_session.rollback()
            raise HTTPException(409, "Duplicate or conflicting data") from exc
        return self

    def delete(self) -> bool:
        """Delete this model instance, commit, and report whether it was persisted."""
        from sqlalchemy import inspect as sqlalchemy_inspect

        session = _session()
        if not sqlalchemy_inspect(self).persistent:
            return False
        session.delete(self)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                409,
                f"Cannot delete {self.__class__.__name__}; protected records still reference it",
            ) from exc
        return True

    def delet(self) -> bool:
        """Alias for delete()."""
        return self.delete()

    @classmethod
    def filter(cls, **filters):
        return _QuerySet(cls, **filters)

    @classmethod
    def search(cls, **terms):
        """Find rows by case-insensitive substring matches on string fields."""
        return _QuerySet(cls, search=True, **terms)

    @classmethod
    def search_any(cls, query: str, *fields):
        """Search a term across selected string fields using OR matching."""
        if not isinstance(query, str) or not query:
            raise ValueError("search query must be a non-empty string")
        if not fields:
            fields = tuple(
                column.key for column in cls.__table__.columns
                if isinstance(column.type, String)
            )
        if not fields:
            raise ValueError(f"{cls.__name__} has no string fields to search")
        return _QuerySet(cls, search_any=True, **{field: query for field in fields})

    @classmethod
    def asearch(cls, **terms):
        """Build an async case-insensitive substring search across all terms."""
        return _AsyncQuerySet(cls, search=True, **terms)

    @classmethod
    def asearch_any(cls, query: str, *fields):
        """Build an async OR search across selected string fields."""
        if not isinstance(query, str) or not query:
            raise ValueError("search query must be a non-empty string")
        if not fields:
            fields = tuple(
                column.key for column in cls.__table__.columns
                if isinstance(column.type, String)
            )
        if not fields:
            raise ValueError(f"{cls.__name__} has no string fields to search")
        return _AsyncQuerySet(cls, search_any=True, **{field: query for field in fields})

    @classmethod
    def unique(cls, **filters):
        """Build a lookup that requires at most one matching row."""
        return _SingleQuery(cls, **filters)


def _field(sql_type: Any, **kwargs):
    blank = kwargs.pop("blank", True)
    info = dict(kwargs.pop("info", {}) or {})
    info.setdefault("blank", blank)
    kwargs["info"] = info
    kwargs.setdefault("nullable", True)
    return mapped_column(sql_type, **kwargs)


def AutoField(**kwargs):
    """Declare an integer primary key; `Model` already provides `id` by default."""
    kwargs.setdefault("primary_key", True)
    kwargs.setdefault("autoincrement", True)
    kwargs.setdefault("nullable", False)
    return _field(Integer, **kwargs)


def IntegerField(**kwargs):
    return _field(Integer, **kwargs)


def BigIntegerField(**kwargs):
    """Declare a signed 64-bit integer column."""
    return _field(BigInteger, **kwargs)


BigIntField = BigIntegerField


def StringField(max_length: int = 255, **kwargs):
    return _field(String(max_length), **kwargs)


def UUIDField(**kwargs):
    """Declare a native UUID column, generating a UUID4 when omitted."""
    kwargs.setdefault("default", uuid.uuid4)
    return _field(Uuid(as_uuid=True), **kwargs)


def ImageField(upload_to: str = "uploads/images", max_length: int = 500, **kwargs):
    """Store a relative image path; upload bytes through ``Request.form()``."""
    upload_to = upload_to.strip("/")
    if not upload_to or any(part in {".", ".."} for part in upload_to.split("/")):
        raise ValueError("upload_to must be a safe relative directory")
    info = dict(kwargs.pop("info", {}) or {})
    info.update({"upload_to": upload_to, "media_type": "image"})
    return _field(String(max_length), info=info, **kwargs)


def EmailField(max_length: int = 254, **kwargs):
    """Declare a string column intended for an email address."""
    return StringField(max_length=max_length, **kwargs)


def IPField(max_length: int = 45, **kwargs):
    """Declare a string column sized for IPv4 or IPv6 addresses."""
    return StringField(max_length=max_length, **kwargs)


def PhoneField(max_length: int = 32, **kwargs):
    """Declare a string column intended for an international phone number."""
    return StringField(max_length=max_length, **kwargs)


def TextField(**kwargs):
    return _field(Text, **kwargs)


# Spelling aliases for projects that prefer the original snake-style names.
Email_field = EmailField
IP_field = IPField
Phone_field = PhoneField
TExtfield = TextField


def BooleanField(**kwargs):
    """Declare a boolean column that defaults to ``True`` unless overridden."""
    kwargs.setdefault("default", True)
    return _field(Boolean, **kwargs)


def FloatField(**kwargs):
    return _field(Float, **kwargs)


def DecimalField(max_digits: int = 10, decimal_places: int = 2, **kwargs):
    """Declare a fixed-precision decimal column returning ``decimal.Decimal`` values."""
    if max_digits <= 0 or decimal_places < 0 or decimal_places > max_digits:
        raise ValueError("DecimalField requires max_digits > 0 and 0 <= decimal_places <= max_digits")
    return _field(Numeric(precision=max_digits, scale=decimal_places, asdecimal=True), **kwargs)


def DateTimeField(**kwargs):
    return _field(DateTime, **kwargs)


def JSONField(**kwargs):
    return _field(JSON, **kwargs)


def ForeignKeyField(target: str, *, model=PROTECT, on_delete: str | None = None, **kwargs):
    """Declare a foreign key with model=cascade.delete, model=null, or model=protected."""
    constraint_name = kwargs.pop("constraint_name", None)
    policies = {CASCADE: "CASCADE", SET_NULL: "SET NULL", PROTECT: "RESTRICT"}
    # Keep on_delete as a readable alias while accepting the requested `model=` API.
    if on_delete is not None:
        if model != PROTECT:
            raise ValueError("Pass either model= or on_delete=, not both")
        model = on_delete
    on_delete = model
    if isinstance(on_delete, str):
        normalized = on_delete.upper().replace(".", "_")
        aliases = {
            "CASCADE_DELETE": CASCADE,
            "NULL": SET_NULL,
            "PROTECTED": PROTECT,
        }
        on_delete = aliases.get(normalized, normalized)
    if on_delete not in policies:
        raise ValueError("on_delete must be CASCADE, SET_NULL, or PROTECT")
    nullable = kwargs.pop("nullable", True)
    if on_delete == SET_NULL and not nullable:
        raise ValueError("ForeignKeyField(on_delete=SET_NULL) requires nullable=True")
    return _field(
        ForeignKey(target, name=constraint_name, ondelete=policies[on_delete]),
        nullable=nullable,
        **kwargs,
    )


class _ManyRelatedSet(set):
    """Set-like SQLAlchemy relationship collection with list-style all()."""

    def all(self):
        return list(self)

    def set(self, values):
        self.clear()
        self.update(values)
        return self


def ManyToManyField(target, *, table_name: str | None = None, back_populates: str | None = None):
    """Declare a many-to-many relationship with an inferred association table.

    ``target`` may be a model class or its class name. By default the join
    table is named ``<source_table>_<field_name>`` and is included in model
    metadata so Alembic can migrate it.
    """
    field_holder = {}

    def make_relationship(cls):
        field_name = next(
            (name for name, value in cls.__dict__.items() if value is field_holder["attribute"]),
            None,
        )
        if field_name is None:
            raise RuntimeError("Could not determine ManyToManyField name")

        target_class = target if isinstance(target, type) else None
        target_name = target_class.__name__ if target_class is not None else str(target).rsplit(".", 1)[-1]
        target_table = getattr(target_class, "__tablename__", target_name.lower())
        source_table = cls.__tablename__
        association_name = table_name or f"{source_table}_{field_name}"
        association = cls.metadata.tables.get(association_name)
        if association is None:
            if source_table == target_table:
                source_column, target_column = f"{field_name}_source_id", f"{field_name}_target_id"
            else:
                source_column, target_column = f"{source_table}_id", f"{target_table}_id"
            association = Table(
                association_name,
                cls.metadata,
                Column(source_column, ForeignKey(f"{source_table}.id", ondelete="CASCADE"), primary_key=True),
                Column(target_column, ForeignKey(f"{target_table}.id", ondelete="CASCADE"), primary_key=True),
            )
        return relationship(
            target if target_class is not None else target_name,
            secondary=association,
            back_populates=back_populates,
            collection_class=_ManyRelatedSet,
        )

    attribute = declared_attr(make_relationship)
    field_holder["attribute"] = attribute
    return attribute


# Shared persistent key/value table for stateful security features. It is part
# of normal model metadata so `python lenora.py migration` manages its lifecycle.
SECURITY_STORE_TABLE = Table(
    "lenapi_security_store",
    Model.metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=True),
    Column("expires_at", Float, nullable=True),
)

# Persistent background jobs are filtered from ordinary migrations. The
# background-worker CLI command opts this table into migration when requested.
BACKGROUND_JOBS_TABLE = Table(
    "lenapi_background_jobs",
    Model.metadata,
    Column("job_id", String(36), primary_key=True),
    Column("task_name", String(200), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("available_at", Float, nullable=False, index=True),
    Column("locked_until", Float, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
)


class User(Model):
    """Framework-provided user table, included in every project's metadata."""

    username = StringField(max_length=120, unique=True)
    email = EmailField(max_length=254, unique=True, nullable=True)
    first_name = StringField(max_length=150, default="")
    last_name = StringField(max_length=150, default="")
    password = StringField(max_length=255)
    api_key = StringField(max_length=64, unique=True, nullable=True)


class AdminUser(Model):
    """Framework-owned administrator account for private docs access."""

    __tablename__ = "admin_user"

    username = StringField(max_length=120, unique=True, nullable=False)
    email = EmailField(max_length=254, unique=True, nullable=False)
    password = StringField(max_length=255, nullable=False)
    created_at = FloatField(default=0.0, nullable=False)


ADMIN_SESSION_TABLE = Table(
    "lenapi_admin_sessions",
    Model.metadata,
    Column(
        "session_hash",
        String(64),
        primary_key=True,
    ),
    Column(
        "admin_user_id",
        Integer,
        ForeignKey("admin_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("expires_at", Float, nullable=False, index=True),
)
