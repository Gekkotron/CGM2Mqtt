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

Recommended for running the bridge permanently on a home server. Run these
commands from the repository root (the folder containing `docker-compose.yml`):

```bash
git clone https://github.com/Gekkotron/CGM2Mqtt.git
cd CGM2Mqtt
cp .env.example .env
$EDITOR .env
docker compose up -d --build
docker compose logs -f
```

The container reads its configuration from `.env` (via `env_file`) and restarts
automatically (`restart: unless-stopped`), including after a reboot.

Inside the container, `localhost` refers to the container itself:

- Broker on the Docker host: set `MQTT_HOST=host.docker.internal`.
- Broker in another container: put both on the same Docker network and use the
  broker's service name.
- Broker elsewhere on the LAN: use its IP or hostname.

### Day-to-day commands

```bash
docker compose ps                # status
docker compose logs -f           # follow logs
docker compose restart           # restart (e.g. after editing .env)
docker compose down              # stop and remove the container
git pull && docker compose up -d --build   # update to the latest version
```

### Migrating from a bare Python install

Stop the old process first, otherwise two bridges publish to the same topics:

```bash
pgrep -af cgm2mqtt               # find it
pkill -f cgm2mqtt.py             # SIGTERM: publishes "offline" and exits cleanly
```

If it was started by systemd, stop it with
`sudo systemctl stop <service> && sudo systemctl disable <service>` instead,
or it will be restarted. Remove any `@reboot` line from `crontab -e` too.

### Without compose

```bash
docker build -t cgm2mqtt .
docker run -d --name cgm2mqtt --restart unless-stopped \
  --env-file .env --add-host host.docker.internal:host-gateway cgm2mqtt
```

### Troubleshooting

- **`no configuration file provided: not found`**: you are not in the
  repository root, or your checkout predates the Docker files. `cd` into the
  repo and `git pull`.
- **`git pull` complains a `refs/heads/...` reference was not fetched**: your
  local branch tracks a remote branch that has been deleted. Point it at `main`:
  `git fetch origin && git switch main && git branch -u origin/main && git pull`.

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
