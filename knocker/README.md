# tf2-knocker

Wake-on-knock proxy for the TF2 server. A tiny always-on pod that sits
behind the same Kubernetes Service selector as the real server:

- While the server is scaled to zero, the knocker reports ready and
  receives the game port traffic. It answers A2S_INFO queries itself
  with a fake "sleeping" server listing (so the server browser and
  tf2-web keep working without waking anything), and scales the `tf2`
  deployment 0→1 only on genuine connect traffic.
- Once the server is up, the knocker reports not-ready (the Service
  endpoints flip back to the real server) and polls it over A2S,
  scaling 1→0 after `IDLE_MINUTES` (default 30) of consecutive
  zero-player readings.

The fake server name contains the marker `(sleeping` — `web/` keys off
that exact substring to show its "server asleep" banner, so changing
the name in `fake_info_response()` means updating
`web/routes/index.js` in the same change.

Stdlib only; talks to the Kubernetes API directly with the pod's
service account token. Deployment manifests live in `fluv/kube` under
`tf2-knocker/`.

Published as `ghcr.io/fluv/tf2-knocker` by
`.github/workflows/knocker-image.yml`.
