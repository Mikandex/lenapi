"""Minimal app for trying lenApi routes and Pydantic request schemas."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from lenapi import Body, Depends, HTTPException, LenApi, Request, UploadFile, check_password, create_authenticator, hash_api_key, make_api_key, make_password
from app.models import Classroom, Photo, Student, User
import settings

app = LenApi()

if settings.AUTHENTICATION == "basic_login":
    identifier_field = getattr(settings, "BASIC_AUTH_FIELD", "username")
    if identifier_field not in User.__table__.columns:
        raise ValueError(f"BASIC_AUTH_FIELD '{identifier_field}' is not a field on User")


def authenticate_user(identifier, password):
    identifier_field = getattr(settings, "BASIC_AUTH_FIELD", "username")
    user = User.get(**{identifier_field: identifier}).get()
    if user is not None and check_password(password, user.password):
        return str(user.id)
    return None


def authenticate_api_key(api_key):
    if not api_key:
        return None
    user = User.get(api_key=hash_api_key(api_key)).get()
    return {"user_id": str(user.id), "auth_method": "api_key"} if user is not None else None


authenticator = create_authenticator(
    settings, authenticate=authenticate_user, authenticate_api_key=authenticate_api_key
)
if settings.AUTHENTICATION == "jwt":
    app.include_router(authenticator.router(authenticate_user))


@app.get("/auth/profile")
def auth_profile(identity=Depends(authenticator)):
    if isinstance(identity, dict) and "sub" in identity:
        return {"user_id": identity["sub"]}
    return {"identity": identity}


class StudentCreate(BaseModel):
    person_name: str
    course: str
    classroom_id: int | None = None


class StudentRead(StudentCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class StudentSummary(BaseModel):
    person_name: str
    course: str


class ClassroomCreate(BaseModel):
    name: str


class ClassroomRead(ClassroomCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class StudentBatchCreate(BaseModel):
    students: list[StudentCreate] = Field(min_length=1)


class StudentBatchUpdateItem(StudentCreate):
    id: int


class StudentBatchUpdate(BaseModel):
    students: list[StudentBatchUpdateItem] = Field(min_length=1)


class StudentBatchDelete(BaseModel):
    ids: list[int] = Field(min_length=1)


class StudentBatchDeleteResult(BaseModel):
    deleted: int


class UserRegistration(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    first_name: str = Field(default="", max_length=150)
    last_name: str = Field(default="", max_length=150)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.password != self.confirm_password:
            raise ValueError("password and confirm_password must match")
        return self


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    first_name: str
    last_name: str
    api_key: str | None = None


@app.post("/auth/register", response_model=UserPublic)
def register_user(data: UserRegistration):
    api_key = make_api_key() if settings.AUTHENTICATION == "api_key" else None
    user = User.create(
        username=data.username,
        email=data.email,
        first_name=data.first_name,
        last_name=data.last_name,
        password=make_password(data.password),
        api_key=hash_api_key(api_key) if api_key is not None else None,
    )
    return {"id": user.id, "username": user.username, "email": user.email,
            "first_name": user.first_name, "last_name": user.last_name, "api_key": api_key}


@app.get("/")
async def home():
    return {"message": "lenApi is running", "port": 8009}


@app.post(
    "/photos",
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"image": {"type": "string", "format": "binary"}},
                    "required": ["image"],
                }
            }
        },
    },
)
async def upload_photo(request: Request):
    form = await request.form()
    image = form.get("image")
    if not isinstance(image, UploadFile):
        raise HTTPException(422, "An image file is required in the 'image' field")

    upload_to = Photo.__table__.c.image.info["upload_to"]
    filename = await image.save_image(Path(settings.MEDIA_ROOT) / upload_to)
    photo = Photo(image=f"{upload_to}/{filename}")
    photo.save()
    return {
        "id": photo.id,
        "image": photo.image,
        "url": f"{settings.MEDIA_URL.rstrip('/')}/{photo.image}",
    }


@app.post("/home", response_model=StudentCreate)
async def create_home(data: StudentCreate):
    return data


@app.post("/webhooks/events")
async def receive_event(payload: Any = Body()):
    return payload


@app.post("/classrooms", response_model=ClassroomRead)
def create_classroom(data: ClassroomCreate):
    return Classroom.create(name=data.name)


@app.get("/classrooms", response_model=list[ClassroomRead])
def list_classrooms():
    return Classroom.get.all()


@app.post("/students", response_model=StudentRead)
def create_student(data: StudentCreate):
    student = Student(
        person_name=data.person_name,
        course=data.course,
        classroom_id=data.classroom_id,
    )
    student.save()
    return student


@app.post("/students/batch", response_model=list[StudentRead])
def create_students_batch(data: StudentBatchCreate, db: Session):
    names = [student.person_name for student in data.students]
    if len(names) != len(set(names)):
        raise HTTPException(409, "Double entries are not allowed in the student batch")
    existing = db.scalars(select(Student.person_name).where(Student.person_name.in_(names))).all()
    if existing:
        raise HTTPException(409, "A student name in this batch already exists")
    students = [Student(**student.model_dump()) for student in data.students]
    db.add_all(students)
    db.commit()
    for student in students:
        db.refresh(student)
    return students


@app.put("/students/batch", response_model=list[StudentRead])
def update_students_batch(data: StudentBatchUpdate, db: Session):
    items = data.students
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Student IDs must be unique in a batch update")
    students = db.scalars(select(Student).where(Student.id.in_(ids))).all()
    students_by_id = {student.id: student for student in students}
    missing = sorted(set(ids) - set(students_by_id))
    if missing:
        raise HTTPException(404, f"Student IDs not found: {missing}")

    changed_names = {
        item.person_name
        for item in items
        if item.person_name != students_by_id[item.id].person_name
    }
    for item in items:
        current_name = students_by_id[item.id].person_name
        if item.person_name != current_name and any(
            other.id != item.id and other.person_name == item.person_name for other in items
        ):
            raise HTTPException(409, "Double entries are not allowed in the student batch")
    if changed_names:
        conflicts = db.scalars(
            select(Student.person_name).where(
                Student.person_name.in_(changed_names),
                Student.id.not_in(ids),
            )
        ).all()
        if conflicts:
            raise HTTPException(409, "A student name in this batch already exists")

    for item in items:
        student = students_by_id[item.id]
        student.person_name = item.person_name
        student.course = item.course
        student.classroom_id = item.classroom_id
    db.commit()
    for student in students:
        db.refresh(student)
    return [students_by_id[item.id] for item in items]


@app.delete("/students/batch", response_model=StudentBatchDeleteResult)
def delete_students_batch(data: StudentBatchDelete, db: Session):
    ids = data.ids
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Student IDs must be unique in a batch delete")
    students = db.scalars(select(Student).where(Student.id.in_(ids))).all()
    found_ids = {student.id for student in students}
    missing = sorted(set(ids) - found_ids)
    if missing:
        raise HTTPException(404, f"Student IDs not found: {missing}")
    for student in students:
        db.delete(student)
    db.commit()
    return {"deleted": len(students)}


@app.put("/students/{student_id}", response_model=StudentRead)
def update_student(student_id: int, data: StudentCreate):
    return Student.update_or_404(
        defaults=data.model_dump(),
        id=student_id,
    )


@app.get("/students", response_model=list[StudentRead])
def list_students():
    return Student.filter().values().order("_id").all()


@app.get("/students/names", response_model=list[str])
def list_student_names():
    return Student.filter().value("person_name")


@app.get("/students/exists", response_model=bool)
def student_exists(person_name: str):
    return Student.get(person_name=person_name).exists()


@app.get("/students/lookup", response_model=str | None)
def lookup_student_name(person_name: str):
    return Student.get.unique("person_name").get(person_name=person_name)


@app.get("/students/summary", response_model=StudentSummary | None)
def lookup_student_summary(person_name: str):
    return Student.get("person_name", "course").get(person_name=person_name)


@app.get("/students/filter", response_model=list[StudentRead])
def filter_students(request: Request):
    """Filter students with query parameters such as course__icontains or id__gte."""
    params = dict(request.query_params)
    limit = params.pop("limit", "100")
    offset = params.pop("offset", "0")
    ordering = params.pop("order_by", "_id")
    filters = {}
    try:
        limit, offset = int(limit), int(offset)
        for lookup, raw_value in params.items():
            field, separator, operator = lookup.rpartition("__")
            field = field if separator and operator in Student.filter()._FILTER_OPERATORS else lookup
            column = Student.__table__.columns["id" if field == "_id" else field]
            python_type = column.type.python_type
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            if separator and operator in {"in", "notin"}:
                values = str(values[0]).split(",")
            converted = []
            for value in values:
                if python_type is bool:
                    lowered = str(value).lower()
                    if lowered not in {"true", "false", "1", "0"}:
                        raise ValueError(f"{lookup} must be true or false")
                    converted.append(lowered in {"true", "1"})
                else:
                    converted.append(python_type(value))
            filters[lookup] = converted if separator and operator in {"in", "notin"} else converted[-1]
        query = Student.filter(**filters).values().order(ordering).limit(limit).offset(offset)
        return query.all()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"Invalid student filter: {exc}") from exc


@app.get("/students/search", response_model=list[StudentRead])
def search_students(
    q: str | None = None,
    person_name: str | None = None,
    fields: str = "person_name,course",
    limit: int = 100,
    offset: int = 0,
):
    term = q or person_name
    if not term:
        raise HTTPException(422, "Provide a search term with q or person_name")
    try:
        return Student.search_any(term, *[field.strip() for field in fields.split(",") if field.strip()]) \
            .order("_id").limit(limit).offset(offset).all()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"Invalid student search: {exc}") from exc


@app.get("/students/{student_id}/classroom", response_model=ClassroomRead | None)
def get_student_classroom(student_id: int):
    student = Student.get(id=student_id).special("classroom_id")
    return None if student is None else student.classroom_id


@app.delete("/students/{student_id}", response_model=bool)
def delete_student(student_id: int):
    return Student.get(id=student_id).delete()


# Async CRUD example. These routes use the AsyncSession configured by LenApi.
@app.post("/async/students", response_model=StudentRead)
async def async_create_student(data: StudentCreate):
    return await Student.acreate(**data.model_dump())


@app.get("/async/students", response_model=list[StudentRead])
async def async_list_students():
    return await Student.afilter().order("_id").all()


@app.get("/async/students/{student_id}", response_model=StudentRead)
async def async_get_student(student_id: int):
    student = await Student.aget(id=student_id)
    if student is None:
        raise HTTPException(404, "Student not found")
    return student


@app.put("/async/students/{student_id}", response_model=StudentRead)
async def async_update_student(student_id: int, data: StudentCreate):
    student = await Student.aget(id=student_id)
    if student is None:
        raise HTTPException(404, "Student not found")
    for field, value in data.model_dump().items():
        setattr(student, field, value)
    return await student.asave()


@app.delete("/async/students/{student_id}", response_model=bool)
async def async_delete_student(student_id: int):
    student = await Student.aget(id=student_id)
    if student is None:
        raise HTTPException(404, "Student not found")
    return await student.adelete()
