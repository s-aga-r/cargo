# Smoke check for a real region

`tools/mail-smoke/check.sh <cluster> [--phase 3|6|7]` is what the plan's phases 3b, 6 and 7 end on. It asks Cargo (`cargo.cloud_mail.smoke`) whether the cluster is Active and Healthy with nothing drifted, the directory in step, every node with reverse DNS and in the ingress record, the platform domain verified and addresses issued, the default certificate covering the hostname and JMAP answering Cargo's key; then it asks a public resolver for the A, MX, SPF chain, DMARC and DKIM records and checks the certificate presented on 443, 465, 993, 587 and 25 and the 401 JMAP gives a stranger. Phase 6 adds three serving nodes on one version after a completed rolling upgrade and a serving gateway; phase 7 adds required grants, a site's verified domain and no `frappemail-*` selector anywhere.

Three checks need a person and are printed as such: one message to an external mailbox arriving with SPF, DKIM and DMARC passing, an unknown local part being rejected, and a plain site sending from its platform credential.

Needs `dig`, `openssl` and `curl` on the host running it, and a bench with `SITE`. It changes nothing.
