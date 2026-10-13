import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from tools.fake_atlas.fake_atlas import (
	FAKE_PUBLIC_IPV4,
	Handler,
	attach_public_ip,
	detach_public_ip,
	network_payload,
)

HEADERS = {"Authorization": "Bearer test", "X-Tenant-ID": "1"}


class TestFakeAtlas(unittest.TestCase):
	def setUp(self):
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()

	def tearDown(self):
		self.server.shutdown()
		self.server.server_close()
		self.thread.join()

	def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
		connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
		self.addCleanup(connection.close)
		connection.request(method, path, body=json.dumps(body) if body else None, headers=HEADERS)
		response = connection.getresponse()
		raw = response.read()

		return response.status, json.loads(raw) if raw else None

	def test_tagged_system_image_request_returns_the_base_image(self):
		status, payload = self.request(
			"GET", "/api/atlas/images?image_type=system&tag=purpose:base,os:Ubuntu,os_version:24.04&limit=100"
		)

		self.assertEqual(status, 200)
		self.assertEqual(payload["items"][0]["id"], "ubuntu-24.04")

	def test_a_public_address_is_reported_only_when_asked_for_and_running(self):
		pending = {"state": "pending", "address": "cargo-vm1", "public_ipv4": "ip-1"}
		self.assertIsNone(network_payload(pending)["public_ipv4"])
		self.assertEqual(network_payload({**pending, "state": "running"})["public_ipv4"], FAKE_PUBLIC_IPV4)
		self.assertIsNone(
			network_payload({**pending, "state": "running", "public_ipv4": None})["public_ipv4"]
		)
		self.assertEqual(network_payload(pending)["mesh_ipv6"], "cargo-vm1")

	def test_a_reserved_address_is_released_only_once_its_machine_lets_go(self):
		status, reserved = self.request("POST", "/api/atlas/public-ips", {"version": 4})
		self.assertEqual((status, reserved["status"], reserved["version"]), (201, "reserved", 4))
		path = f"/api/atlas/public-ips/{reserved['id']}"

		attach_public_ip(reserved["id"], "vm-1")
		status, refused = self.request("DELETE", path)
		self.assertEqual((status, refused["error"]["code"]), (409, "public_ip_in_use"))
		self.assertEqual(self.request("GET", path)[1]["virtual_machine_id"], "vm-1")

		detach_public_ip("vm-1")
		self.assertEqual(self.request("DELETE", path)[0], 204)
		self.assertEqual(self.request("DELETE", path)[0], 404)

	def test_a_machine_attaches_only_an_address_that_was_reserved(self):
		with self.assertRaises(KeyError):
			attach_public_ip("never-reserved", "vm-1")


if __name__ == "__main__":
	unittest.main()
