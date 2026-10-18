"""Model base class and field declarations for lenApi projects.

Import this module with ``from lenapi import model`` and declare fields as
``model.StringField(...)``, ``model.ForeignKeyField(...)``, and so on.
"""

from .models import (
    AutoField,
    Avg,
    BigIntField,
    BigIntegerField,
    BooleanField,
    CASCADE,
    Count,
    DateTimeField,
    DecimalField,
    EmailField,
    Email_field,
    FloatField,
    ForeignKeyField,
    ImageField,
    IPField,
    IP_field,
    IntegerField,
    JSONField,
    ManyToManyField,
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
    TExtfield,
    UUIDField,
    cascade,
    null,
    protected,
)

# Short alias for JSON column declarations: ``model.Json()``.
Json = JSONField

__all__ = [
    "Model", "AutoField", "IntegerField", "BigIntField", "BigIntegerField", "DecimalField", "StringField", "EmailField", "IPField", "PhoneField", "TextField",
    "Email_field", "IP_field", "Phone_field", "TExtfield",
    "BooleanField", "FloatField", "DateTimeField", "JSONField", "Json", "UUIDField", "ImageField",
    "Count", "Sum", "Min", "Max", "Avg",
    "ForeignKeyField", "ManyToManyField", "CASCADE", "SET_NULL", "PROTECT",
    "cascade", "null", "protected",
]
