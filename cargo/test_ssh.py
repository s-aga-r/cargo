import io
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from cargo.image_builder.doctype.pilot_image.pilot_image import PilotImage
from cargo.ssh import MASK, HostKeyPin, Masker, OutputLog, SshError, live_output, run_over_ssh


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

	output: ClassVar[list[str]] = ["ok\n"]
	returncode = 0

	def __init__(self, args, **kwargs) -> None:
		self.args = args
		self.stdin = io.StringIO()
		self.stdout = iter(self.output)
		hosts = next(a for a in args if a.startswith("UserKnownHostsFile=")).split("=", 1)[1]
		# What ssh would do: with checking on, a key must already be in the file; on first contact
		# the presented key is written to it.
		self.known_hosts = Path(hosts).read_text().strip()
		if self.option("StrictHostKeyChecking") == "yes" and self.known_hosts != PRESENTED:
			self.output = ["Host key verification failed.\n"]
			self.returncode = 255
		with Path(hosts).open("a") as known:
			known.write(f"{PRESENTED}\n")

	def wait(self) -> None:
		pass

	def poll(self):
		return self.returncode  # finished as soon as its output was read

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
		self.assertEqual(ssh.known_hosts, PRESENTED)  # the pin was written for ssh to check against
		self.assertEqual(recorded, [])  # already known; nothing to record

	def test_without_a_pin_nothing_is_kept(self):
		ssh = self.run_ssh(None)

		self.assertEqual(ssh.option("StrictHostKeyChecking"), "accept-new")
		self.assertFalse(Path(ssh.option("UserKnownHostsFile")).exists())


class UnitTestMasking(UnitTestCase):
	"""What a node prints may carry its secrets; nothing Cargo keeps may."""

	def test_every_spelling_of_a_secret_is_hidden(self):
		mask = Masker(['p@ss"word', "short"])
		self.assertEqual(mask('plain p@ss"word here'), f"plain {MASK} here")
		self.assertEqual(mask('json "p@ss\\"word" here'), f'json "{MASK}" here')
		self.assertEqual(mask("shell 'p@ss\"word' here"), f"shell {MASK} here")
		self.assertEqual(mask("short and shorter"), f"{MASK} and {MASK}er")

	def test_output_and_the_error_tail_are_masked(self):
		class Chatty(FakeSsh):
			output: ClassVar[list[str]] = ["export TOKEN=hunter2\n", 'rejected {"secret":"hunter2"}\n']
			returncode = 1

		seen = []
		with patch("cargo.ssh.subprocess.Popen", side_effect=lambda *a, **k: Chatty(*a, **k)):
			with self.assertRaises(SshError) as refused:
				run_over_ssh("fdaa:1::9", "x", "a-key", on_output=seen.append, secrets=["hunter2"])

		self.assertNotIn("hunter2", "".join(seen))
		self.assertNotIn("hunter2", str(refused.exception))
		self.assertIn(MASK, seen[0])
