// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

const HEADLINES = {
	Draft: __("Add the machine. Atlas builds it, and setting up starts once it boots."),
	"Setting Up": __("Installing Postgres and opening it to the mesh. Follow the Setup Log below."),
	Active: __("Postgres is serving on the mesh. Services get a database each through Postgres Database."),
	Failed: __("The last run failed. See the Setup Log below, then set up again on the same machine."),
};

frappe.ui.form.on("Postgres Server", {
	refresh(frm) {
		if (frm.is_new()) return;
		const guidance = HEADLINES[frm.doc.status];
		if (guidance) frm.dashboard.set_headline(guidance);

		if (!frm.doc.machine && frm.doc.status !== "Setting Up") {
			frm.add_custom_button(__("Machine"), () => ask_for_machine(frm), __("Add")).addClass("btn-primary");
		}
		if (frm.doc.machine && frm.doc.status !== "Setting Up") {
			const first = frm.doc.status === "Draft";
			frm.add_custom_button(first ? __("Set Up Postgres") : __("Set Up Again"), () =>
				frappe.confirm(__("Install Postgres on this machine?"), () =>
					frm.call("setup").then(() => frm.reload_doc())
				)
			).addClass(first ? "btn-primary" : "");
		}
		if (frm.doc.auto_spawn && frm.doc.auto_setup_attempts) {
			frm.add_custom_button(__("Reset Setup Attempts"), () =>
				frm.call("reset_auto_setup_attempts").then(() => frm.reload_doc())
			);
		}
	},
});

function ask_for_machine(frm) {
	frappe.prompt(
		[
			{ fieldname: "cpu_millicores", label: __("CPU (millicores)"), fieldtype: "Int", default: 2000, reqd: 1 },
			{ fieldname: "ram_gb", label: __("RAM (GB)"), fieldtype: "Int", default: 4, reqd: 1 },
			{
				fieldname: "disk_gb",
				label: __("Disk (GB)"),
				description: __("Holds every database the region's services keep here."),
				fieldtype: "Int",
				default: 50,
				reqd: 1,
			},
		],
		(values) => frm.call("create_postgres_node", values).then(() => frm.reload_doc()),
		__("Add Machine"),
		__("Ask Atlas")
	);
}
