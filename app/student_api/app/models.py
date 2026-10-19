"""Application database models."""

from lenapi import User, model


class Classroom(model.Model):
    name = model.StringField(max_length=120)


class Student(model.Model):
    person_name = model.StringField(max_length=120, unique=True)

    course = model.StringField(max_length=120)

    classroom_id = model.ForeignKeyField(
        "classroom.id",
        nullable=True,
        model=model.null,
        constraint_name="fk_student_classroom_id_classroom",
    )


class Photo(model.Model):
    image = model.ImageField(upload_to="photos")
