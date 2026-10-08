import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import frappe
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

from cargo.auth import ALGORITHM, CENTRAL_ISSUER, TOKEN_HEADER

SETTINGS = {
	"central_url": "http://central.test",
	"central_webhook_url": "http://central.test/api/method/central.api.state_delivery.receive",
	"central_webhook_enabled": 1,
	"atlas_url": "http://atlas.test",
	"cargo_url": "http://cargo.test",
	"jwks_url": "http://atlas.test/api/atlas/jwks.json",
	"proxy_url": "http://proxy.test",
	"wildcard_domain": "example.test",
	"region_id": 1,
	"region": "test-region",
	"atlas_tenant_id": 0,
	# Release tracking is off unless a test turns it on, whatever ran before it.
	"track_pilot_releases": 0,
	"max_auto_retry_count": 3,
}
SECRETS = {
	"atlas_token": "test-atlas-token",
	"central_webhook_secret": "test-webhook-secret",
	"proxy_token": "test-proxy-token",
}
DATUM_SECRETS = ("datum_user_password", "insights_user_password", "default_user_password")
TEST_ZONE = "example.test"


def use_test_settings() -> None:
	"""Stand Cargo Settings up for one test: a cluster cannot be inserted without them.

	Written inside the test's transaction, so it is rolled back with everything else and no
	site is left holding a made-up token."""
	for field, value in SETTINGS.items():
		frappe.db.set_single_value("Cargo Settings", field, value)

	for field, secret in SECRETS.items():
		set_encrypted_password("Cargo Settings", "Cargo Settings", secret, field)

	frappe.clear_document_cache("Cargo Settings", "Cargo Settings")


def reset_datum_server() -> None:
	"""Clear the datum host between tests: a Single has no row to delete, and its secrets
	live apart."""
	frappe.db.delete("Singles", {"doctype": "Datum Server"})
	for field in DATUM_SECRETS:
		remove_encrypted_password("Datum Server", "Datum Server", field)

	frappe.clear_document_cache("Datum Server", "Datum Server")


def make_dns_zone(domain_name: str = TEST_ZONE, default: bool = True, **fields):
	"""A zone whose records are published by hand, named on Cargo Settings unless told otherwise."""
	if frappe.db.exists("DNS Zone", domain_name):
		zone = frappe.get_doc("DNS Zone", domain_name)
	else:
		zone = frappe.new_doc("DNS Zone")
		zone.domain_name = domain_name
	zone.update({"enabled": 1, "dns_provider": "", **fields})
	zone.flags.skip_dns_provider_verification = True
	zone.save()
	frappe.clear_document_cache("DNS Zone", domain_name)
	if default:
		frappe.db.set_single_value("Cargo Settings", "dns_zone", domain_name)
	return zone


# One key per test process signs as Central and as this region's Atlas: the key id, not the
# key, is what tells Cargo who signed, which is exactly the rule under test.
TEST_KEY = Ed25519PrivateKey.generate()
TEST_KEY_ID = f"{CENTRAL_ISSUER}:test"


def atlas_issuer() -> str:
	return f"atlas:{frappe.db.get_single_value('Cargo Settings', 'region_id')}"


def signed_token(
	scope: str,
	site: str | None = None,
	aud: str | None = None,
	issuer: str = CENTRAL_ISSUER,
	kid: str | None = None,
	expires_in: int = 300,
	**extra,
) -> str:
	"""A token as Central (or, with `issuer=atlas_issuer()`, this region's Atlas) would mint it."""
	region_id = frappe.db.get_single_value("Cargo Settings", "region_id")
	now = datetime.now(UTC)
	claims = {
		"iss": issuer,
		"sub": CENTRAL_ISSUER if issuer == CENTRAL_ISSUER else "atlas",
		"aud": aud or f"atlas-cargo:{region_id}",
		"scope": scope,
		"iat": now,
		"exp": now + timedelta(seconds=expires_in),
		**extra,
	}
	if site:
		claims["site"] = site
	return jwt.encode(claims, TEST_KEY, algorithm=ALGORITHM, headers={"kid": kid or f"{issuer}:test"})


def test_signing_keys() -> list[jwt.PyJWK]:
	raw = TEST_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
	x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
	return [
		jwt.PyJWK({"kty": "OKP", "crv": "Ed25519", "x": x, "kid": kid, "alg": ALGORITHM, "use": "sig"})
		for kid in (TEST_KEY_ID, f"{atlas_issuer()}:test")
	]


@contextmanager
def trusted_test_keys():
	"""Cargo's verifier runs for real against the test key; only the key-set fetch is stood down."""
	client = Mock()
	client.get_signing_keys.return_value = test_signing_keys()
	with patch("cargo.auth.jwks_client", return_value=client):
		yield


@contextmanager
def as_request(token: str | None, request_ip: str | None = None, path: str = "/api/method/test"):
	"""A guest request carrying `token`, the way a site or Central reaches a Cargo endpoint."""
	before = (
		frappe.session.user,
		getattr(frappe.local, "request", None),
		getattr(frappe.local, "request_ip", None),
	)
	frappe.set_user("Guest")
	frappe.local.request = frappe._dict(headers={TOKEN_HEADER: token} if token else {}, path=path)
	frappe.local.request_ip = request_ip
	frappe.local.request_claims = None
	try:
		yield
	finally:
		frappe.set_user(before[0])
		frappe.local.request = before[1]
		frappe.local.request_ip = before[2]
		frappe.local.request_claims = None
