"""Minimal app for trying lenApi routes and Pydantic request schemas."""

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lenapi import Body, HTTPException, LENAPI, Request, UploadFile
from app.models import Classroom, Photo, Student
from app.schemas import (
    ClassroomCreate,
    ClassroomRead,
    StudentBatchCreate,
    StudentBatchDelete,
    StudentBatchDeleteResult,
    StudentBatchUpdate,
    StudentCreate,
    StudentRead,
    StudentSummary,
)
import settings

app = LENAPI()


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


# Async CRUD example. These routes use the AsyncSession configured by LENAPI.
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
