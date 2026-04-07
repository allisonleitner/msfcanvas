from __future__ import annotations

from django.db.models import (
    DO_NOTHING,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    JSONField,
    UniqueConstraint,
)

from canvas_sdk.v1.data import Staff
from canvas_sdk.v1.data.base import CustomModel


class ReviewedClaim(CustomModel):
    """Track per-user reviewed state for claims."""

    staff = ForeignKey(
        Staff,
        to_field="dbid",
        on_delete=DO_NOTHING,
        related_name="rcw_reviewed_claims",
    )
    claim_id = CharField(max_length=256)
    reviewed = BooleanField(default=True)
    reviewed_at = DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["staff", "claim_id"],
                name="uq_rcw_staff_claim",
            ),
        ]


class ColumnViewConfig(CustomModel):
    """Store per-user saved column view configurations."""

    staff = ForeignKey(
        Staff,
        to_field="dbid",
        on_delete=DO_NOTHING,
        related_name="rcw_column_views",
    )
    config_name = CharField(max_length=100, default="Default")
    columns = JSONField(default=list)  # [{name, visible, order}, ...]
    is_default = BooleanField(default=False)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["staff", "config_name"],
                name="uq_rcw_staff_view",
            ),
        ]
