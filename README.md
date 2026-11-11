# CGM2Mqtt

Bridge that publishes glucose data from Abbott's **FreeStyle LibreLinkUp**
sharing service to an MQTT broker.

Topics (relative to `MQTT_BASE_TOPIC`, default `cgm`):

| Topic          | Direction | Retained | Description                                     |
|----------------|-----------|----------|-------------------------------------------------|
| `cgm/last`     | publish   | yes      | Latest glucose measurement                      |
| `cgm/history`  | publish   | no       | Full graph history on each fetch (~8 hours)     |
| `cgm/update`   | subscribe | -        | Publish anything here to trigger a fresh fetch  |
| `cgm/status`   | publish   | yes      | `online` / `offline` (uses MQTT LWT)            |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env
python cgm2mqtt.py
```

## Docker

```bash
cp .env.example .env
$EDITOR .env
docker compose up -d --build
docker compose logs -f
```

The container reads its configuration from `.env` (via `env_file`). Inside the
container, `localhost` refers to the container itself, so if the broker runs on
the Docker host set `MQTT_HOST=host.docker.internal`. If the broker is another
container, put both on the same Docker network and use its service name.

Without compose:

```bash
docker build -t cgm2mqtt .
docker run -d --name cgm2mqtt --restart unless-stopped \
  --env-file .env --add-host host.docker.internal:host-gateway cgm2mqtt
```

## Configuration

All configuration lives in `.env`. See `.env.example` for the full list. The
required keys are `LIBRE_EMAIL`, `LIBRE_PASSWORD`, and `MQTT_HOST`.

Set `MQTT_USERNAME` / `MQTT_PASSWORD` if your broker requires auth (leave
blank for anonymous brokers). Set `MQTT_TLS=true` to connect over TLS.

The LibreLinkUp account must already be set up in the LibreLinkUp mobile app
and must have at least one accepted patient sharing connection. If your
account requires 2FA or new terms-of-use acceptance, complete that in the app
first - the server will reject token-less login attempts otherwise.

## Example payload (`cgm/last`)

```json
{
  "fetched_at": "2026-05-08T12:34:56+00:00",
  "patient": {
    "patient_id": "...",
    "first_name": "Jane",
    "last_name": "Doe"
  },
  "measurement": {
    "timestamp": "2026-05-08T12:34:00+00:00",
    "factory_timestamp": "2026-05-08T12:34:00+00:00",
    "value_mgdl": 117,
    "value_mmoll": 6.49,
    "trend_arrow": 3,
    "trend_message": null,
    "measurement_color": 1,
    "type": 1
  }
}
```

## Triggering a fetch on demand

```bash
mosquitto_pub -h localhost -t cgm/update -n
```

The bridge will fetch immediately and publish to `cgm/last` and `cgm/history`.

## Notes

- API based on the community-documented LibreLinkUp endpoints
  (<https://gist.github.com/khskekec/6c13ba01b10d3018d816706a32ae8ab2>).
- The bridge auto-detects the regional API endpoint when the global endpoint
  responds with a redirect, or you can pin it with `LIBRE_REGION=eu`.
- Tokens are cached in memory and refreshed automatically when they expire.

## License

Provided as-is for personal use.
