"""Model base class and field declarations for lenApi projects.

Import this module with ``from lenapi import model`` and declare fields as
``model.StringField(...)``, ``model.ForeignKeyField(...)``, and so on.
"""

from sqlalchemy import UniqueConstraint

from .models import (
    AutoField,
    Avg,
    AdminUser,
    BigIntField,
    BigIntegerField,
    BooleanField,
    CASCADE,
    Count,
    DateField,
    DateTimeField,
    DecimalField,
    DurationField,
    EmailField,
    Email_field,
    FloatField,
    ForeignKeyField,
    FileField,
    FileImageField,
    ImageField,
    IPField,
    IP_field,
    IntegerField,
    PositiveBigIntegerField,
    PositiveIntegerField,
    JSONField,
    ManyToManyField,
    OneToOneField,
    Max,
    Min,
    Model,
    PhoneField,
    Phone_field,
    PROTECT,
    SET_NULL,
    StringField,
    Sum,
    TextField,
    TimeField,
    TExtfield,
    UUIDField,
    URLField,
    cascade,
    null,
    protected,
)

# Short alias for JSON column declarations: ``model.Json()``.
Json = JSONField

__all__ = [
    "Model", "AdminUser", "AutoField", "IntegerField", "PositiveIntegerField", "BigIntField", "BigIntegerField", "PositiveBigIntegerField", "DecimalField", "StringField", "EmailField", "IPField", "PhoneField", "TextField",
    "UniqueConstraint",
    "Email_field", "IP_field", "Phone_field", "TExtfield",
    "BooleanField", "FloatField", "DateTimeField", "DateField", "TimeField", "DurationField", "JSONField", "Json", "UUIDField", "ImageField", "FileField", "FileImageField", "URLField",
    "Count", "Sum", "Min", "Max", "Avg",
    "ForeignKeyField", "OneToOneField", "ManyToManyField", "CASCADE", "SET_NULL", "PROTECT",
    "cascade", "null", "protected",
]
