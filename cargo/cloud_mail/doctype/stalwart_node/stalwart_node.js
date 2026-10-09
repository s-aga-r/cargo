// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Stalwart Node', {
	refresh(frm) {
		if (frm.doc.__islocal) return

		if (frm.doc.status === 'Provisioned') {
			frm.add_custom_button(__('Check Health'), () => frm.events.call(frm, 'check_health', __('Checking...')), __('Actions'))
		}
		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Drain'), () => frm.events.call(frm, 'drain', __('Removing from ingress DNS...')), __('Actions'))
		}
		if (frm.doc.status === 'Draining') {
			frm.add_custom_button(__('Restore'), () => frm.events.call(frm, 'restore', __('Adding to ingress DNS...')), __('Actions'))
		}
		frm.add_custom_button(__('Verify PTR'), () => frm.events.call(frm, 'verify_ptr', __('Resolving...')), __('Actions'))
		if (frm.doc.machine && ['Pending', 'Failed', 'Disabled'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Release Machine'), () => frappe.confirm(__('Let this machine go? Atlas terminates it if it still runs; the node keeps its name and asks for another.'), () => frm.events.call(frm, 'release_machine', __('Releasing...'))), __('Actions'))
		}
	},

	call(frm, method, freeze_message) {
		frappe.call({
			doc: frm.doc,
			method,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (!r.exc) frm.reload_doc()
			},
		})
	},
})
