import io
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from cargo.image_builder.doctype.pilot_image.pilot_image import PilotImage
from cargo.ssh import HostKeyPin, OutputLog, live_output, run_over_ssh


class TestOutputLog(IntegrationTestCase):
	"""How a command's output reaches its document, while it runs and once it ends."""

	def image(self) -> PilotImage:
		with patch.object(PilotImage, "after_insert"):
			image: PilotImage = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": f"v0.0.1-{frappe.generate_hash(length=6)}",
					"frappe_branch": "version-16",
					"image_type": "Base",
				}
			).insert()

		image.db_set("build_log", "the last run's log")
		return image

	def test_a_run_does_not_write_the_document_until_it_ends(self):
		"""A write would lock the row for the whole run, and block a stop request."""
		image = self.image()

		with patch.object(PilotImage, "db_set") as db_set, OutputLog(image, "build_log") as log:
			log.write("step one\n")
			db_set.assert_not_called()

		db_set.assert_called_once_with("build_log", "step one\n", update_modified=False)

	def test_a_new_run_hides_the_last_runs_log(self):
		image = self.image()

		with OutputLog(image, "build_log"):
			self.assertEqual(live_output(image, "build_log"), "")

	def test_a_run_that_prints_nothing_clears_the_last_runs_log(self):
		image = self.image()

		with OutputLog(image, "build_log"):
			pass

		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "build_log"), "")


PRESENTED = "fdaa:1::9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPresentedByTheMachine"


class FakeSsh:
	"""Stands in for the ssh binary: notes the options it was given and writes the host key it
	"saw" into the known-hosts file, as ssh does on first contact."""

	def __init__(self, args, **kwargs) -> None:
		self.args = args
		self.stdin = io.StringIO()
		self.stdout = iter(["ok\n"])
		self.returncode = 0
		hosts = next(a for a in args if a.startswith("UserKnownHostsFile=")).split("=", 1)[1]
		with Path(hosts).open("a") as known:
			known.write(f"{PRESENTED}\n")

	def wait(self) -> None:
		pass

	def kill(self) -> None:
		pass

	def option(self, name: str) -> str:
		return next(a for a in self.args if a.startswith(f"{name}=")).split("=", 1)[1]


class UnitTestHostKeyPinning(UnitTestCase):
	"""A machine is recognised by the key it first answered with."""

	def run_ssh(self, pin: HostKeyPin | None) -> FakeSsh:
		seen = []
		with patch(
			"cargo.ssh.subprocess.Popen",
			side_effect=lambda *a, **k: seen.append(FakeSsh(*a, **k)) or seen[-1],
		):
			run_over_ssh("fdaa:1::9", "uptime", "a-key", pin=pin)
		return seen[0]

	def test_first_contact_records_the_key_the_machine_presented(self):
		recorded = []
		ssh = self.run_ssh(HostKeyPin(None, recorded.append))

		self.assertEqual(ssh.option("StrictHostKeyChecking"), "accept-new")
		self.assertEqual(recorded, [PRESENTED])

	def test_a_pinned_key_is_the_only_one_accepted(self):
		recorded = []
		ssh = self.run_ssh(HostKeyPin(PRESENTED, recorded.append))

		self.assertEqual(ssh.option("StrictHostKeyChecking"), "yes")
		self.assertEqual(recorded, [])  # already known; nothing to record

	def test_without_a_pin_nothing_is_kept(self):
		ssh = self.run_ssh(None)

		self.assertEqual(ssh.option("StrictHostKeyChecking"), "accept-new")
		self.assertFalse(Path(ssh.option("UserKnownHostsFile")).exists())
