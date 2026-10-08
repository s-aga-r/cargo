"""The surface the Suite app's client depends on: which methods exist, that every one of them
demands a token, and the exception names the client switches on. A change here is a change to
the Suite app too."""

import importlib

import frappe
from frappe.tests import IntegrationTestCase

from cargo.cloud_mail.api import site as site_api
from cargo.testing import as_request

MODULES = (
	"site",
	"mail.accounts",
	"mail.dmarc",
	"mail.domains",
	"mail.groups",
	"mail.mailing_lists",
	"mail.meta",
	"mail.tls",
)

# Everything served under cargo.cloud_mail.api. Adding or removing one is deliberate.
METHODS = frozenset(
	{
		"site.ping",
		"site.update_site_profile",
		"mail.accounts.add_alias",
		"mail.accounts.create_account",
		"mail.accounts.create_app_password",
		"mail.accounts.delete_account",
		"mail.accounts.get_account",
		"mail.accounts.get_quotas",
		"mail.accounts.list_accounts",
		"mail.accounts.remove_alias",
		"mail.accounts.rotate_app_password",
		"mail.accounts.set_account_enabled",
		"mail.accounts.set_alias_enabled",
		"mail.accounts.set_aliases",
		"mail.accounts.set_groups",
		"mail.accounts.set_password",
		"mail.accounts.update_account",
		"mail.dmarc.get_dmarc_report",
		"mail.dmarc.get_dmarc_summary",
		"mail.dmarc.list_dmarc_reports",
		"mail.domains.check_domain",
		"mail.domains.create_domain",
		"mail.domains.delete_domain",
		"mail.domains.get_dns_records",
		"mail.domains.get_domain",
		"mail.domains.list_domains",
		"mail.domains.refresh_dns_records",
		"mail.domains.update_domain",
		"mail.domains.verify_dns_records",
		"mail.groups.add_group_alias",
		"mail.groups.create_group",
		"mail.groups.delete_group",
		"mail.groups.get_group",
		"mail.groups.list_groups",
		"mail.groups.remove_group_alias",
		"mail.groups.set_group_alias_enabled",
		"mail.groups.set_group_aliases",
		"mail.groups.set_group_members",
		"mail.groups.update_group",
		"mail.mailing_lists.add_mailing_list_alias",
		"mail.mailing_lists.add_recipients",
		"mail.mailing_lists.create_mailing_list",
		"mail.mailing_lists.delete_mailing_list",
		"mail.mailing_lists.get_mailing_list",
		"mail.mailing_lists.list_mailing_lists",
		"mail.mailing_lists.list_recipients",
		"mail.mailing_lists.remove_mailing_list_alias",
		"mail.mailing_lists.remove_recipients",
		"mail.mailing_lists.set_mailing_list_alias_enabled",
		"mail.mailing_lists.set_mailing_list_aliases",
		"mail.mailing_lists.set_recipients",
		"mail.mailing_lists.update_mailing_list",
		"mail.meta.get_account_options",
		"mail.tls.get_tls_report",
		"mail.tls.get_tls_summary",
		"mail.tls.list_tls_reports",
	}
)

# What the Suite app's client (suite/mail/suite_cloud) calls today.
CLIENT_METHODS = frozenset(
	{
		"site.ping",
		"site.update_site_profile",
		"mail.accounts.create_account",
		"mail.accounts.delete_account",
		"mail.accounts.get_account",
		"mail.accounts.get_quotas",
		"mail.accounts.list_accounts",
		"mail.accounts.set_account_enabled",
		"mail.accounts.set_groups",
		"mail.accounts.set_password",
		"mail.accounts.update_account",
		"mail.dmarc.get_dmarc_report",
		"mail.dmarc.get_dmarc_summary",
		"mail.dmarc.list_dmarc_reports",
		"mail.domains.check_domain",
		"mail.domains.create_domain",
		"mail.domains.delete_domain",
		"mail.domains.get_dns_records",
		"mail.domains.get_domain",
		"mail.domains.list_domains",
		"mail.domains.update_domain",
		"mail.domains.verify_dns_records",
		"mail.groups.add_group_alias",
		"mail.groups.create_group",
		"mail.groups.delete_group",
		"mail.groups.get_group",
		"mail.groups.list_groups",
		"mail.groups.set_group_alias_enabled",
		"mail.groups.set_group_members",
		"mail.groups.update_group",
		"mail.mailing_lists.add_recipients",
		"mail.mailing_lists.create_mailing_list",
		"mail.mailing_lists.delete_mailing_list",
		"mail.mailing_lists.get_mailing_list",
		"mail.mailing_lists.list_mailing_lists",
		"mail.mailing_lists.list_recipients",
		"mail.mailing_lists.remove_recipients",
		"mail.mailing_lists.update_mailing_list",
		"mail.meta.get_account_options",
		"mail.tls.get_tls_report",
		"mail.tls.get_tls_summary",
		"mail.tls.list_tls_reports",
	}
)

# The `exc_type` names a client reads off an error response, and the status each carries.
EXCEPTIONS = {
	"SiteAuthError": (frappe.AuthenticationError, 401),
	"SiteSuspendedError": (frappe.PermissionError, 403),
	"StalwartRejected": (frappe.ValidationError, 422),
	"ClusterMisconfiguredError": (frappe.ValidationError, 502),
}


def served() -> dict[str, object]:
	methods = {}
	for module_name in MODULES:
		module = importlib.import_module(f"cargo.cloud_mail.api.{module_name}")
		for name, value in vars(module).items():
			if not callable(value) or getattr(value, "__module__", None) != module.__name__:
				continue
			try:
				frappe.is_whitelisted(value)
			except Exception:
				continue
			methods[f"{module_name}.{name}"] = value
	return methods


class TestApiContract(IntegrationTestCase):
	def test_the_served_methods_are_exactly_the_pinned_ones(self) -> None:
		self.assertEqual(set(served()), set(METHODS))

	def test_everything_the_suite_client_calls_is_served(self) -> None:
		self.assertEqual(CLIENT_METHODS - METHODS, set())

	def test_every_method_demands_a_token_before_reading_anything(self) -> None:
		for path, method in served().items():
			with self.subTest(path), as_request(None), self.assertRaises(frappe.AuthenticationError):
				method()

	def test_the_exception_names_a_client_switches_on_are_kept(self) -> None:
		for name, (base, status) in EXCEPTIONS.items():
			exception = getattr(site_api, name)
			self.assertTrue(issubclass(exception, base), name)
			self.assertEqual(exception.http_status_code, status, name)
