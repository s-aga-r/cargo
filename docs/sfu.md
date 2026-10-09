# SFU

## Purpose

Each region runs one mediasoup SFU for Frappe Meet, and Cargo owns it. Sites in the region sign their room tokens with its secret and send browsers to its public name. The software is the Suite project's own: its Docker deployment, with nginx and a Let's Encrypt certificate in front, installed from a pinned ref of `frappe/suite`.

## Records

An **SFU Server** is the region's one SFU, a Single in the same shape as the Postgres and Valkey servers: a machine with a public IPv4 from Atlas, a Status for the build lifecycle, a Health for how it is doing, the Suite ref and image installed, the number of mediasoup workers and the first media port, and two secrets Cargo mints: the JWT secret sites sign with, and a metrics token.

Its hostname is `sfu.<zone>`, under the DNS Zone named on Cargo Settings. When the machine runs, its public address becomes the hostname's A record in that zone, which the certificate authority resolves. Browsers reach the machine directly, never through the Proxy: media is UDP.

## How it is built

The machine is asked of Atlas with a public IPv4 and a firewall that opens TCP 80 and 443 to everyone, one UDP port per worker from the media port on, and everything to the mesh. The install script puts Docker on the machine from Docker's own repository, runs the Suite project's installer from the pinned ref into `/opt/meet-sfu`, writes the deployment's environment whole, with the announced IP, the domain, the secrets and the worker count, and runs the deployment's own `setup`, which pulls the image, provisions the certificate and starts everything. It then waits for `/health` on the machine. Re-running is how a failed run is retried.

## Auto spawn

```json
"default_sfu_config": {
  "sfu": {"cpu_millicores": 4000, "ram_gb": 8, "disk_gb": 40},
  "ssl_email": "ops@example.com",
  "workers": 4
}
```

The machine size and `ssl_email` are required; `suite_ref`, `image`, `workers` and `media_port` fall back to the record's defaults. `ensure_sfu` waits for an enabled DNS Zone on Cargo Settings, claims the empty Single, rents the machine, waits for its public address, sets the server up, and retries a failed bring-up up to three times.

## Sites

Central fetches `sfu_server_url` and `sfu_secret` from `cargo.sfu.api.get_credential` with an `sfu:*` token, and puts them in a site's config the way it does the mail credential. A site's own token never carries that scope. The Suite app reads both keys as it does today.

## Health

Every minute Cargo asks `https://<hostname>/health`, as a browser would. Anything but a 2xx is Critical. Readings go to `logs/sfu_health.json.log`. The SFU exports Prometheus metrics behind its metrics token; shipping them to datum is not wired yet.

## Not here

Captions need a separate speech-to-text server, which this does not run; the deployment keeps the Suite default for `STT_SERVER_URL`. Log shipping to the Suite project's Alloy and Loki is off. The deployment pulls a `latest` image unless `image` pins one.
