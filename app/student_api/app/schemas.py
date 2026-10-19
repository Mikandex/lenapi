"""Application request and response schemas."""

from pydantic import BaseModel, ConfigDict, Field

from lenapi.authentication import UserPublic, UserRegistration


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
