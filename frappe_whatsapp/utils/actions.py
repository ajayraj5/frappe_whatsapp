import frappe
from werkzeug.wrappers import Response
from frappe_whatsapp.utils.webhook import send_and_log_whatsapp_message

from frappe.website.utils import is_signup_disabled
from frappe import _
from frappe.utils import (
	escape_html
)



def user_number_validation(doc, method):
    try:
        frappe.log_error("user", doc)
        user = None
        if doc.phone:
            user = frappe.db.get_value("User", {
                    "phone": doc.phone
                }, ["name"]) or frappe.db.get_value("User", {
                    "mobile_no": doc.phone
                }, ["name"])
        
        if user and user != doc.name:
            raise Exception(f"{doc.phone} is already used by some other user.")

        if doc.mobile_no:
            user = frappe.db.get_value("User", {
                    "mobile_no": doc.mobile_no
                }, ["name"]) or frappe.db.get_value("User", {
                    "mobile_no": doc.mobile_no
                }, ["name"])
        
        if user and user != doc.name:
            raise Exception(f"{doc.mobile_no} is already used by some other user.")


 
    except Exception as e:
        # frappe.log_error("User Number Validation Event Error:", e);
        frappe.throw(
                msg=f'{e}',
                title='Duplicate Phone No. Error',
            )



@frappe.whitelist(allow_guest=True)
def sign_up(email: str, full_name: str, phone: str) -> tuple[int, str]:
	if is_signup_disabled():
		send_and_log_whatsapp_message(phone, _("Sign Up is disabled"))

	user = frappe.db.get("User", {"email": email})
	if user:
		if user.enabled:
			send_and_log_whatsapp_message(phone, _("Already Registered"))
			return 0
		else:
			send_and_log_whatsapp_message(phone, _("Registered but disabled"))
			return 0
	else:
		if frappe.db.get_creation_count("User", 60) > 300:
			message_body = (
				_("Temporarily Disabled"),
				_(
					"Too many users signed up recently, so the registration is disabled. Please try back in an hour"
				))
			send_and_log_whatsapp_message(phone, message_body)

		from frappe.utils import random_string

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": escape_html(full_name),
				"enabled": 1,
				"new_password": random_string(10),
				"user_type": "Website User",
				"phone":escape_html(phone)
			}
		)
		user.flags.ignore_permissions = True
		user.flags.ignore_password_policy = True
		user.insert()

		# set default signup role as per Portal Settings
		default_role = frappe.db.get_single_value("Portal Settings", "default_role")
		if default_role:
			user.add_roles(default_role)

		if user.flags.email_sent:
			message_body =  _("Please check your email for verification")
			send_and_log_whatsapp_message(phone, message_body)
			return 1
		else:
			message_body =  _("Please ask your administrator to verify your sign-up")
			send_and_log_whatsapp_message(phone, message_body)
			return 2




def log_error_with_user_info(title, message, user=None):
    # Determine the user to log (manual input or session user)
    user = user or frappe.session.user
    user_info = f"Logged by: {user}"
    
    # Combine user information with the message
    enhanced_message = f"{user_info}\n\n{message}"
    
    # Log the error with the title and enhanced message
    frappe.log_error(title=title, message=enhanced_message)