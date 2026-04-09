import base64
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
        self.auth_url = f"https://{instance}.canvasmedical.com"
        self.fhir_url = f"https://fumage-{instance}.canvasmedical.com"
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        resp = http_requests.post(
            f"{self.auth_url}/auth/token/",
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
            f"{self.fhir_url}/Practitioner",
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
            f"{self.fhir_url}/Location",
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
            f"{self.fhir_url}/Appointment/{appointment_id}",
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
            f"{self.fhir_url}/Appointment/{appointment_id}",
            json=appt,
            headers=headers,
            timeout=15,
        )
        return dict(resp.json())


# ---------------------------------------------------------------------------
# Sonos API client
# ---------------------------------------------------------------------------


class SonosClient:
    """Lightweight client for Sonos Control API calls."""

    API_BASE = "https://api.ws.sonos.com/control/api/v1"
    TOKEN_URL = "https://api.sonos.com/login/v3/oauth/access"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token: str | None = None

    def _get_basic_auth(self) -> str:
        creds = f"{self.client_id}:{self.client_secret}"
        return base64.b64encode(creds.encode()).decode()

    def _refresh_access_token(self) -> str:
        resp = http_requests.post(
            self.TOKEN_URL,
            headers={
                "Authorization": f"Basic {self._get_basic_auth()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        return self._access_token

    def _get_token(self) -> str:
        if self._access_token:
            return self._access_token
        return self._refresh_access_token()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        """Make a Sonos API request with automatic token refresh on 401."""
        url = f"{self.API_BASE}{path}"
        resp = http_requests.request(
            method, url, headers=self._headers(), timeout=15, **kwargs
        )
        if resp.status_code == 401:
            self._refresh_access_token()
            resp = http_requests.request(
                method, url, headers=self._headers(), timeout=15, **kwargs
            )
        resp.raise_for_status()
        if resp.status_code == 200 and resp.content:
            return dict(resp.json())
        return {"status": resp.status_code}

    # -- Discovery --

    def get_households(self) -> dict:
        return self._request("GET", "/households")

    def get_groups(self, household_id: str) -> dict:
        return self._request("GET", f"/households/{household_id}/groups")

    def get_favorites(self, household_id: str) -> dict:
        return self._request("GET", f"/households/{household_id}/favorites")

    # -- Playback --

    def load_favorite(self, group_id: str, favorite_id: str, play_on_completion: bool = True) -> dict:
        return self._request(
            "POST",
            f"/groups/{group_id}/favorites",
            json={"favoriteId": favorite_id, "playOnCompletion": play_on_completion},
        )

    def play(self, group_id: str) -> dict:
        return self._request("POST", f"/groups/{group_id}/playback/play")

    def pause(self, group_id: str) -> dict:
        return self._request("POST", f"/groups/{group_id}/playback/pause")

    def set_volume(self, group_id: str, volume: int) -> dict:
        return self._request(
            "POST",
            f"/groups/{group_id}/groupVolume",
            json={"volume": volume},
        )

    def play_audio_clip(
        self,
        player_id: str,
        stream_url: str,
        name: str = "Floor Plan Alert",
        volume: int = 50,
    ) -> dict:
        return self._request(
            "POST",
            f"/players/{player_id}/audioClip",
            json={
                "name": name,
                "appId": "com.ellemedicine.floorplan",
                "priority": "LOW",
                "streamUrl": stream_url,
                "volume": volume,
            },
        )


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

ELLE_MEDICINE_ROOMS = [
    # Clinical Exam Rooms
    {"key": "elle-one", "name": "Elle One", "room_type": "exam", "number": "105", "bookable": True, "equipment": []},
    {"key": "elle-two", "name": "Elle Two", "room_type": "exam", "number": "106", "bookable": True, "equipment": []},
    {"key": "thrive", "name": "Thrive", "room_type": "exam", "number": "107", "bookable": True, "equipment": ["InBody"]},
    # Wellness / Treatment Rooms
    {"key": "restore", "name": "Restore", "room_type": "treatment", "number": "109", "bookable": True, "equipment": ["Hyperbaric Chamber"]},
    {"key": "float", "name": "Float", "room_type": "treatment", "number": "108", "bookable": True, "equipment": ["Zero Gravity Dry Float"]},
    # Member / Schedulable Spaces
    {"key": "studio-one", "name": "Studio One", "room_type": "hoteling", "number": "104", "bookable": True, "equipment": []},
    {"key": "collective", "name": "The Collective", "room_type": "conference", "number": "105", "bookable": True, "equipment": []},
    # Staff / Non-Schedulable
    {"key": "studio-two", "name": "Studio Two", "room_type": "staff", "number": "113", "bookable": False, "equipment": []},
    {"key": "lab", "name": "Lab", "room_type": "lab", "number": "115", "bookable": False, "equipment": []},
    {"key": "hub", "name": "The Hub", "room_type": "utility", "number": "", "bookable": False, "equipment": []},
    {"key": "cafe", "name": "The Cafe", "room_type": "utility", "number": "114", "bookable": False, "equipment": []},
    {"key": "it", "name": "IT", "room_type": "utility", "number": "116", "bookable": False, "equipment": []},
    {"key": "hallway", "name": "Hallway", "room_type": "utility", "number": "", "bookable": False, "equipment": []},
    {"key": "entry", "name": "Entry", "room_type": "utility", "number": "100", "bookable": False, "equipment": []},
    {"key": "lounge", "name": "Lounge", "room_type": "waiting", "number": "102", "bookable": False, "equipment": []},
    {"key": "physician", "name": "Physician's Suite", "room_type": "office", "number": "111", "bookable": False, "equipment": []},
    {"key": "workroom", "name": "Workroom", "room_type": "utility", "number": "112", "bookable": False, "equipment": []},
]

ELLE_MEDICINE_RESOURCES = [
    # Treatment services - Restore
    {"key": "hyperbaric", "name": "Hyperbaric Oxygen Therapy", "resource_type": "service", "room_key": "restore", "portable": False, "price_cents": 20000, "credit_amount": 1, "default_duration_minutes": 60, "description": "Hyperbaric oxygen therapy session"},
    # Treatment services - Float
    {"key": "dry-float", "name": "Zero Gravity Dry Float", "resource_type": "service", "room_key": "float", "portable": False, "price_cents": 10000, "credit_amount": 1, "default_duration_minutes": 60, "description": "Zero gravity dry float for nervous system recovery"},
    # Clinical services - Thrive (Dietician)
    {"key": "inbody", "name": "InBody Scan", "resource_type": "service", "room_key": "thrive", "portable": False, "price_cents": 5000, "credit_amount": 0, "default_duration_minutes": 15, "description": "InBody composition analysis"},
    {"key": "nutrition-consult", "name": "Nutrition Consult", "resource_type": "service", "room_key": "thrive", "portable": False, "price_cents": 15000, "credit_amount": 1, "default_duration_minutes": 45, "description": "Lifestyle & performance nutrition consultation"},
]

# Demo data for testing without Sonos OAuth credentials
SONOS_DEMO_HOUSEHOLD = {"id": "demo-household-001", "name": "Elle Medicine Demo"}
SONOS_DEMO_PLAYERS = [
    {"id": "demo-player-restore", "name": "Restore Room Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-studio-one", "name": "Studio One Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-studio-two", "name": "Studio Two Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-float", "name": "Float Room Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-elle-one", "name": "Elle One Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-elle-two", "name": "Elle Two Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-thrive", "name": "Thrive Room Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-collective", "name": "Collective Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
    {"id": "demo-player-lobby", "name": "Lobby Sonos", "capabilities": ["PLAYBACK", "AUDIO_CLIP"]},
]
SONOS_DEMO_GROUPS = [
    {"id": "demo-group-restore", "name": "Restore", "playerIds": ["demo-player-restore"]},
    {"id": "demo-group-studio-one", "name": "Studio One", "playerIds": ["demo-player-studio-one"]},
    {"id": "demo-group-studio-two", "name": "Studio Two", "playerIds": ["demo-player-studio-two"]},
    {"id": "demo-group-float", "name": "Float", "playerIds": ["demo-player-float"]},
    {"id": "demo-group-elle-one", "name": "Elle One", "playerIds": ["demo-player-elle-one"]},
    {"id": "demo-group-elle-two", "name": "Elle Two", "playerIds": ["demo-player-elle-two"]},
    {"id": "demo-group-thrive", "name": "Thrive", "playerIds": ["demo-player-thrive"]},
    {"id": "demo-group-collective", "name": "Collective", "playerIds": ["demo-player-collective"]},
    {"id": "demo-group-lobby", "name": "Lobby", "playerIds": ["demo-player-lobby"]},
]
SONOS_DEMO_FAVORITES = [
    {"id": "demo-fav-ocean", "name": "Ocean Waves", "description": "Calming ocean sounds"},
    {"id": "demo-fav-forest", "name": "Forest Rain", "description": "Gentle rain on leaves"},
    {"id": "demo-fav-tibetan", "name": "Tibetan Bowls", "description": "Singing bowl meditation"},
    {"id": "demo-fav-ambient", "name": "Ambient Wellness", "description": "Soft ambient piano & nature"},
    {"id": "demo-fav-spa", "name": "Spa Relaxation", "description": "Classic spa background music"},
    {"id": "demo-fav-nature", "name": "Nature Sounds Mix", "description": "Birds, streams, wind"},
    {"id": "demo-fav-lo-fi", "name": "Lo-Fi Calm", "description": "Chill lo-fi beats"},
    {"id": "demo-fav-classical", "name": "Soft Classical", "description": "Quiet classical strings"},
]

ELLE_MEDICINE_AUDIO_PRESETS = [
    {"key": "acupuncture-ambient", "name": "Acupuncture Ambient", "match_type": "resource_key", "match_value": "acupuncture", "volume": 20, "priority": 10},
    {"key": "iv-therapy-ambient", "name": "IV Therapy Ambient", "match_type": "resource_key", "match_value": "iv-therapy", "volume": 35, "priority": 10},
    {"key": "nad-infusion-ambient", "name": "NAD+ Infusion Ambient", "match_type": "resource_key", "match_value": "nad-infusion", "volume": 30, "priority": 10},
    {"key": "chelation-ambient", "name": "Chelation Ambient", "match_type": "resource_key", "match_value": "chelation", "volume": 30, "priority": 10},
    {"key": "float-ambient", "name": "Float Therapy Ambient", "match_type": "resource_key", "match_value": "float-therapy", "volume": 15, "priority": 10},
    {"key": "hyperbaric-ambient", "name": "Hyperbaric Ambient", "match_type": "resource_key", "match_value": "hyperbaric", "volume": 25, "priority": 10},
    {"key": "massage-ambient", "name": "Massage Ambient", "match_type": "resource_key", "match_value": "massage", "volume": 20, "priority": 10},
    {"key": "wellness-default", "name": "Wellness Room Default", "match_type": "room_type", "match_value": "wellness", "volume": 25, "priority": 5},
    {"key": "exam-default", "name": "Exam Room Default", "match_type": "room_type", "match_value": "exam", "volume": 20, "priority": 5},
    {"key": "treatment-default", "name": "Treatment Room Default", "match_type": "room_type", "match_value": "treatment", "volume": 25, "priority": 5},
    {"key": "default-ambient", "name": "Default Ambient", "match_type": "default", "match_value": "", "volume": 25, "priority": 0},
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

    def _sonos_client(self) -> SonosClient | None:
        client_id = self.secrets.get("SONOS_CLIENT_ID", "")
        client_secret = self.secrets.get("SONOS_CLIENT_SECRET", "")
        refresh_token = self.secrets.get("SONOS_REFRESH_TOKEN", "")
        if not all([client_id, client_secret, refresh_token]):
            return None
        return SonosClient(client_id, client_secret, refresh_token)

    def _sonos_demo_mode(self) -> bool:
        """True when Sonos credentials are not configured (use demo data)."""
        return self._sonos_client() is None

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
        """Idempotent seed of Elle Medicine rooms, resources, and audio presets."""
        from floor_plan_viewer.models.custom_data import AudioPreset, Resource, Room

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

        presets_created = 0
        for p in ELLE_MEDICINE_AUDIO_PRESETS:
            _, was_created = AudioPreset.objects.get_or_create(
                key=p["key"],
                defaults={
                    "name": p["name"],
                    "match_type": p["match_type"],
                    "match_value": p["match_value"],
                    "volume": p["volume"],
                    "priority": p["priority"],
                },
            )
            if was_created:
                presets_created += 1

        return [JSONResponse({
            "success": True,
            "rooms_created": rooms_created,
            "resources_created": resources_created,
            "presets_created": presets_created,
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

        # Sonos auto-play if assignment created as in-progress
        sonos_result = None
        if body.get("status") == "in-progress":
            try:
                sonos_result = self._sonos_trigger_play(assignment, triggered_by="auto_assign")
            except Exception as e:
                log.warning("Sonos auto-play on create failed: %s", e)

        return [JSONResponse({
            "success": True,
            "id": assignment.pk,
            "canvas_sync": canvas_result,
            "sonos": sonos_result,
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

        # Sonos triggers on status change
        sonos_result = None
        new_status = body.get("status")
        try:
            if new_status == "in-progress":
                sonos_result = self._sonos_trigger_play(assignment, triggered_by="auto_start")
            elif new_status in ("completed", "cancelled"):
                sonos_result = self._sonos_trigger_pause(assignment, triggered_by="auto_complete")
        except Exception as e:
            log.warning("Sonos trigger on assignment update failed: %s", e)

        return [JSONResponse({"success": True, "sonos": sonos_result}, status_code=HTTPStatus.OK)]

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

    # ------------------------------------------------------------------
    # Sonos - Discovery & Configuration
    # ------------------------------------------------------------------

    @api.get("/sonos/status")
    def sonos_status(self) -> list[Response | Effect]:
        """Health check: is Sonos configured, how many speakers mapped."""
        from floor_plan_viewer.models.custom_data import SonosSpeaker

        demo = self._sonos_demo_mode()
        speaker_count = SonosSpeaker.objects.filter(active=True).count()
        return [JSONResponse({
            "configured": True,  # always true so UI is usable (demo mode fills gaps)
            "demo_mode": demo,
            "speaker_count": speaker_count,
        }, status_code=HTTPStatus.OK)]

    @api.get("/sonos/households")
    def sonos_households(self) -> list[Response | Effect]:
        if self._sonos_demo_mode():
            return [JSONResponse({"households": [SONOS_DEMO_HOUSEHOLD]}, status_code=HTTPStatus.OK)]
        sonos = self._sonos_client()
        assert sonos is not None
        try:
            data = sonos.get_households()
            return [JSONResponse(data, status_code=HTTPStatus.OK)]
        except Exception as e:
            log.warning("Sonos households error: %s", e)
            return [JSONResponse({"error": str(e)}, status_code=HTTPStatus.BAD_GATEWAY)]

    @api.get("/sonos/players")
    def sonos_players(self) -> list[Response | Effect]:
        household_id = self.request.query_params.get("household_id", "")
        if not household_id:
            return [JSONResponse({"error": "household_id query param required"}, status_code=HTTPStatus.BAD_REQUEST)]
        if self._sonos_demo_mode():
            return [JSONResponse({"players": SONOS_DEMO_PLAYERS, "groups": SONOS_DEMO_GROUPS}, status_code=HTTPStatus.OK)]
        sonos = self._sonos_client()
        assert sonos is not None
        try:
            data = sonos.get_groups(household_id)
            return [JSONResponse(data, status_code=HTTPStatus.OK)]
        except Exception as e:
            log.warning("Sonos players error: %s", e)
            return [JSONResponse({"error": str(e)}, status_code=HTTPStatus.BAD_GATEWAY)]

    @api.get("/sonos/favorites")
    def sonos_favorites(self) -> list[Response | Effect]:
        household_id = self.request.query_params.get("household_id", "")
        if not household_id:
            return [JSONResponse({"error": "household_id query param required"}, status_code=HTTPStatus.BAD_REQUEST)]
        if self._sonos_demo_mode():
            return [JSONResponse({"items": SONOS_DEMO_FAVORITES}, status_code=HTTPStatus.OK)]
        sonos = self._sonos_client()
        assert sonos is not None
        try:
            data = sonos.get_favorites(household_id)
            return [JSONResponse(data, status_code=HTTPStatus.OK)]
        except Exception as e:
            log.warning("Sonos favorites error: %s", e)
            return [JSONResponse({"error": str(e)}, status_code=HTTPStatus.BAD_GATEWAY)]

    # ------------------------------------------------------------------
    # Sonos - Speaker Mapping CRUD
    # ------------------------------------------------------------------

    @api.get("/sonos/speakers")
    def get_sonos_speakers(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import SonosSpeaker

        speakers = SonosSpeaker.objects.filter(active=True).order_by("room_key")
        return [JSONResponse({
            "speakers": [
                {
                    "id": s.pk,
                    "room_key": s.room_key,
                    "player_id": s.player_id,
                    "group_id": s.group_id or "",
                    "player_name": s.player_name,
                    "household_id": s.household_id,
                }
                for s in speakers
            ]
        }, status_code=HTTPStatus.OK)]

    @api.post("/sonos/speakers")
    def create_sonos_speaker(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import SonosSpeaker

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        for field in ("room_key", "player_id", "player_name", "household_id"):
            if not body.get(field):
                return [JSONResponse({"error": f"{field} is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        # Upsert: if room already has a speaker mapping, update it
        existing = SonosSpeaker.objects.filter(room_key=body["room_key"]).first()
        if existing:
            existing.player_id = body["player_id"]
            existing.group_id = body.get("group_id", "")
            existing.player_name = body["player_name"]
            existing.household_id = body["household_id"]
            existing.active = True
            existing.save()
            return [JSONResponse({"success": True, "id": existing.pk, "updated": True}, status_code=HTTPStatus.OK)]

        speaker = SonosSpeaker.objects.create(
            room_key=body["room_key"],
            player_id=body["player_id"],
            group_id=body.get("group_id", ""),
            player_name=body["player_name"],
            household_id=body["household_id"],
        )
        return [JSONResponse({"success": True, "id": speaker.pk}, status_code=HTTPStatus.CREATED)]

    @api.put("/sonos/speakers/<room_key>")
    def update_sonos_speaker(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import SonosSpeaker

        room_key = self.request.path_params["room_key"]
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        speaker = SonosSpeaker.objects.filter(room_key=room_key, active=True).first()
        if not speaker:
            return [JSONResponse({"error": "Speaker mapping not found"}, status_code=HTTPStatus.NOT_FOUND)]

        for field in ("player_id", "group_id", "player_name", "household_id"):
            if field in body:
                setattr(speaker, field, body[field])
        speaker.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.delete("/sonos/speakers/<room_key>")
    def delete_sonos_speaker(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import SonosSpeaker

        room_key = self.request.path_params["room_key"]
        speaker = SonosSpeaker.objects.filter(room_key=room_key).first()
        if not speaker:
            return [JSONResponse({"error": "Speaker mapping not found"}, status_code=HTTPStatus.NOT_FOUND)]
        speaker.active = False
        speaker.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    # ------------------------------------------------------------------
    # Sonos - Audio Preset CRUD
    # ------------------------------------------------------------------

    @api.get("/sonos/presets")
    def get_sonos_presets(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import AudioPreset

        presets = AudioPreset.objects.filter(active=True).order_by("-priority", "key")
        return [JSONResponse({
            "presets": [
                {
                    "id": p.pk,
                    "key": p.key,
                    "name": p.name,
                    "match_type": p.match_type,
                    "match_value": p.match_value,
                    "sonos_favorite_id": p.sonos_favorite_id or "",
                    "sonos_favorite_name": p.sonos_favorite_name or "",
                    "volume": p.volume,
                    "priority": p.priority,
                }
                for p in presets
            ]
        }, status_code=HTTPStatus.OK)]

    @api.post("/sonos/presets")
    def create_sonos_preset(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import AudioPreset

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        for field in ("key", "name", "match_type"):
            if not body.get(field):
                return [JSONResponse({"error": f"{field} is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        if AudioPreset.objects.filter(key=body["key"]).exists():
            return [JSONResponse({"error": "Preset key already exists"}, status_code=HTTPStatus.CONFLICT)]

        preset = AudioPreset.objects.create(
            key=body["key"],
            name=body["name"],
            match_type=body["match_type"],
            match_value=body.get("match_value", ""),
            sonos_favorite_id=body.get("sonos_favorite_id", ""),
            sonos_favorite_name=body.get("sonos_favorite_name", ""),
            volume=body.get("volume", 25),
            priority=body.get("priority", 0),
        )
        return [JSONResponse({"success": True, "id": preset.pk}, status_code=HTTPStatus.CREATED)]

    @api.put("/sonos/presets/<key>")
    def update_sonos_preset(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import AudioPreset

        key = self.request.path_params["key"]
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        preset = AudioPreset.objects.filter(key=key, active=True).first()
        if not preset:
            return [JSONResponse({"error": "Preset not found"}, status_code=HTTPStatus.NOT_FOUND)]

        for field in ("name", "match_type", "match_value", "sonos_favorite_id",
                       "sonos_favorite_name", "volume", "priority"):
            if field in body:
                setattr(preset, field, body[field])
        preset.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.delete("/sonos/presets/<key>")
    def delete_sonos_preset(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import AudioPreset

        key = self.request.path_params["key"]
        preset = AudioPreset.objects.filter(key=key).first()
        if not preset:
            return [JSONResponse({"error": "Preset not found"}, status_code=HTTPStatus.NOT_FOUND)]
        preset.active = False
        preset.save()
        return [JSONResponse({"success": True}, status_code=HTTPStatus.OK)]

    @api.post("/sonos/presets/seed")
    def seed_sonos_presets(self) -> list[Response | Effect]:
        """Idempotent seed of default audio presets."""
        from floor_plan_viewer.models.custom_data import AudioPreset

        created = 0
        for p in ELLE_MEDICINE_AUDIO_PRESETS:
            _, was_created = AudioPreset.objects.get_or_create(
                key=p["key"],
                defaults={
                    "name": p["name"],
                    "match_type": p["match_type"],
                    "match_value": p["match_value"],
                    "volume": p["volume"],
                    "priority": p["priority"],
                },
            )
            if was_created:
                created += 1

        return [JSONResponse({"success": True, "presets_created": created}, status_code=HTTPStatus.OK)]

    # ------------------------------------------------------------------
    # Sonos - Playback Helpers
    # ------------------------------------------------------------------

    def _resolve_preset(self, assignment: Any, explicit_key: str = "") -> Any:
        """Resolve the best-matching audio preset for an assignment.

        Priority order:
        1. Explicit preset_key if provided
        2. Match by resource_key (from assignment.resource_keys)
        3. Match by appointment_type string
        4. Match by room_type
        5. Fallback to match_type="default"
        """
        from floor_plan_viewer.models.custom_data import AudioPreset, Room

        if explicit_key:
            preset = AudioPreset.objects.filter(key=explicit_key, active=True).first()
            if preset and preset.sonos_favorite_id:
                return preset

        active_presets = list(AudioPreset.objects.filter(active=True).exclude(sonos_favorite_id=""))

        # Match by resource_key
        resource_keys = assignment.resource_keys or []
        resource_matches = [
            p for p in active_presets
            if p.match_type == "resource_key" and p.match_value in resource_keys
        ]
        if resource_matches:
            return max(resource_matches, key=lambda p: p.priority)

        # Match by appointment_type
        appt_type = assignment.appointment_type or ""
        if appt_type:
            type_matches = [
                p for p in active_presets
                if p.match_type == "appointment_type" and p.match_value.lower() == appt_type.lower()
            ]
            if type_matches:
                return max(type_matches, key=lambda p: p.priority)

        # Match by room_type
        room = Room.objects.filter(key=assignment.room_key).first()
        if room:
            room_matches = [
                p for p in active_presets
                if p.match_type == "room_type" and p.match_value == room.room_type
            ]
            if room_matches:
                return max(room_matches, key=lambda p: p.priority)

        # Fallback to default
        defaults = [p for p in active_presets if p.match_type == "default"]
        if defaults:
            return max(defaults, key=lambda p: p.priority)

        return None

    def _sonos_trigger_play(self, assignment: Any, triggered_by: str = "auto_assign") -> dict | None:
        """Start Sonos playback for a room assignment. Returns status dict or None."""
        from floor_plan_viewer.models.custom_data import SonosPlaybackLog, SonosSpeaker

        demo = self._sonos_demo_mode()

        speaker = SonosSpeaker.objects.filter(room_key=assignment.room_key, active=True).first()
        if not speaker:
            return None

        preset = self._resolve_preset(assignment)
        if not preset:
            return {"skipped": True, "reason": "no matching preset with a Sonos favorite"}

        group_id = speaker.group_id or speaker.player_id

        # In demo mode, skip actual Sonos API calls
        if not demo:
            try:
                sonos = self._sonos_client()
                assert sonos is not None
                sonos.load_favorite(group_id, preset.sonos_favorite_id, play_on_completion=True)
                sonos.set_volume(group_id, preset.volume)
            except Exception as e:
                log.warning("Sonos play error for room %s: %s", assignment.room_key, e)
                SonosPlaybackLog.objects.create(
                    assignment_id=assignment.pk,
                    room_key=assignment.room_key,
                    player_id=speaker.player_id,
                    preset_key=preset.key,
                    action="error",
                    triggered_by=triggered_by,
                    error_message=str(e),
                )
                return {"error": str(e)}

        SonosPlaybackLog.objects.create(
            assignment_id=assignment.pk,
            room_key=assignment.room_key,
            player_id=speaker.player_id,
            preset_key=preset.key,
            action="play",
            volume=preset.volume,
            triggered_by=triggered_by,
        )
        return {"playing": True, "demo_mode": demo, "preset": preset.key, "volume": preset.volume, "speaker": speaker.player_name}

    def _sonos_trigger_pause(self, assignment: Any, triggered_by: str = "auto_complete") -> dict | None:
        """Pause Sonos playback for a room assignment."""
        from floor_plan_viewer.models.custom_data import SonosPlaybackLog, SonosSpeaker

        demo = self._sonos_demo_mode()

        speaker = SonosSpeaker.objects.filter(room_key=assignment.room_key, active=True).first()
        if not speaker:
            return None

        if not demo:
            try:
                sonos = self._sonos_client()
                assert sonos is not None
                group_id = speaker.group_id or speaker.player_id
                sonos.pause(group_id)
            except Exception as e:
                log.warning("Sonos pause error for room %s: %s", assignment.room_key, e)
                SonosPlaybackLog.objects.create(
                    assignment_id=assignment.pk,
                    room_key=assignment.room_key,
                    player_id=speaker.player_id,
                    action="error",
                    triggered_by=triggered_by,
                    error_message=str(e),
                )
                return {"error": str(e)}

        SonosPlaybackLog.objects.create(
            assignment_id=assignment.pk,
            room_key=assignment.room_key,
            player_id=speaker.player_id,
            action="pause",
            triggered_by=triggered_by,
        )
        return {"paused": True, "demo_mode": demo, "speaker": speaker.player_name}

    # ------------------------------------------------------------------
    # Sonos - Playback Control Endpoints
    # ------------------------------------------------------------------

    @api.post("/sonos/play")
    def sonos_play(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import AudioPreset, RoomAssignment, SonosPlaybackLog, SonosSpeaker

        demo = self._sonos_demo_mode()

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        room_key = body.get("room_key", "")
        if not room_key:
            return [JSONResponse({"error": "room_key is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        speaker = SonosSpeaker.objects.filter(room_key=room_key, active=True).first()
        if not speaker:
            return [JSONResponse({"error": f"No speaker mapped to room {room_key}"}, status_code=HTTPStatus.NOT_FOUND)]

        # Resolve preset
        preset = None
        preset_key = body.get("preset_key", "")
        assignment_id = body.get("assignment_id")
        triggered_by = body.get("triggered_by", "manual")

        if preset_key:
            preset = AudioPreset.objects.filter(key=preset_key, active=True).first()
        elif assignment_id:
            assignment = RoomAssignment.objects.filter(pk=assignment_id).first()
            if assignment:
                preset = self._resolve_preset(assignment)

        if not preset or not preset.sonos_favorite_id:
            return [JSONResponse({"error": "No matching preset with a Sonos favorite"}, status_code=HTTPStatus.NOT_FOUND)]

        group_id = speaker.group_id or speaker.player_id

        # In demo mode, skip actual Sonos API calls but still log
        if not demo:
            try:
                sonos = self._sonos_client()
                assert sonos is not None
                sonos.load_favorite(group_id, preset.sonos_favorite_id, play_on_completion=True)
                sonos.set_volume(group_id, preset.volume)
            except Exception as e:
                log.warning("Sonos play error: %s", e)
                return [JSONResponse({"error": str(e)}, status_code=HTTPStatus.BAD_GATEWAY)]

        SonosPlaybackLog.objects.create(
            assignment_id=assignment_id or 0,
            room_key=room_key,
            player_id=speaker.player_id,
            preset_key=preset.key,
            action="play",
            volume=preset.volume,
            triggered_by=triggered_by,
        )
        return [JSONResponse({
            "success": True,
            "playing": True,
            "demo_mode": demo,
            "preset": preset.key,
            "volume": preset.volume,
            "speaker": speaker.player_name,
        }, status_code=HTTPStatus.OK)]

    @api.post("/sonos/pause")
    def sonos_pause(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import SonosPlaybackLog, SonosSpeaker

        demo = self._sonos_demo_mode()

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        room_key = body.get("room_key", "")
        if not room_key:
            return [JSONResponse({"error": "room_key is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        speaker = SonosSpeaker.objects.filter(room_key=room_key, active=True).first()
        if not speaker:
            return [JSONResponse({"error": f"No speaker mapped to room {room_key}"}, status_code=HTTPStatus.NOT_FOUND)]

        triggered_by = body.get("triggered_by", "manual")
        assignment_id = body.get("assignment_id", 0)

        if not demo:
            try:
                sonos = self._sonos_client()
                assert sonos is not None
                group_id = speaker.group_id or speaker.player_id
                sonos.pause(group_id)
            except Exception as e:
                log.warning("Sonos pause error: %s", e)
                return [JSONResponse({"error": str(e)}, status_code=HTTPStatus.BAD_GATEWAY)]

        SonosPlaybackLog.objects.create(
            assignment_id=assignment_id,
            room_key=room_key,
            player_id=speaker.player_id,
            action="pause",
            triggered_by=triggered_by,
        )
        return [JSONResponse({"success": True, "paused": True, "demo_mode": demo}, status_code=HTTPStatus.OK)]

    @api.post("/sonos/volume")
    def sonos_volume(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import SonosPlaybackLog, SonosSpeaker

        demo = self._sonos_demo_mode()

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            return [JSONResponse({"error": "Invalid JSON"}, status_code=HTTPStatus.BAD_REQUEST)]

        room_key = body.get("room_key", "")
        volume = body.get("volume")
        if not room_key or volume is None:
            return [JSONResponse({"error": "room_key and volume are required"}, status_code=HTTPStatus.BAD_REQUEST)]

        speaker = SonosSpeaker.objects.filter(room_key=room_key, active=True).first()
        if not speaker:
            return [JSONResponse({"error": f"No speaker mapped to room {room_key}"}, status_code=HTTPStatus.NOT_FOUND)]

        if not demo:
            try:
                sonos = self._sonos_client()
                assert sonos is not None
                group_id = speaker.group_id or speaker.player_id
                sonos.set_volume(group_id, int(volume))
            except Exception as e:
                log.warning("Sonos volume error: %s", e)
                return [JSONResponse({"error": str(e)}, status_code=HTTPStatus.BAD_GATEWAY)]

        SonosPlaybackLog.objects.create(
            assignment_id=0,
            room_key=room_key,
            player_id=speaker.player_id,
            action="volume_change",
            volume=int(volume),
            triggered_by="manual",
        )
        return [JSONResponse({"success": True, "volume": int(volume), "demo_mode": demo}, status_code=HTTPStatus.OK)]

    @api.get("/sonos/log")
    def sonos_log(self) -> list[Response | Effect]:
        from floor_plan_viewer.models.custom_data import SonosPlaybackLog

        room_key = self.request.query_params.get("room_key", "")
        assignment_id = self.request.query_params.get("assignment_id", "")

        qs = SonosPlaybackLog.objects.all().order_by("-created_at")
        if room_key:
            qs = qs.filter(room_key=room_key)
        if assignment_id:
            qs = qs.filter(assignment_id=int(assignment_id))

        entries = qs[:50]
        return [JSONResponse({
            "log": [
                {
                    "id": e.pk,
                    "assignment_id": e.assignment_id,
                    "room_key": e.room_key,
                    "player_id": e.player_id,
                    "preset_key": e.preset_key,
                    "action": e.action,
                    "volume": e.volume,
                    "triggered_by": e.triggered_by,
                    "error_message": e.error_message,
                    "created_at": e.created_at.isoformat() if e.created_at else "",
                }
                for e in entries
            ]
        }, status_code=HTTPStatus.OK)]
