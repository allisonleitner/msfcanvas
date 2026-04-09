from typing import Any

from django.db.models import (
    DO_NOTHING,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    IntegerField,
    JSONField,
    TextField,
    UniqueConstraint,
)
from canvas_sdk.v1.data import Staff
from canvas_sdk.v1.data.base import CustomModel


class Room(CustomModel):
    """A physical room in the office floor plan."""

    key: Any = CharField(max_length=50)  # matches data-room attr in SVG
    name: Any = CharField(max_length=100)
    room_type: Any = CharField(max_length=50)  # exam, wellness, treatment, lab, conference, office, utility
    number: Any = CharField(max_length=20, blank=True, default="")
    bookable: Any = BooleanField(default=True)
    active: Any = BooleanField(default=True)
    equipment: Any = JSONField(default=list)  # legacy display list
    practice_location_id: Any = CharField(max_length=256, blank=True, default="")  # Canvas PracticeLocation UUID

    class Meta:
        constraints = [
            UniqueConstraint(fields=["key"], name="uq_fp_room_key"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"


class Resource(CustomModel):
    """A schedulable resource / piece of equipment (e.g., hyperbaric chamber, IV station)."""

    key: Any = CharField(max_length=50)
    name: Any = CharField(max_length=100)
    resource_type: Any = CharField(max_length=50)  # equipment, service, device
    room_key: Any = CharField(max_length=50, blank=True, default="")  # home room
    description: Any = CharField(max_length=500, blank=True, default="")
    portable: Any = BooleanField(default=False)
    active: Any = BooleanField(default=True)
    price_cents: Any = IntegerField(default=0)  # session price in cents ($45.00 = 4500)
    credit_amount: Any = IntegerField(default=0)  # credits per session
    default_duration_minutes: Any = IntegerField(default=30)
    practice_location_id: Any = CharField(max_length=256, blank=True, default="")
    practitioner_id: Any = CharField(max_length=256, blank=True, default="")  # Canvas Practitioner UUID

    class Meta:
        constraints = [
            UniqueConstraint(fields=["key"], name="uq_fp_resource_key"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"


class RoomAssignment(CustomModel):
    """Links an appointment/patient to a room for a time window."""

    room_key: Any = CharField(max_length=50, db_index=True)
    appointment_id: Any = CharField(max_length=256, blank=True, default="")
    patient_id: Any = CharField(max_length=256, blank=True, default="")
    patient_name: Any = CharField(max_length=256, blank=True, default="")
    appointment_type: Any = CharField(max_length=256, blank=True, default="")
    provider_name: Any = CharField(max_length=256, blank=True, default="")
    start_time: Any = DateTimeField()
    end_time: Any = DateTimeField()
    status: Any = CharField(max_length=50, default="scheduled")
    notes: Any = TextField(blank=True, default="")
    resource_keys: Any = JSONField(default=list)  # ["hyperbaric-1", "iv-station-1"]
    assigned_by: Any = ForeignKey(
        Staff,
        to_field="dbid",
        on_delete=DO_NOTHING,
        related_name="%(app_label)s_fp_room_assignments",
        null=True,
        blank=True,
    )

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["room_key", "appointment_id"],
                name="uq_fp_room_appointment",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.room_key}: {self.patient_name} ({self.status})"
