#!/usr/bin/env python3
"""Publish FreeStyle Libre LinkUp glucose data to MQTT.

Topics (relative to MQTT_BASE_TOPIC, default ``cgm``):
    <base>/last     - latest glucose measurement (retained)
    <base>/history  - full graph history on each fetch
    <base>/update   - publish anything here to trigger an immediate fetch
    <base>/status   - online/offline (retained, with LWT)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

LIBRE_DEFAULT_BASE = "https://api.libreview.io"
LIBRE_VERSION = "4.16.0"
LIBRE_PRODUCT = "llu.android"

log = logging.getLogger("cgm2mqtt")


# --------------------------------------------------------------------------- #
# LibreLinkUp client
# --------------------------------------------------------------------------- #

class LibreLinkUpError(RuntimeError):
    pass


class LibreLinkUp:
    def __init__(self, email: str, password: str, region: str | None = None):
        self.email = email
        self.password = password
        self.region = (region or "").strip().lower() or None
        self.session = requests.Session()
        self.token: str | None = None
        self.token_expires: int = 0
        self.account_id: str | None = None

    @property
    def base_url(self) -> str:
        return f"https://api-{self.region}.libreview.io" if self.region else LIBRE_DEFAULT_BASE

    def _headers(self, with_auth: bool = True) -> dict[str, str]:
        h = {
            "product": LIBRE_PRODUCT,
            "version": LIBRE_VERSION,
            "accept-encoding": "gzip",
            "cache-control": "no-cache",
            "connection": "Keep-Alive",
            "content-type": "application/json",
        }
        if with_auth and self.token:
            h["authorization"] = f"Bearer {self.token}"
        if with_auth and self.account_id:
            h["account-id"] = self.account_id
        return h

    def login(self) -> None:
        # Up to 3 attempts to follow region redirects.
        for _ in range(3):
            r = self.session.post(
                f"{self.base_url}/llu/auth/login",
                headers=self._headers(with_auth=False),
                json={"email": self.email, "password": self.password},
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") or {}

            redirect_region = (data.get("redirect") and data.get("region")) or None
            if redirect_region:
                log.info("Login redirected to region %s", data["region"])
                self.region = data["region"]
                continue

            ticket = data.get("authTicket") or {}
            if not ticket.get("token"):
                # Surface 2FA / terms-of-use steps explicitly.
                step = data.get("step") or {}
                raise LibreLinkUpError(
                    f"Login did not return a token. status={payload.get('status')} "
                    f"step={step!r}. If your account requires 2FA or new-terms acceptance, "
                    "complete it in the LibreLinkUp app first."
                )
            self.token = ticket["token"]
            self.token_expires = int(ticket.get("expires", 0))
            user_id = (data.get("user") or {}).get("id", "") or ""
            self.account_id = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
            log.info("Logged in to LibreLinkUp (region=%s)", self.region or "global")
            return

        raise LibreLinkUpError("LibreLinkUp login redirected too many times")

    def _ensure_token(self) -> None:
        if not self.token or time.time() > self.token_expires - 60:
            self.login()

    def _get(self, path: str) -> dict:
        self._ensure_token()
        r = self.session.get(f"{self.base_url}{path}", headers=self._headers(), timeout=15)
        if r.status_code == 401:
            # Force re-login once and retry.
            self.token = None
            self._ensure_token()
            r = self.session.get(f"{self.base_url}{path}", headers=self._headers(), timeout=15)
        if r.status_code in (429, 430):
            retry_after = r.headers.get("Retry-After", "?")
            raise LibreLinkUpError(f"Rate limited (HTTP {r.status_code}); retry after {retry_after}")
        r.raise_for_status()
        return r.json()

    def get_connections(self) -> list[dict]:
        return self._get("/llu/connections").get("data", []) or []

    def get_graph(self, patient_id: str) -> dict:
        return self._get(f"/llu/connections/{patient_id}/graph").get("data", {}) or {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_libre_timestamp(ts: str | None) -> str | None:
    """Convert ``"5/21/2022 3:38:50 PM"`` into an ISO-8601 string (UTC)."""
    if not ts:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return ts


def normalize_measurement(m: dict | None) -> dict | None:
    if not m:
        return None
    value = m.get("ValueInMgPerDl")
    if value is None:
        value = m.get("Value")
    return {
        "timestamp": parse_libre_timestamp(m.get("Timestamp")),
        "factory_timestamp": parse_libre_timestamp(m.get("FactoryTimestamp")),
        "value_mgdl": value,
        "value_mmoll": round(value / 18.0182, 2) if isinstance(value, (int, float)) else None,
        "trend_arrow": m.get("TrendArrow"),
        "trend_message": m.get("TrendMessage"),
        "measurement_color": m.get("MeasurementColor"),
        "type": m.get("type"),
    }


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

class App:
    def __init__(self) -> None:
        load_dotenv()

        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        email = os.getenv("LIBRE_EMAIL")
        password = os.getenv("LIBRE_PASSWORD")
        if not email or not password:
            raise SystemExit("LIBRE_EMAIL and LIBRE_PASSWORD must be set in .env")

        self.libre = LibreLinkUp(email, password, os.getenv("LIBRE_REGION"))

        self.base_topic = os.getenv("MQTT_BASE_TOPIC", "cgm").rstrip("/")
        self.topic_last = f"{self.base_topic}/last"
        self.topic_history = f"{self.base_topic}/history"
        self.topic_update = f"{self.base_topic}/update"
        self.topic_status = f"{self.base_topic}/status"

        self.poll_interval = max(15, int(os.getenv("POLL_INTERVAL", "60")))

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()

        client_id = os.getenv("MQTT_CLIENT_ID", "cgm2mqtt")
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        username = os.getenv("MQTT_USERNAME")
        password_mqtt = os.getenv("MQTT_PASSWORD")
        if username:
            self.client.username_pw_set(username, password_mqtt or None)
        if os.getenv("MQTT_TLS", "false").lower() in ("1", "true", "yes"):
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

        self.client.will_set(self.topic_status, payload="offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self.mqtt_host = os.getenv("MQTT_HOST", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))

    # -- MQTT callbacks -----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected to %s:%s", self.mqtt_host, self.mqtt_port)
        client.publish(self.topic_status, "online", qos=1, retain=True)
        client.subscribe(self.topic_update, qos=1)
        log.info("Subscribed to %s", self.topic_update)
        self._wake.set()

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        log.warning("MQTT disconnected; paho will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        if msg.topic == self.topic_update:
            log.info("Update requested via %s (payload=%r)", msg.topic, msg.payload[:64])
            self._wake.set()

    # -- Fetch / publish ----------------------------------------------------

    def _fetch_and_publish(self) -> None:
        with self._lock:
            connections = self.libre.get_connections()
            if not connections:
                log.warning("No LibreLinkUp connections available for this account")
                return

            connection = connections[0]
            patient_id = connection.get("patientId") or connection.get("id")
            if not patient_id:
                raise LibreLinkUpError("Connection has no patientId")

            graph = self.libre.get_graph(patient_id)

            latest = normalize_measurement(connection.get("glucoseMeasurement"))
            history = [normalize_measurement(m) for m in graph.get("graphData", []) or []]
            history = [h for h in history if h]

            patient = {
                "patient_id": patient_id,
                "first_name": connection.get("firstName"),
                "last_name": connection.get("lastName"),
            }

            now_iso = datetime.now(timezone.utc).isoformat()

            last_payload = {
                "fetched_at": now_iso,
                "patient": patient,
                "measurement": latest,
            }
            history_payload = {
                "fetched_at": now_iso,
                "patient": patient,
                "count": len(history),
                "measurements": history,
            }

            self.client.publish(
                self.topic_last,
                json.dumps(last_payload),
                qos=1,
                retain=True,
            )
            self.client.publish(
                self.topic_history,
                json.dumps(history_payload),
                qos=1,
                retain=False,
            )
            log.info(
                "Published %s=%s mg/dL and %d history points",
                self.topic_last,
                latest.get("value_mgdl") if latest else "n/a",
                len(history),
            )

    # -- Loop ---------------------------------------------------------------

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        log.info("Connecting to MQTT broker %s:%s ...", self.mqtt_host, self.mqtt_port)
        self.client.connect_async(self.mqtt_host, self.mqtt_port, keepalive=60)
        self.client.loop_start()

        try:
            while not self._stop.is_set():
                try:
                    self._fetch_and_publish()
                except LibreLinkUpError as exc:
                    log.error("LibreLinkUp error: %s", exc)
                except requests.RequestException as exc:
                    log.error("HTTP error talking to LibreLinkUp: %s", exc)
                except Exception:
                    log.exception("Unexpected error during fetch")

                # Wait until either the poll interval elapses or an update is requested.
                self._wake.wait(timeout=self.poll_interval)
                self._wake.clear()
        finally:
            log.info("Shutting down")
            try:
                self.client.publish(self.topic_status, "offline", qos=1, retain=True).wait_for_publish(timeout=2)
            except Exception:
                pass
            self.client.loop_stop()
            self.client.disconnect()

    def _handle_signal(self, signum, _frame):
        log.info("Received signal %s, stopping", signum)
        self._stop.set()
        self._wake.set()


def main() -> int:
    try:
        App().run()
    except SystemExit:
        raise
    except Exception:
        logging.exception("Fatal error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
