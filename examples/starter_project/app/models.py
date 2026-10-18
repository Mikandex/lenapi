"""Define database models here."""

from lenapi import model


class User(model.Model):
    username = model.StringField(max_length=120, unique=True)
    email = model.StringField(max_length=254, unique=True, nullable=True)
    first_name = model.StringField(max_length=150, default="")
    last_name = model.StringField(max_length=150, default="")
    password = model.StringField(max_length=255)
    api_key = model.StringField(max_length=64, unique=True, nullable=True)
