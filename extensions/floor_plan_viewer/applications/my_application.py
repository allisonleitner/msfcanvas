import json
from datetime import datetime, time, timedelta, timezone
from http import HTTPStatus
from typing import Any

import requests as http_requests  # type: ignore[import-untyped]
from logger import log

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.effects.simple_api import JSONResponse, Response
from canvas_sdk.handlers.application import Application
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string
from canvas_sdk.v1.data import Staff
from canvas_sdk.v1.data.appointment import Appointment


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class MyApplication(Application):
    """Elle Medicine Floor Plan Viewer - interactive room & resource dashboard."""

    def on_open(self) -> Effect:
        return LaunchModalEffect(
            content=render_to_string("templates/floor_plan.html"),
            target=LaunchModalEffect.TargetType.PAGE,
        ).apply()


# ---------------------------------------------------------------------------
# FHIR helper
# ---------------------------------------------------------------------------


class FHIRClient:
    """Lightweight client for Canvas FHIR API calls."""

    def __init__(self, instance: str, client_id: str, client_secret: str):
        self.base_url = f"https://{instance}.canvasmedical.com"
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        resp = http_requests.post(
            f"{self.base_url}/auth/token/",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def create_practitioner(self, name: str) -> dict:
        """Create a FHIR Practitioner resource (for schedulable resources)."""
        parts = name.split(" ", 1)
        given = parts[0]
        family = parts[1] if len(parts) > 1 else "Resource"
        payload = {
            "resourceType": "Practitioner",
            "name": [{"family": family, "given": [given], "text": name}],
            "active": True,
        }
        resp = http_requests.post(
            f"{self.base_url}/Practitioner",
            json=payload,
            headers=self._headers(),
            timeout=15,
        )
        return dict(resp.json())

    def create_location(self, name: str, physical_type: str = "ro") -> dict:
        """Create a FHIR Location resource. physical_type: 'ro' (room) or 'area'."""
        display = "Room" if physical_type == "ro" else "Area"
        payload = {
            "resourceType": "Location",
            "name": name,
            "status": "active",
            "mode": "instance",
            "physicalType": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/location-physical-type",
                        "code": physical_type,
                        "display": display,
                    }
                ]
            },
        }
        resp = http_requests.post(
            f"{self.base_url}/Location",
            json=payload,
            headers=self._headers(),
            timeout=15,
        )
        return dict(resp.json())

    def update_appointment_location(self, appointment_id: str, location_id: str) -> dict:
        """Update an appointment's supportingInformation with a Location reference."""
        headers = self._headers()
        # GET current state
        resp = http_requests.get(
            f"{self.base_url}/Appointment/{appointment_id}",
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return {"error": f"Could not fetch appointment: {resp.status_code}"}
        appt = resp.json()

        # Merge location into supportingInformation
        ref = {"reference": f"Location/{location_id}"}
        si = appt.get("supportingInformation", [])
        # Replace any existing Location refs
        si = [r for r in si if not r.get("reference", "").startswith("Location/")]
        si.append(ref)
        appt["supportingInformation"] = si

        resp = http_requests.put(
            f"{self.base_url}/Appointment/{appointment_id}",
            json=appt,
            headers=headers,
            timeout=15,
        )
        return dict(resp.json())


# ---------------------------------------------------------------------------
# Seed data
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

ELLE_MEDICINE_RESOURCES = [
    {"key": "iv-therapy", "name": "IV Therapy", "resource_type": "service", "room_key": "studio-one", "portable": False, "price_cents": 15000, "credit_amount": 1, "default_duration_minutes": 45, "description": "Vitamin & nutrient IV drip"},
    {"key": "nad-infusion", "name": "NAD+ Infusion", "resource_type": "service", "room_key": "studio-one", "portable": False, "price_cents": 35000, "credit_amount": 2, "default_duration_minutes": 90, "description": "NAD+ anti-aging infusion therapy"},
    {"key": "chelation", "name": "Chelation Therapy", "resource_type": "service", "room_key": "studio-two", "portable": False, "price_cents": 25000, "credit_amount": 2, "default_duration_minutes": 60, "description": "Heavy metal chelation treatment"},
    {"key": "hyperbaric", "name": "Hyperbaric Chamber", "resource_type": "equipment", "room_key": "float", "portable": False, "price_cents": 20000, "credit_amount": 1, "default_duration_minutes": 60, "description": "Hyperbaric oxygen therapy session"},
    {"key": "acupuncture", "name": "Acupuncture", "resource_type": "service", "room_key": "restore", "portable": False, "price_cents": 12000, "credit_amount": 1, "default_duration_minutes": 45, "description": "Traditional acupuncture treatment"},
    {"key": "massage", "name": "Massage Therapy", "resource_type": "service", "room_key": "restore", "portable": False, "price_cents": 15000, "credit_amount": 1, "default_duration_minutes": 60, "description": "Therapeutic massage session"},
    {"key": "float-therapy", "name": "Float Therapy", "resource_type": "service", "room_key": "float", "portable": False, "price_cents": 10000, "credit_amount": 1, "default_duration_minutes": 60, "description": "Sensory deprivation float session"},
    {"key": "lab-panel", "name": "Lab Panel", "resource_type": "service", "room_key": "lab", "portable": False, "price_cents": 8000, "credit_amount": 0, "default_duration_minutes": 15, "description": "Comprehensive blood work / lab panel"},
]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class FloorPlanApi(StaffSessionAuthMixin, SimpleAPI):
    """REST API for rooms, resources, assignments, and Canvas integration."""

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

    def _fhir_client(self) -> FHIRClient | None:
        instance = self.environment.get("CUSTOMER_IDENTIFIER", "")
        client_id = self.secrets.get("FHIR_CLIENT_ID", "")
        client_secret = self.secrets.get("FHIR_CLIENT_SECRET", "")
        if not all([instance, client_id, client_secret]):
            return None
        return FHIRClient(instance, client_id, client_secret)

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
                    "practice_location_id": r.practice_location_id or "",
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

        for field in ("key", "name", "room_type"):
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

        for field in ("name", "room_type", "number", "bookable", "equipment", "practice_location_id"):
            if field in body:
                setattr(room, field, body[field])
        room.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.post("/rooms/seed")
    def seed_rooms(self) -> list[Response | Effect]:
        """Idempotent seed of Elle Medicine rooms and resources."""
        from floor_plan_viewer.models.custom_data import Resource, Room

        rooms_created = 0
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
                rooms_created += 1

        resources_created = 0
        for res in ELLE_MEDICINE_RESOURCES:
            _, was_created = Resource.objects.get_or_create(
                key=res["key"],
                defaults={
                    "name": res["name"],
                    "resource_type": res["resource_type"],
                    "room_key": res["room_key"],
                    "portable": res["portable"],
                    "price_cents": res.get("price_cents", 0),
                    "credit_amount": res.get("credit_amount", 0),
                    "default_duration_minutes": res.get("default_duration_minutes", 30),
                    "description": res.get("description", ""),
                },
            )
            if was_created:
                resources_created += 1

        return [JSONResponse({
            "success": True,
            "rooms_created": rooms_created,
            "resources_created": resources_created,
        }, status_code=HTTPStatus.OK)]

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    @api.get("/resources")
    def get_resources(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Resource

        resources = Resource.objects.filter(active=True).order_by("key")
        return [JSONResponse({
            "resources": [
                {
                    "id": r.pk,
                    "key": r.key,
                    "name": r.name,
                    "resource_type": r.resource_type,
                    "room_key": r.room_key,
                    "description": r.description or "",
                    "portable": r.portable,
                    "price_cents": r.price_cents or 0,
                    "credit_amount": r.credit_amount or 0,
                    "default_duration_minutes": r.default_duration_minutes or 30,
                    "practice_location_id": r.practice_location_id or "",
                    "practitioner_id": r.practitioner_id or "",
                }
                for r in resources
            ]
        }, status_code=HTTPStatus.OK)]

    @api.post("/resources")
    def create_resource(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Resource

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        for field in ("key", "name", "resource_type"):
            if not body.get(field):
                return [JSONResponse({"error": f"{field} is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        resource = Resource.objects.create(
            key=body["key"],
            name=body["name"],
            resource_type=body["resource_type"],
            room_key=body.get("room_key", ""),
            description=body.get("description", ""),
            portable=body.get("portable", False),
            price_cents=body.get("price_cents", 0),
            credit_amount=body.get("credit_amount", 0),
            default_duration_minutes=body.get("default_duration_minutes", 30),
        )
        return [JSONResponse({"success": True, "id": resource.pk}, status_code=HTTPStatus.CREATED)]

    @api.put("/resources/<key>")
    def update_resource(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Resource

        key = self.request.path_params["key"]
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        resource = Resource.objects.filter(key=key).first()
        if not resource:
            return [JSONResponse({"error": "Resource not found"}, status_code=HTTPStatus.NOT_FOUND)]

        for field in ("name", "resource_type", "room_key", "description", "portable",
                       "price_cents", "credit_amount", "default_duration_minutes", "active"):
            if field in body:
                setattr(resource, field, body[field])
        resource.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.delete("/resources/<key>")
    def delete_resource(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Resource

        key = self.request.path_params["key"]
        resource = Resource.objects.filter(key=key).first()
        if not resource:
            return [JSONResponse({"error": "Resource not found"}, status_code=HTTPStatus.NOT_FOUND)]
        resource.active = False
        resource.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.post("/resources/<key>/create-practitioner")
    def create_resource_practitioner(self) -> list[Response | Effect]:
        """Create a Canvas Practitioner for this resource so it appears on the scheduling screen."""
        from floor_plan_viewer.models.custom_data import Resource

        key = self.request.path_params["key"]
        resource = Resource.objects.filter(key=key).first()
        if not resource:
            return [JSONResponse({"error": "Resource not found"}, status_code=HTTPStatus.NOT_FOUND)]

        if resource.practitioner_id:
            return [JSONResponse({"error": "Already has a practitioner", "practitioner_id": resource.practitioner_id}, status_code=HTTPStatus.CONFLICT)]

        fhir = self._fhir_client()
        if not fhir:
            return [JSONResponse({"error": "FHIR credentials not configured"}, status_code=HTTPStatus.BAD_REQUEST)]

        try:
            resp = fhir.create_practitioner(resource.name)
            pract_id = resp.get("id", "")
            if pract_id:
                resource.practitioner_id = pract_id
                resource.save()
                return [JSONResponse({"success": True, "practitioner_id": pract_id}, status_code=HTTPStatus.OK)]
            return [JSONResponse({"error": f"FHIR response: {resp}"}, status_code=HTTPStatus.BAD_GATEWAY)]
        except Exception as e:
            return [JSONResponse({"error": str(e)}, status_code=HTTPStatus.BAD_GATEWAY)]

    # ------------------------------------------------------------------
    # Waiting room - checked-in patients not yet in a room
    # ------------------------------------------------------------------

    @api.get("/waiting")
    def get_waiting_patients(self) -> list[Response | Effect]:
        """Return checked-in appointments that don't have an active room assignment."""
        from floor_plan_viewer.models.custom_data import RoomAssignment

        start, end = self._today_range()

        appointments = Appointment.objects.filter(
            start_time__gte=start,
            start_time__lt=end,
        ).select_related("patient", "provider").order_by("start_time")

        # IDs that already have a non-cancelled room assignment
        assigned_ids = set(
            RoomAssignment.objects.filter(
                start_time__lt=end,
                end_time__gt=start,
            ).exclude(status="cancelled").values_list("appointment_id", flat=True)
        )

        waiting = []
        for appt in appointments:
            appt_status = str(getattr(appt, "status", ""))
            # Canvas uses various status representations; look for checked-in
            if "check" not in appt_status.lower():
                continue
            if str(appt.id) in assigned_ids:
                continue

            patient_name = ""
            patient_id = ""
            if appt.patient:
                patient_name = f"{appt.patient.first_name} {appt.patient.last_name}".strip()
                patient_id = str(appt.patient.id)

            provider_name = ""
            if appt.provider:
                provider_name = (
                    getattr(appt.provider, "credentialed_name", "")
                    or f"{appt.provider.first_name} {appt.provider.last_name}".strip()
                )

            duration = appt.duration_minutes or 30
            end_time = appt.start_time + timedelta(minutes=duration)

            waiting.append({
                "id": str(appt.id),
                "patient_name": patient_name,
                "patient_id": patient_id,
                "provider_name": provider_name,
                "start_time": appt.start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "status": appt_status,
            })

        return [JSONResponse({"waiting": waiting}, status_code=HTTPStatus.OK)]

    # ------------------------------------------------------------------
    # Canvas sync - create PracticeLocations for rooms & resources
    # ------------------------------------------------------------------

    @api.post("/sync-to-canvas")
    def sync_to_canvas(self) -> list[Response | Effect]:
        """Create PracticeLocations in Canvas for all bookable rooms and resources."""
        from floor_plan_viewer.models.custom_data import Resource, Room

        fhir = self._fhir_client()
        if not fhir:
            return [JSONResponse({
                "error": "FHIR credentials not configured. Set FHIR_CLIENT_ID and FHIR_CLIENT_SECRET secrets.",
            }, status_code=HTTPStatus.BAD_REQUEST)]

        errors: list[str] = []
        rooms_synced = 0
        resources_synced = 0

        # Sync bookable rooms
        for room in Room.objects.filter(active=True, bookable=True):
            if room.practice_location_id:
                continue
            try:
                resp = fhir.create_location(room.name, physical_type="ro")
                loc_id = resp.get("id", "")
                if loc_id:
                    room.practice_location_id = loc_id
                    room.save()
                    rooms_synced = rooms_synced + 1
                else:
                    errors.append(f"Room {room.key}: {resp}")
            except Exception as e:
                errors.append(f"Room {room.key}: {e}")

        # Sync resources
        for resource in Resource.objects.filter(active=True):
            if resource.practice_location_id:
                continue
            try:
                resp = fhir.create_location(resource.name, physical_type="ro")
                loc_id = resp.get("id", "")
                if loc_id:
                    resource.practice_location_id = loc_id
                    resource.save()
                    resources_synced = resources_synced + 1
                else:
                    errors.append(f"Resource {resource.key}: {resp}")
            except Exception as e:
                errors.append(f"Resource {resource.key}: {e}")

        return [JSONResponse({
            "rooms_synced": rooms_synced,
            "resources_synced": resources_synced,
            "errors": errors,
        }, status_code=HTTPStatus.OK)]

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
                return [JSONResponse({"error": "Invalid date format"}, status_code=HTTPStatus.BAD_REQUEST)]
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
                    "resource_keys": a.resource_keys or [],
                }
                for a in assignments
            ]
        }, status_code=HTTPStatus.OK)]

    @api.post("/assignments")
    def create_assignment(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import Room, RoomAssignment

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        for field in ("room_key", "start_time", "end_time"):
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
            resource_keys=body.get("resource_keys", []),
            assigned_by_id=staff_dbid,
        )

        # Update appointment location in Canvas if we have FHIR creds and a location ID
        canvas_result = None
        appointment_id = body.get("appointment_id", "")
        if appointment_id:
            room = Room.objects.filter(key=body["room_key"]).first()
            if room and room.practice_location_id:
                fhir = self._fhir_client()
                if fhir:
                    try:
                        canvas_result = fhir.update_appointment_location(
                            appointment_id, room.practice_location_id
                        )
                    except Exception as e:
                        log.warning("Failed to update appointment location in Canvas: %s", e)
                        canvas_result = {"error": str(e)}

        return [JSONResponse({
            "success": True,
            "id": assignment.pk,
            "canvas_sync": canvas_result,
        }, status_code=HTTPStatus.CREATED)]

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

        for field in ("room_key", "status", "notes", "start_time", "end_time",
                       "patient_name", "appointment_type", "provider_name", "resource_keys"):
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
                provider_name = (
                    getattr(appt.provider, "credentialed_name", "")
                    or f"{appt.provider.first_name} {appt.provider.last_name}".strip()
                )

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
