app_name = "cargo"
app_title = "Cargo"
app_publisher = "Aradhya-Tripathi"
app_description = "App to manage and provision all frappe cloud services"
app_email = "developers@frappe.io"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "cargo",
# 		"logo": "/assets/cargo/logo.png",
# 		"title": "Cargo",
# 		"route": "/cargo",
# 		"has_permission": "cargo.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/cargo/css/cargo.css"
# app_include_js = "/assets/cargo/js/cargo.js"

# include js, css files in header of web template
# web_include_css = "/assets/cargo/css/cargo.css"
# web_include_js = "/assets/cargo/js/cargo.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "cargo/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "cargo/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "cargo.utils.jinja_methods",
# 	"filters": "cargo.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "cargo.install.before_install"
after_install = "cargo.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "cargo.uninstall.before_uninstall"
# after_uninstall = "cargo.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "cargo.utils.before_app_install"
# after_app_install = "cargo.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "cargo.utils.before_app_uninstall"
# after_app_uninstall = "cargo.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "cargo.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "cargo.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["cargo.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"cargo.tasks.all"
# 	],
# 	"daily": [
# 		"cargo.tasks.daily"
# 	],
# 	"hourly": [
# 		"cargo.tasks.hourly"
# 	],
# 	"weekly": [
# 		"cargo.tasks.weekly"
# 	],
# 	"monthly": [
# 		"cargo.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "cargo.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "cargo.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "cargo.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "cargo.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["cargo.utils.before_request"]
# after_request = ["cargo.utils.after_request"]

# Job Events
# ----------
# before_job = ["cargo.utils.before_job"]
# after_job = ["cargo.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"cargo.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Whitelisted methods take JSON bodies natively and must annotate every argument, as
# Suite Cloud's did; every Cargo method already does.
use_json_request_body = True
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


scheduler_events = {
	"cron": {
		"* * * * *": [
			"cargo.workflow_engine.doctype.press_workflow.press_workflow.retry_workflows",
			"cargo.workflow_engine.doctype.press_workflow.press_workflow.retry_workflow_callbacks",
			"cargo.workflow_engine.doctype.press_workflow_task.press_workflow_task.retry_tasks",
			"cargo.cargo.doctype.machine.machine.sync_pending_machines",
			"cargo.object_storage.spawn.ensure_cluster",
			"cargo.telemetry.spawn.ensure_telemetry",
			"cargo.postgres.spawn.ensure_postgres",
			"cargo.valkey.spawn.ensure_valkey",
			"cargo.cloud_mail.spawn.ensure_mail",
			"cargo.sfu.spawn.ensure_sfu",
			"cargo.image_builder.doctype.pilot_image.pilot_image.start_image_build_with_latest_pilot_release",
			"cargo.image_builder.doctype.pilot_image.pilot_image.retry_failed_image_types_with_latest_version",
			"cargo.image_builder.doctype.pilot_image.pilot_image.retire_older_images",
			# Machines die without telling anyone, so health is re-read on a clock. A minute
			# is what sets alerting latency; the read is two calls to the gateway.
			"cargo.object_storage.health.refresh_health",
			"cargo.cloud_mail.health.refresh_health",
			"cargo.cloud_mail.cluster.bootstrap.poll_pending",
			"cargo.postgres.health.refresh_health",
			"cargo.valkey.health.refresh_health",
			"cargo.sfu.health.refresh_health",
		],
		# One SSH session per node, so five minutes rather than one. Nothing Garage
		# exports moves meaningfully faster.
		"*/5 * * * *": [
			"cargo.object_storage.health.ship_metrics",
			"cargo.cloud_mail.health.ship_metrics",
		],
	},
	"hourly": [
		"cargo.object_storage.health.prune_history",
		"cargo.cloud_mail.health.prune_history",
		"cargo.postgres.health.prune_history",
		"cargo.valkey.health.prune_history",
		"cargo.sfu.health.prune_history",
		# A customer domain goes live once its records resolve, and a DKIM key the cluster was
		# still generating is picked up on the next pass. A failed lookup never turns one off.
		"cargo.cloud_mail.doctype.mail_domain.mail_domain.refresh_rotating_domains",
		"cargo.cloud_mail.doctype.mail_domain.mail_domain.verify_unverified_domains",
		"cargo.cloud_mail.tenancy.platform.provide_platform_addresses",
		"cargo.cloud_mail.doctype.dmarc_report.dmarc_report.fetch_all_clusters",
		"cargo.cloud_mail.doctype.tls_report.tls_report.fetch_all_clusters",
	],
	"daily": [
		"cargo.backup.backup_database",
		"cargo.postgres.backup.backup_databases",
		"cargo.cargo.doctype.dns_record.dns_record.verify_all_dns_records",
		"cargo.cloud_mail.doctype.stalwart_cluster.stalwart_cluster.check_all_clusters",
		"cargo.cloud_mail.doctype.stalwart_node.stalwart_node.verify_all_ptr_records",
		"cargo.cloud_mail.doctype.egress_ip_pool.egress_ip_pool.verify_all_ptr_records",
		"cargo.cloud_mail.doctype.dmarc_report.dmarc_report.prune_expired_reports",
		"cargo.cloud_mail.doctype.tls_report.tls_report.prune_expired_reports",
		"cargo.cloud_mail.doctype.mail_domain.mail_domain.purge_disabled_domains",
		"cargo.cloud_mail.doctype.mail_domain.mail_domain.reverify_ownership",
	],
}
