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
    max_concurrent: Any = IntegerField(default=1)  # 1 = no double-booking, 0 = unlimited
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


class StaffConfig(CustomModel):
    """Per-staff scheduling configuration (schedulable flag, default room)."""

    staff_id: Any = CharField(max_length=256)  # Canvas Staff UUID
    staff_name: Any = CharField(max_length=256)
    schedulable: Any = BooleanField(default=True)
    default_room_key: Any = CharField(max_length=50, blank=True, default="")
    active: Any = BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["staff_id"], name="uq_fp_staff_config"),
        ]

    def __str__(self) -> str:
        return f"{self.staff_name} ({'schedulable' if self.schedulable else 'hidden'})"


class SonosSpeaker(CustomModel):
    """Maps a physical Sonos speaker to a room on the floor plan."""

    room_key: Any = CharField(max_length=50, db_index=True)
    player_id: Any = CharField(max_length=256)  # Sonos player ID
    group_id: Any = CharField(max_length=256, blank=True, default="")  # Sonos group ID
    player_name: Any = CharField(max_length=256)  # human-readable name from Sonos
    household_id: Any = CharField(max_length=256)
    active: Any = BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["room_key"], name="uq_fp_sonos_room_key"),
        ]

    def __str__(self) -> str:
        return f"{self.player_name} → {self.room_key}"


class AudioPreset(CustomModel):
    """Maps an appointment type or resource to a Sonos audio preset."""

    key: Any = CharField(max_length=100)
    name: Any = CharField(max_length=256)
    match_type: Any = CharField(max_length=50)  # appointment_type, resource_key, room_type, default
    match_value: Any = CharField(max_length=256, blank=True, default="")
    sonos_favorite_id: Any = CharField(max_length=256, blank=True, default="")
    sonos_favorite_name: Any = CharField(max_length=256, blank=True, default="")
    volume: Any = IntegerField(default=25)  # 0-100
    priority: Any = IntegerField(default=0)  # higher wins on multiple matches
    active: Any = BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["key"], name="uq_fp_audio_preset_key"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.match_type}={self.match_value})"


class SonosPlaybackLog(CustomModel):
    """Append-only audit trail of Sonos playback actions."""

    assignment_id: Any = IntegerField(default=0, db_index=True)
    room_key: Any = CharField(max_length=50)
    player_id: Any = CharField(max_length=256)
    preset_key: Any = CharField(max_length=100, blank=True, default="")
    action: Any = CharField(max_length=50)  # play, pause, stop, volume_change, error
    volume: Any = IntegerField(default=0)
    triggered_by: Any = CharField(max_length=50)  # auto_assign, auto_start, auto_complete, manual, timer
    error_message: Any = TextField(blank=True, default="")
    created_at: Any = DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.room_key}: {self.action} ({self.triggered_by})"
