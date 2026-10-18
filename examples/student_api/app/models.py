"""Application database models."""

from lenapi import model


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


class User(model.Model):
    username = model.StringField(max_length=120, unique=True)
    email = model.StringField(max_length=254, unique=True, nullable=True)
    first_name = model.StringField(max_length=150, default="")
    last_name = model.StringField(max_length=150, default="")
    password = model.StringField(max_length=255)
    api_key = model.StringField(max_length=64, unique=True, nullable=True)


class Photo(model.Model):
    image = model.ImageField(upload_to="photos")
