import json
from datetime import datetime, timezone
from http import HTTPStatus

import arrow
from django.db.models import Count, Q, F, Case, When, Value

from canvas_sdk.effects import Effect
from canvas_sdk.effects.claim import ClaimEffect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.effects.simple_api import Response, JSONResponse
from canvas_sdk.handlers.application import Application
from canvas_sdk.handlers.simple_api import StaffSessionAuthMixin, SimpleAPI, api
from canvas_sdk.templates import render_to_string
from canvas_sdk.v1.data import Note, Command, Referral, ImagingOrder, Staff
from canvas_sdk.v1.data.claim import Claim, ClaimLabel, ClaimQueue
from canvas_sdk.v1.data.coverage import Coverage, Transactor
from canvas_sdk.v1.data.line_item_transaction import NewLineItemAdjustment
from canvas_sdk.v1.data.note import NoteStates, NoteTypeCategories, NoteType
from canvas_sdk.v1.data.posting import BasePosting, CoveragePosting
from canvas_sdk.v1.data.task import TaskLabel, TaskStatus


class MyApplication(Application):
    """An embeddable application that can be registered to Canvas."""

    def on_open(self) -> Effect:
        """Handle the on_open event."""
        return LaunchModalEffect(
            content=render_to_string("templates/encounter_list.html"),
            target=LaunchModalEffect.TargetType.PAGE,
        ).apply()


class RevenueCycleApi(StaffSessionAuthMixin, SimpleAPI):
    """API for Revenue Cycle Worklist functionality."""

    @api.get("/encounters")
    def get_encounters(self) -> list[Response | Effect]:
        """Get list of open encounters with pagination."""
        provider_ids = self.request.query_params.get("provider_ids")
        location_ids = self.request.query_params.get("location_ids")
        billable_only = self.request.query_params.get("billable_only") == "true"
        note_type_names = self.request.query_params.get("note_type_names")
        claim_queue_names = self.request.query_params.get("claim_queue_names")
        has_uncommitted_commands = self.request.query_params.get("has_uncommitted_commands") == "true"
        has_delegated_orders = self.request.query_params.get("has_delegated_orders") == "true"
        patient_search = self.request.query_params.get("patient_search")
        dos_start = self.request.query_params.get("dos_start")
        dos_end = self.request.query_params.get("dos_end")
        insurance_names = self.request.query_params.get("insurance_names")
        tag_ids = self.request.query_params.get("tag_ids")

        # Pagination parameters
        page = int(self.request.query_params.get("page", 1))
        page_size = int(self.request.query_params.get("page_size", 25))

        # Sorting parameters
        sort_by = self.request.query_params.get("sort_by", "dos")
        sort_direction = self.request.query_params.get("sort_direction", "asc")

        note_queryset = Note.objects.exclude(current_state__state__in=(
            NoteStates.SIGNED,
            NoteStates.LOCKED,
            NoteStates.DELETED,
            NoteStates.DISCHARGED,
            NoteStates.SCHEDULING,
            NoteStates.BOOKED,
            NoteStates.CANCELLED,
            NoteStates.CONFIRM_IMPORT,
            NoteStates.REVERTED
        ))

        note_queryset = note_queryset.exclude(note_type_version__category__in=(NoteTypeCategories.MESSAGE,
                                                               NoteTypeCategories.LETTER,))

        if provider_ids:
            clean_ids = [pid.strip() for pid in provider_ids.split(',') if pid.strip()]
            if clean_ids:
                note_queryset = note_queryset.filter(provider__id__in=clean_ids)

        if location_ids:
            clean_location_ids = [lid.strip() for lid in location_ids.split(',') if lid.strip()]
            if clean_location_ids:
                note_queryset = note_queryset.filter(location__id__in=clean_location_ids)

        if note_type_names:
            clean_note_type_names = [name.strip() for name in note_type_names.split(',') if name.strip()]
            if clean_note_type_names:
                note_queryset = note_queryset.filter(note_type_version__name__in=clean_note_type_names)

        if claim_queue_names:
            clean_claim_queue_names = [cqn.strip() for cqn in claim_queue_names.split(',') if cqn.strip()]
            if clean_claim_queue_names:
                note_queryset = note_queryset.filter(claims__current_queue__name__in=clean_claim_queue_names)

        if insurance_names:
            clean_insurance_names = [name.strip() for name in insurance_names.split(',') if name.strip()]
            if clean_insurance_names:
                note_queryset = note_queryset.filter(
                    patient__coverages__issuer__name__in=clean_insurance_names,
                    patient__coverages__state="active",
                )

        if tag_ids:
            clean_tag_ids = [tid.strip() for tid in tag_ids.split(',') if tid.strip()]
            if clean_tag_ids:
                note_queryset = note_queryset.filter(
                    claims__claim_labels__label__id__in=clean_tag_ids
                )

        if patient_search:
            note_queryset = self._apply_patient_search(note_queryset, patient_search)

        if dos_start or dos_end:
            note_queryset = self._apply_dos_range_filter(note_queryset, dos_start, dos_end)

        # Add annotations
        note_queryset = note_queryset.annotate(
            staged_commands_count=Count(
                'commands',
                filter=Q(commands__state__in=('staged', 'in_review',))
            )
        )

        if has_uncommitted_commands:
            note_queryset = note_queryset.filter(staged_commands_count__gt=0)

        if billable_only:
            note_queryset = note_queryset.filter(note_type_version__is_billable=True)

        # Apply sorting and pagination
        if sort_by == "delegatedOrders" or has_delegated_orders:
            # For delegated orders, calculate count first, then sort
            paginated_notes, total_count, total_pages = self._sort_and_paginate_delegated_orders(
                note_queryset, sort_direction, page, page_size, has_delegated_orders
            )
        else:
            # For other columns, use normal database sorting
            paginated_notes, total_count, total_pages = self._sort_and_paginate_database(
                note_queryset, sort_by, sort_direction, page, page_size
            )

        # Convert queryset to encounter data
        encounters = []
        for note in paginated_notes:
            claim = note.get_claim()
            claim_queue = claim.current_queue.name if claim else None
            claim_id = str(claim.id) if claim else None
            claim_dbid = claim.dbid if claim else None

            # Calculate days in queue from claim modified date
            days_in_queue = None
            if claim and claim.modified:
                delta = datetime.now(timezone.utc) - claim.modified
                days_in_queue = delta.days

            # Get insurance payer name from patient's active coverages
            insurance = None
            if note.patient:
                primary_coverage = (
                    Coverage.objects.filter(patient=note.patient, state="active")
                    .order_by("coverage_rank")
                    .select_related("issuer")
                    .first()
                )
                if primary_coverage and primary_coverage.issuer:
                    insurance = primary_coverage.issuer.name

            # Get claim tags/labels
            tags = []
            if claim:
                for cl in claim.claim_labels.select_related("label").all():
                    tags.append({
                        "id": str(cl.label.id),
                        "name": cl.label.name,
                        "color": cl.label.color if cl.label.color else None,
                    })

            # Get balance data
            patient_balance = None
            insurance_balance = None
            if claim:
                patient_balance = float(claim.patient_balance) if claim.patient_balance else 0.0
                insurance_balance = float(claim.aggregate_coverage_balance) if claim.aggregate_coverage_balance else 0.0

            # Get latest remit
            latest_remit_date = None
            latest_remit_era = None
            if claim:
                latest_coverage_posting = (
                    CoveragePosting.objects.filter(
                        claim=claim,
                        entered_in_error__isnull=True,
                        remittance__isnull=False,
                    )
                    .select_related("remittance")
                    .order_by("-remittance__created")
                    .first()
                )
                if latest_coverage_posting and latest_coverage_posting.remittance:
                    remit = latest_coverage_posting.remittance
                    latest_remit_date = remit.created.isoformat() if remit.created else None
                    latest_remit_era = remit.era_id or None

            # Get adjustment codes
            adjustment_codes = []
            if claim:
                adjustments = NewLineItemAdjustment.objects.filter(
                    posting__claim=claim,
                    posting__entered_in_error__isnull=True,
                ).values("group", "code").distinct()[:10]
                for adj in adjustments:
                    if adj["group"] or adj["code"]:
                        adjustment_codes.append(f"{adj['group']}-{adj['code']}")

            delegated_commands = self._calculate_delegated_orders_count(note)

            try:
                note_title = note.note_type_version.name or "Untitled Note"
            except Exception:
                note_title = "Untitled Note"

            encounter_data = {
                "id": str(note.id),
                "dbid": note.dbid,
                "patient_name": (f"{note.patient.first_name} ({note.patient.nickname}) {note.patient.last_name}" if note.patient.nickname else f"{note.patient.first_name} {note.patient.last_name}" if note.patient else "Unknown Patient"),
                "patient_id": str(note.patient.id) if note.patient else None,
                "patient_dob": arrow.get(note.patient.birth_date).format(
                    "MMM DD, YYYY") if note.patient and note.patient.birth_date else "Unknown",
                "provider": note.provider.credentialed_name if note.provider else "Unknown Provider",
                "provider_id": str(note.provider.id) if note.provider else None,
                "note_title": note_title,
                "dos": arrow.get(note.datetime_of_service).format(
                    "MMM DD, YYYY") if note.datetime_of_service else "Unknown",
                "dos_iso": note.datetime_of_service.isoformat() if note.datetime_of_service else None,
                "billable": self._get_billable_status(note),
                "uncommitted_commands": note.staged_commands_count,
                "delegated_orders": delegated_commands,
                "claim_id": claim_id,
                "claim_dbid": claim_dbid,
                "claim_queue": claim_queue,
                "days_in_queue": days_in_queue,
                "insurance": insurance,
                "tags": tags,
                "patient_balance": patient_balance,
                "insurance_balance": insurance_balance,
                "latest_remit_date": latest_remit_date,
                "latest_remit_era": latest_remit_era,
                "adjustment_codes": adjustment_codes,
                "location": note.location.full_name if note.location else "Unknown Location",
                "location_id": str(note.location.id) if note.location else None,
                "created": note.created.isoformat() if note.created else None,
            }
            encounters.append(encounter_data)

        return [JSONResponse({
            "encounters": encounters,
            "pagination": {
                "current_page": page,
                "total_pages": total_pages,
                "total_count": total_count,
                "page_size": page_size,
                "has_previous": page > 1,
                "has_next": page < total_pages,
            }
        }, status_code=HTTPStatus.OK)]

    @api.get("/providers")
    def get_providers(self) -> list[Response | Effect]:
        """Get list of providers who have notes."""
        logged_in_staff = self.request.headers["canvas-logged-in-user-id"]

        providers = [{"id": s.id, "name": s.credentialed_name}
                     for s in
                     Staff.objects.filter(active=True).order_by("first_name", "last_name")]


        return [JSONResponse({
            "logged_in_staff_id": logged_in_staff,
            "providers": providers
        }, status_code=HTTPStatus.OK)]

    @api.get("/locations")
    def get_locations(self) -> list[Response | Effect]:
        """Get list of practice locations that have notes."""

        locations = [{"id": str(n.location.id), "name": n.location.full_name}
                     for n in
                     Note.objects.filter(current_state__state__in=(NoteStates.NEW, NoteStates.UNLOCKED))
                     .filter(location__isnull=False)
                     .order_by("location__full_name", "location__id")
                     .distinct("location__id", "location__full_name")]

        return [JSONResponse({
            "locations": locations
        }, status_code=HTTPStatus.OK)]

    @api.get("/note_types")
    def get_note_types(self) -> list[Response | Effect]:
        """Get list of note type names."""

        note_types = list(NoteType.objects.exclude(
            category__in=(
                NoteTypeCategories.MESSAGE,
                NoteTypeCategories.LETTER,
            )
        ).order_by("name").values("name").distinct())

        return [JSONResponse({
            "note_types": note_types
        }, status_code=HTTPStatus.OK)]

    @api.get("/claim_queues")
    def get_claim_queues(self) -> list[Response | Effect]:
        """Get list of claim queue names."""
        claim_queues = list(ClaimQueue.objects.values("name"))

        return [JSONResponse({
            "claim_queues": claim_queues
        }, status_code=HTTPStatus.OK)]

    @api.post("/move_claim_queue")
    def move_claim_queue(self) -> list[Response | Effect]:
        """Move a claim to a different queue."""
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse(
                {"error": "Invalid JSON body"},
                status_code=HTTPStatus.BAD_REQUEST,
            )]

        claim_id = body.get("claim_id")
        queue_name = body.get("queue_name")

        if not claim_id or not queue_name:
            return [JSONResponse(
                {"error": "claim_id and queue_name are required"},
                status_code=HTTPStatus.BAD_REQUEST,
            )]

        if not Claim.objects.filter(id=claim_id).exists():
            return [JSONResponse(
                {"error": "Claim not found"},
                status_code=HTTPStatus.NOT_FOUND,
            )]

        if not ClaimQueue.objects.filter(name=queue_name).exists():
            return [JSONResponse(
                {"error": "Queue not found"},
                status_code=HTTPStatus.NOT_FOUND,
            )]

        effect = ClaimEffect(claim_id=claim_id).move_to_queue(queue_name)

        return [
            effect,
            JSONResponse(
                {"success": True, "claim_id": claim_id, "queue_name": queue_name},
                status_code=HTTPStatus.OK,
            ),
        ]

    @api.get("/insurances")
    def get_insurances(self) -> list[Response | Effect]:
        """Get list of unique insurance payer names from active coverages."""
        payer_names = list(
            Transactor.objects.filter(
                coverages__state="active",
            )
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        return [JSONResponse({"insurances": payer_names}, status_code=HTTPStatus.OK)]

    @api.get("/tags")
    def get_tags(self) -> list[Response | Effect]:
        """Get list of task labels used for claims."""
        labels = list(
            TaskLabel.objects.filter(active=True, modules__contains=["claims"])
            .values("id", "name", "color")
            .order_by("name")
        )
        # Convert id to string
        for label in labels:
            label["id"] = str(label["id"])
        return [JSONResponse({"tags": labels}, status_code=HTTPStatus.OK)]

    # ── Reviewed claims (per-user) ──────────────────────────────────────

    def _get_staff_dbid(self) -> int:
        """Resolve the logged-in staff UUID to the integer dbid used by CustomModel FKs."""
        staff_uuid = self.request.headers["canvas-logged-in-user-id"]
        return Staff.objects.filter(id=staff_uuid).values_list("dbid", flat=True).first()

    @api.get("/reviewed_claims")
    def get_reviewed_claims(self) -> list[Response | Effect]:
        """Return all reviewed claim_ids for the logged-in user."""
        from revenue_cycle_worklist.models.custom_data import ReviewedClaim

        staff_dbid = self._get_staff_dbid()
        if staff_dbid is None:
            return [JSONResponse({"reviewed": {}}, status_code=HTTPStatus.OK)]

        rows = ReviewedClaim.objects.filter(staff_id=staff_dbid, reviewed=True)
        reviewed = {
            row.claim_id: row.reviewed_at.isoformat() if row.reviewed_at else None
            for row in rows
        }
        return [JSONResponse({"reviewed": reviewed}, status_code=HTTPStatus.OK)]

    @api.post("/reviewed_claims")
    def set_reviewed_claim(self) -> list[Response | Effect]:
        """Toggle the reviewed state of a single claim for the logged-in user."""
        from revenue_cycle_worklist.models.custom_data import ReviewedClaim

        staff_dbid = self._get_staff_dbid()
        if staff_dbid is None:
            return [JSONResponse({"error": "Staff not found"}, status_code=HTTPStatus.BAD_REQUEST)]

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON body"}, status_code=HTTPStatus.BAD_REQUEST)]

        claim_id = body.get("claim_id")
        reviewed = body.get("reviewed", True)

        if not claim_id:
            return [JSONResponse({"error": "claim_id is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        existing = ReviewedClaim.objects.filter(staff_id=staff_dbid, claim_id=claim_id).first()

        if reviewed:
            now = datetime.now(timezone.utc)
            if existing:
                existing.reviewed = True
                existing.reviewed_at = now
                existing.save()
            else:
                ReviewedClaim.objects.create(
                    staff_id=staff_dbid,
                    claim_id=claim_id,
                    reviewed=True,
                    reviewed_at=now,
                )
            return [JSONResponse({"success": True, "reviewed_at": now.isoformat()}, status_code=HTTPStatus.OK)]
        else:
            if existing:
                existing.reviewed = False
                existing.reviewed_at = None
                existing.save()
            return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    # ── Column view configs (per-user) ───────────────────────────────────

    @api.get("/column_views")
    def get_column_views(self) -> list[Response | Effect]:
        """Return all saved column views for the logged-in user."""
        from revenue_cycle_worklist.models.custom_data import ColumnViewConfig

        staff_dbid = self._get_staff_dbid()
        if staff_dbid is None:
            return [JSONResponse({"views": []}, status_code=HTTPStatus.OK)]

        rows = ColumnViewConfig.objects.filter(staff_id=staff_dbid).order_by("config_name")
        views = [
            {
                "id": str(row.id),
                "config_name": row.config_name,
                "columns": row.columns,
                "is_default": row.is_default,
            }
            for row in rows
        ]
        return [JSONResponse({"views": views}, status_code=HTTPStatus.OK)]

    @api.post("/column_views")
    def save_column_view(self) -> list[Response | Effect]:
        """Create or update a column view for the logged-in user."""
        from revenue_cycle_worklist.models.custom_data import ColumnViewConfig

        staff_dbid = self._get_staff_dbid()
        if staff_dbid is None:
            return [JSONResponse({"error": "Staff not found"}, status_code=HTTPStatus.BAD_REQUEST)]

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON body"}, status_code=HTTPStatus.BAD_REQUEST)]

        config_name = body.get("config_name", "Default")
        columns = body.get("columns", [])

        # Clear is_default on all views for this user, then set the new one
        ColumnViewConfig.objects.filter(staff_id=staff_dbid).update(is_default=False)

        existing = ColumnViewConfig.objects.filter(staff_id=staff_dbid, config_name=config_name).first()
        if existing:
            existing.columns = columns
            existing.is_default = True
            existing.save()
            view_id = str(existing.id)
        else:
            obj = ColumnViewConfig.objects.create(
                staff_id=staff_dbid,
                config_name=config_name,
                columns=columns,
                is_default=True,
            )
            view_id = str(obj.id)

        return [JSONResponse({"success": True, "id": view_id}, status_code=HTTPStatus.OK)]

    @api.delete("/column_views")
    def delete_column_view(self) -> list[Response | Effect]:
        """Delete a column view for the logged-in user."""
        from revenue_cycle_worklist.models.custom_data import ColumnViewConfig

        staff_dbid = self._get_staff_dbid()
        if staff_dbid is None:
            return [JSONResponse({"error": "Staff not found"}, status_code=HTTPStatus.BAD_REQUEST)]

        config_name = self.request.query_params.get("config_name")
        if not config_name:
            return [JSONResponse({"error": "config_name is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        deleted, _ = ColumnViewConfig.objects.filter(
            staff_id=staff_dbid, config_name=config_name
        ).delete()

        return [JSONResponse({"success": True, "deleted": deleted > 0}, status_code=HTTPStatus.OK)]

    @api.get("/export_csv")
    def export_csv(self) -> list[Response | Effect]:
        """Export all encounters matching current filters as CSV."""
        # Re-use the same filter logic but without pagination
        provider_ids = self.request.query_params.get("provider_ids")
        location_ids = self.request.query_params.get("location_ids")
        billable_only = self.request.query_params.get("billable_only") == "true"
        note_type_names = self.request.query_params.get("note_type_names")
        claim_queue_names = self.request.query_params.get("claim_queue_names")
        has_uncommitted_commands = self.request.query_params.get("has_uncommitted_commands") == "true"
        has_delegated_orders = self.request.query_params.get("has_delegated_orders") == "true"
        patient_search = self.request.query_params.get("patient_search")
        dos_start = self.request.query_params.get("dos_start")
        dos_end = self.request.query_params.get("dos_end")
        insurance_names = self.request.query_params.get("insurance_names")
        tag_ids = self.request.query_params.get("tag_ids")

        note_queryset = Note.objects.exclude(current_state__state__in=(
            NoteStates.SIGNED, NoteStates.LOCKED, NoteStates.DELETED,
            NoteStates.DISCHARGED, NoteStates.SCHEDULING, NoteStates.BOOKED,
            NoteStates.CANCELLED, NoteStates.CONFIRM_IMPORT, NoteStates.REVERTED,
        )).exclude(note_type_version__category__in=(
            NoteTypeCategories.MESSAGE, NoteTypeCategories.LETTER,
        ))

        if provider_ids:
            clean_ids = [pid.strip() for pid in provider_ids.split(',') if pid.strip()]
            if clean_ids:
                note_queryset = note_queryset.filter(provider__id__in=clean_ids)
        if location_ids:
            clean_ids = [lid.strip() for lid in location_ids.split(',') if lid.strip()]
            if clean_ids:
                note_queryset = note_queryset.filter(location__id__in=clean_ids)
        if note_type_names:
            clean_names = [n.strip() for n in note_type_names.split(',') if n.strip()]
            if clean_names:
                note_queryset = note_queryset.filter(note_type_version__name__in=clean_names)
        if claim_queue_names:
            clean_names = [n.strip() for n in claim_queue_names.split(',') if n.strip()]
            if clean_names:
                note_queryset = note_queryset.filter(claims__current_queue__name__in=clean_names)
        if insurance_names:
            clean_names = [n.strip() for n in insurance_names.split(',') if n.strip()]
            if clean_names:
                note_queryset = note_queryset.filter(
                    patient__coverages__issuer__name__in=clean_names,
                    patient__coverages__state="active",
                )
        if tag_ids:
            clean_ids = [tid.strip() for tid in tag_ids.split(',') if tid.strip()]
            if clean_ids:
                note_queryset = note_queryset.filter(claims__claim_labels__label__id__in=clean_ids)
        if patient_search:
            note_queryset = self._apply_patient_search(note_queryset, patient_search)
        if dos_start or dos_end:
            note_queryset = self._apply_dos_range_filter(note_queryset, dos_start, dos_end)
        if billable_only:
            note_queryset = note_queryset.filter(note_type_version__is_billable=True)

        note_queryset = note_queryset.annotate(
            staged_commands_count=Count(
                'commands', filter=Q(commands__state__in=('staged', 'in_review'))
            )
        )
        if has_uncommitted_commands:
            note_queryset = note_queryset.filter(staged_commands_count__gt=0)

        note_queryset = note_queryset.order_by("datetime_of_service")

        def csv_escape(val: str) -> str:
            """Escape a value for CSV output."""
            s = str(val) if val is not None else ""
            if '"' in s or ',' in s or '\n' in s:
                return '"' + s.replace('"', '""') + '"'
            return s

        rows = []
        rows.append(",".join([
            "Patient Name", "DOB", "Provider", "Location", "Insurance",
            "Note Type", "Date of Service", "Billable", "Uncommitted Commands",
            "Delegated Orders", "Claim Queue", "Days in Queue",
            "Patient Balance", "Insurance Balance", "Latest Remit",
            "Adjustment Codes", "Tags",
        ]))

        for note in note_queryset:
            claim = note.get_claim()
            claim_queue = claim.current_queue.name if claim else ""
            days_in_queue = ""
            if claim and claim.modified:
                days_in_queue = str((datetime.now(timezone.utc) - claim.modified).days)

            insurance = ""
            if note.patient:
                primary_cov = (
                    Coverage.objects.filter(patient=note.patient, state="active")
                    .order_by("coverage_rank").select_related("issuer").first()
                )
                if primary_cov and primary_cov.issuer:
                    insurance = primary_cov.issuer.name

            tag_names = []
            if claim:
                for cl in claim.claim_labels.select_related("label").all():
                    tag_names.append(cl.label.name)

            csv_patient_bal = ""
            csv_insurance_bal = ""
            csv_latest_remit = ""
            csv_adj_codes = ""
            if claim:
                csv_patient_bal = str(float(claim.patient_balance)) if claim.patient_balance else "0.00"
                csv_insurance_bal = str(float(claim.aggregate_coverage_balance)) if claim.aggregate_coverage_balance else "0.00"
                cp = (
                    CoveragePosting.objects.filter(
                        claim=claim, entered_in_error__isnull=True, remittance__isnull=False,
                    ).select_related("remittance").order_by("-remittance__created").first()
                )
                if cp and cp.remittance and cp.remittance.created:
                    csv_latest_remit = arrow.get(cp.remittance.created).format("YYYY-MM-DD")
                adjs = NewLineItemAdjustment.objects.filter(
                    posting__claim=claim, posting__entered_in_error__isnull=True,
                ).values("group", "code").distinct()[:10]
                csv_adj_codes = "; ".join(
                    f"{a['group']}-{a['code']}" for a in adjs if a["group"] or a["code"]
                )

            patient_name = "Unknown Patient"
            if note.patient:
                patient_name = f"{note.patient.first_name} {note.patient.last_name}"

            try:
                note_title = note.note_type_version.name or "Untitled Note"
            except Exception:
                note_title = "Untitled Note"

            delegated_count = self._calculate_delegated_orders_count(note)

            row = [
                csv_escape(patient_name),
                csv_escape(arrow.get(note.patient.birth_date).format("YYYY-MM-DD") if note.patient and note.patient.birth_date else ""),
                csv_escape(note.provider.credentialed_name if note.provider else ""),
                csv_escape(note.location.full_name if note.location else ""),
                csv_escape(insurance),
                csv_escape(note_title),
                csv_escape(arrow.get(note.datetime_of_service).format("YYYY-MM-DD") if note.datetime_of_service else ""),
                csv_escape("Yes" if self._get_billable_status(note) else "No"),
                csv_escape(str(note.staged_commands_count)),
                csv_escape(str(delegated_count)),
                csv_escape(claim_queue),
                csv_escape(days_in_queue),
                csv_escape(csv_patient_bal),
                csv_escape(csv_insurance_bal),
                csv_escape(csv_latest_remit),
                csv_escape(csv_adj_codes),
                csv_escape("; ".join(tag_names)),
            ]
            rows.append(",".join(row))

        csv_content = "\n".join(rows)
        return [Response(
            status_code=HTTPStatus.OK,
            content=csv_content.encode("utf-8"),
            headers={
                "Content-Type": "text/csv",
                "Content-Disposition": f"attachment; filename=revenue_cycle_worklist_{arrow.now().format('YYYY-MM-DD')}.csv",
            },
        )]

    def _get_sort_fields(self, sort_by: str) -> list[str]:
        """Map frontend sort field names to database field names, returning a list of fields."""
        sort_mapping = {
            "patientName": ["patient__first_name", "patient__last_name"],
            "provider": ["provider__first_name", "provider__last_name"],
            "location": ["location__full_name"],
            "noteTitle": ["note_type_version__name"],
            "dos": ["datetime_of_service"],
            "billable": ["note_type_version__is_billable"],  # Will be handled specially in sorting logic
            "uncommittedCommands": ["staged_commands_count"],
            "delegatedOrders": ["created"],  # Handled specially in sorting logic
            "claimQueue": ["claims__current_queue__name"],
            "daysInQueue": ["claims__modified"],
            "insurance": ["patient__coverages__issuer__name"],
            "created": ["created"]
        }
        return sort_mapping.get(sort_by, ["created"])

    def _sort_and_paginate_delegated_orders(self, note_queryset, sort_direction, page, page_size, has_delegated_orders):
        """Sort and paginate for delegated orders using Python calculation."""
        # Get all notes without pagination to calculate delegated orders
        all_notes = list(note_queryset)
        
        # Calculate delegated orders count for each note
        notes_with_delegated_count = []
        for note in all_notes:
            delegated_commands = self._calculate_delegated_orders_count(note)
            if not has_delegated_orders and delegated_commands > 0:
                continue
            if has_delegated_orders and delegated_commands == 0:
                continue
            notes_with_delegated_count.append((note, delegated_commands))
        
        # Sort by delegated orders count
        notes_with_delegated_count.sort(
            key=lambda x: x[1], 
            reverse=(sort_direction == "desc")
        )
        
        # Extract the sorted notes
        sorted_notes = [note for note, _ in notes_with_delegated_count]
        
        # Apply pagination
        return self._apply_pagination(sorted_notes, page, page_size)

    def _apply_patient_search(self, note_queryset, patient_search: str):
        """Apply patient name search across first, last, and nickname fields."""
        normalized_search = patient_search.strip()
        if not normalized_search:
            return note_queryset

        search_terms = normalized_search.split()

        search_filter = (
            Q(patient__first_name__icontains=normalized_search)
            | Q(patient__last_name__icontains=normalized_search)
            | Q(patient__nickname__icontains=normalized_search)
        )

        if len(search_terms) >= 2:
            first_term = search_terms[0]
            last_term = search_terms[-1]
            search_filter |= (
                (Q(patient__first_name__icontains=first_term) | Q(patient__nickname__icontains=first_term))
                & Q(patient__last_name__icontains=last_term)
            )
        elif len(search_terms) == 1:
            single_term = search_terms[0]
            search_filter |= Q(patient__first_name__icontains=single_term)
            search_filter |= Q(patient__last_name__icontains=single_term)
            search_filter |= Q(patient__nickname__icontains=single_term)

        return note_queryset.filter(search_filter)

    def _apply_dos_range_filter(self, note_queryset, dos_start: str | None, dos_end: str | None):
        """Filter notes by date of service range."""
        try:
            parsed_start = arrow.get(dos_start).date() if dos_start else None
        except (arrow.parser.ParserError, TypeError, ValueError):
            parsed_start = None

        try:
            parsed_end = arrow.get(dos_end).date() if dos_end else None
        except (arrow.parser.ParserError, TypeError, ValueError):
            parsed_end = None

        if parsed_start:
            note_queryset = note_queryset.filter(datetime_of_service__date__gte=parsed_start)

        if parsed_end:
            note_queryset = note_queryset.filter(datetime_of_service__date__lte=parsed_end)

        return note_queryset

    def _sort_and_paginate_database(self, note_queryset, sort_by, sort_direction, page, page_size):
        """Sort and paginate using database sorting."""
        # Apply database sorting
        sort_fields = self._get_sort_fields(sort_by)
        
        # Handle billable field specially to treat None as False
        if sort_by == "billable":
            # Use Case/When to treat None as False for sorting
            billable_sort = Case(
                When(note_type_version__is_billable__isnull=True, then=Value(False)),
                default='note_type_version__is_billable'
            )
            if sort_direction == "desc":
                sort_fields = [billable_sort.desc(), "created"]
            else:
                sort_fields = [billable_sort.asc(), "created"]
        else:
            # Handle other fields normally
            if sort_direction == "desc":
                sort_fields = [f"-{field}" for field in sort_fields]
        
        note_queryset = note_queryset.order_by(*sort_fields)
        
        # Get total count
        total_count = note_queryset.count()
        
        # Apply pagination using the helper method
        paginated_notes, _, _ = self._apply_pagination(list(note_queryset), page, page_size)
        
        return paginated_notes, total_count, (total_count + page_size - 1) // page_size

    def _get_billable_status(self, note):
        """Safely get the billable status of a note, handling cases where note_type_version doesn't exist."""
        try:
            return note.note_type_version.is_billable
        except (AttributeError, Exception):
            return False

    def _calculate_delegated_orders_count(self, note):
        """Calculate the delegated orders count for a note using the exact same logic as display."""
        delegated_commands = 0
        # Fetch commands that can be delegated related to the note
        delegatable_commands = Command.objects.filter(note=note, schema_key__in=("imagingOrder", "refer",))
        for command in delegatable_commands:
            # Get the anchor object for the command
            anchor_object = command.anchor_object
            if not anchor_object:
                continue

            should_increase = False
            # If the command is delegated increment the count
            if isinstance(anchor_object, Referral) and anchor_object.forwarded:
                should_increase = True
            elif isinstance(anchor_object, ImagingOrder) and anchor_object.delegated:
                should_increase = True

            if should_increase and anchor_object.get_task_objects().filter(status=TaskStatus.OPEN).exists():
                delegated_commands = delegated_commands + 1
        
        return delegated_commands

    def _apply_pagination(self, items, page, page_size):
        """Apply pagination to a list of items."""
        total_count = len(items)
        total_pages = (total_count + page_size - 1) // page_size
        
        # Validate page number
        if page < 1:
            page = 1
        elif page > total_pages and total_pages > 0:
            page = total_pages
        
        # Calculate offset and apply slicing
        offset = (page - 1) * page_size
        paginated_items = items[offset:offset + page_size]
        
        return paginated_items, total_count, total_pages
