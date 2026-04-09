import json
from datetime import datetime, time, timedelta, timezone
from http import HTTPStatus

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.effects.simple_api import JSONResponse, Response
from canvas_sdk.handlers.application import Application
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string
from canvas_sdk.v1.data import Staff
from canvas_sdk.v1.data.appointment import Appointment


class MyApplication(Application):
    """Elle Medicine Floor Plan Viewer – interactive room status dashboard."""

    def on_open(self) -> Effect:
        return LaunchModalEffect(
            content=render_to_string("templates/floor_plan.html"),
            target=LaunchModalEffect.TargetType.PAGE,
        ).apply()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

ELLE_MEDICINE_ROOMS = [
    {"key": "studio-two", "name": "Studio Two", "room_type": "treatment", "number": "113", "bookable": True, "equipment": ["IV Station"]},
    {"key": "elle-one", "name": "Elle One", "room_type": "exam", "number": "105", "bookable": True, "equipment": []},
    {"key": "elle-two", "name": "Elle Two", "room_type": "exam", "number": "106", "bookable": True, "equipment": []},
    {"key": "restore", "name": "Restore", "room_type": "wellness", "number": "109", "bookable": True, "equipment": ["Acupuncture Table"]},
    {"key": "thrive", "name": "Thrive", "room_type": "exam", "number": "107", "bookable": True, "equipment": []},
    {"key": "studio-one", "name": "Studio One", "room_type": "treatment", "number": "104", "bookable": True, "equipment": ["IV Station", "Infusion Chair"]},
    {"key": "collective", "name": "The Collective", "room_type": "conference", "number": "105", "bookable": True, "equipment": []},
    {"key": "float", "name": "Float", "room_type": "wellness", "number": "108", "bookable": True, "equipment": ["Hyperbaric Chamber"]},
    {"key": "lab", "name": "Lab", "room_type": "lab", "number": "115", "bookable": True, "equipment": ["Centrifuge", "Phlebotomy Station"]},
    {"key": "hub", "name": "The Hub", "room_type": "utility", "number": "", "bookable": False, "equipment": []},
    {"key": "cafe", "name": "The Cafe", "room_type": "utility", "number": "114", "bookable": False, "equipment": []},
    {"key": "it", "name": "IT", "room_type": "utility", "number": "116", "bookable": False, "equipment": []},
    {"key": "hallway", "name": "Hallway", "room_type": "utility", "number": "", "bookable": False, "equipment": []},
    {"key": "entry", "name": "Entry", "room_type": "utility", "number": "100", "bookable": False, "equipment": []},
    {"key": "lounge", "name": "Lounge", "room_type": "utility", "number": "102", "bookable": False, "equipment": []},
    {"key": "physician", "name": "Physician's Suite", "room_type": "office", "number": "111", "bookable": False, "equipment": []},
    {"key": "workroom", "name": "Workroom", "room_type": "utility", "number": "112", "bookable": False, "equipment": []},
]


class FloorPlanApi(StaffSessionAuthMixin, SimpleAPI):
    """REST API for rooms, assignments, and appointments."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_staff_dbid(self) -> int | None:
        staff_uuid = self.request.headers.get("canvas-logged-in-user-id", "")
        if not staff_uuid:
            return None
        result = Staff.objects.filter(id=staff_uuid).values_list("dbid", flat=True).first()
        return int(result) if result is not None else None

    @staticmethod
    def _today_range() -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        return start, end

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------

    @api.get("/rooms")
    def get_rooms(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Room

        rooms = Room.objects.filter(active=True).order_by("key")
        return [JSONResponse({
            "rooms": [
                {
                    "id": r.pk,
                    "key": r.key,
                    "name": r.name,
                    "room_type": r.room_type,
                    "number": r.number,
                    "bookable": r.bookable,
                    "equipment": r.equipment or [],
                }
                for r in rooms
            ]
        }, status_code=HTTPStatus.OK)]

    @api.post("/rooms")
    def create_room(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Room

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        required = ["key", "name", "room_type"]
        for field in required:
            if not body.get(field):
                return [JSONResponse({"error": f"{field} is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        if Room.objects.filter(key=body["key"]).exists():
            return [JSONResponse({"error": "Room key already exists"}, status_code=HTTPStatus.CONFLICT)]

        room = Room.objects.create(
            key=body["key"],
            name=body["name"],
            room_type=body["room_type"],
            number=body.get("number", ""),
            bookable=body.get("bookable", True),
            equipment=body.get("equipment", []),
        )
        return [JSONResponse({"success": True, "id": room.pk}, status_code=HTTPStatus.CREATED)]

    @api.put("/rooms/<key>")
    def update_room(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Room

        key = self.request.path_params["key"]
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        room = Room.objects.filter(key=key).first()
        if not room:
            return [JSONResponse({"error": "Room not found"}, status_code=HTTPStatus.NOT_FOUND)]

        for field in ("name", "room_type", "number", "bookable", "equipment"):
            if field in body:
                setattr(room, field, body[field])
        room.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.post("/rooms/seed")
    def seed_rooms(self) -> list[Response | Effect]:
        """Idempotent seed of Elle Medicine rooms."""
        from floor_plan_viewer.models.custom_data import Room

        created = 0
        for r in ELLE_MEDICINE_ROOMS:
            _, was_created = Room.objects.get_or_create(
                key=r["key"],
                defaults={
                    "name": r["name"],
                    "room_type": r["room_type"],
                    "number": r["number"],
                    "bookable": r["bookable"],
                    "equipment": r["equipment"],
                },
            )
            if was_created:
                created += 1
        return [JSONResponse({"success": True, "created": created}, status_code=HTTPStatus.OK)]

    # ------------------------------------------------------------------
    # Assignments
    # ------------------------------------------------------------------

    @api.get("/assignments")
    def get_assignments(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import RoomAssignment

        date_str = self.request.query_params.get("date", "")
        if date_str:
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return [JSONResponse({"error": "Invalid date format, use YYYY-MM-DD"}, status_code=HTTPStatus.BAD_REQUEST)]
            start = datetime.combine(day.date(), time.min, tzinfo=timezone.utc)
            end = start + timedelta(days=1)
        else:
            start, end = self._today_range()

        assignments = RoomAssignment.objects.filter(
            start_time__lt=end,
            end_time__gt=start,
        ).exclude(status="cancelled").order_by("start_time")

        return [JSONResponse({
            "assignments": [
                {
                    "id": a.pk,
                    "room_key": a.room_key,
                    "appointment_id": a.appointment_id,
                    "patient_id": a.patient_id,
                    "patient_name": a.patient_name,
                    "appointment_type": a.appointment_type,
                    "provider_name": a.provider_name,
                    "start_time": a.start_time.isoformat(),
                    "end_time": a.end_time.isoformat(),
                    "status": a.status,
                    "notes": a.notes,
                }
                for a in assignments
            ]
        }, status_code=HTTPStatus.OK)]

    @api.post("/assignments")
    def create_assignment(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import RoomAssignment

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        required = ["room_key", "start_time", "end_time"]
        for field in required:
            if not body.get(field):
                return [JSONResponse({"error": f"{field} is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        staff_dbid = self._get_staff_dbid()

        assignment = RoomAssignment.objects.create(
            room_key=body["room_key"],
            appointment_id=body.get("appointment_id", ""),
            patient_id=body.get("patient_id", ""),
            patient_name=body.get("patient_name", ""),
            appointment_type=body.get("appointment_type", ""),
            provider_name=body.get("provider_name", ""),
            start_time=body["start_time"],
            end_time=body["end_time"],
            status=body.get("status", "scheduled"),
            notes=body.get("notes", ""),
            assigned_by_id=staff_dbid,
        )
        return [JSONResponse({"success": True, "id": assignment.pk}, status_code=HTTPStatus.CREATED)]

    @api.put("/assignments/<id>")
    def update_assignment(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import RoomAssignment

        pk = self.request.path_params["id"]
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        assignment = RoomAssignment.objects.filter(pk=pk).first()
        if not assignment:
            return [JSONResponse({"error": "Assignment not found"}, status_code=HTTPStatus.NOT_FOUND)]

        for field in ("room_key", "status", "notes", "start_time", "end_time", "patient_name", "appointment_type", "provider_name"):
            if field in body:
                setattr(assignment, field, body[field])
        assignment.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.delete("/assignments/<id>")
    def delete_assignment(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import RoomAssignment

        pk = self.request.path_params["id"]
        deleted, _ = RoomAssignment.objects.filter(pk=pk).delete()
        return [JSONResponse({"success": True, "deleted": deleted > 0}, status_code=HTTPStatus.OK)]

    # ------------------------------------------------------------------
    # Appointments (read from Canvas)
    # ------------------------------------------------------------------

    @api.get("/appointments/today")
    def get_todays_appointments(self) -> list[Response | Effect]:
        start, end = self._today_range()

        appointments = Appointment.objects.filter(
            start_time__gte=start,
            start_time__lt=end,
        ).select_related("patient", "provider").order_by("start_time")

        result = []
        for appt in appointments:
            patient_name = ""
            patient_id = ""
            if appt.patient:
                patient_name = f"{appt.patient.first_name} {appt.patient.last_name}".strip()
                patient_id = str(appt.patient.id)

            provider_name = ""
            if appt.provider:
                provider_name = getattr(appt.provider, "credentialed_name", "") or f"{appt.provider.first_name} {appt.provider.last_name}".strip()

            duration = appt.duration_minutes or 30
            end_time = appt.start_time + timedelta(minutes=duration)

            result.append({
                "id": str(appt.id),
                "patient_name": patient_name,
                "patient_id": patient_id,
                "provider_name": provider_name,
                "start_time": appt.start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration_minutes": duration,
                "status": getattr(appt, "status", ""),
            })

        return [JSONResponse({"appointments": result}, status_code=HTTPStatus.OK)]
