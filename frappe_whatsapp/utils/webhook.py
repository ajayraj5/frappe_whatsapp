import frappe
import json
import requests
from werkzeug.wrappers import Response 
import frappe.utils
import re
import ast
from frappe.utils import get_url
from frappe.desk.reportview import get_count

@frappe.whitelist(allow_guest=True)
def webhook():
    """Meta webhook."""
    if frappe.request.method == "GET":
        return get()
    return post()

def get():
    """Get."""
    hub_challenge = frappe.form_dict.get("hub.challenge")
    webhook_verify_token = frappe.db.get_single_value("WhatsApp Settings", "webhook_verify_token")

    if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
        frappe.throw("Verify token does not match")

    return Response(hub_challenge, status=200)

def post():
    """Post."""
    data = frappe.local.form_dict
    frappe.get_doc({
        "doctype": "WhatsApp Notification Log",
        "template": "Webhook",
        "meta_data": json.dumps(data)
    }).insert(ignore_permissions=True)

    messages = []
    try:
        messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
    except KeyError:
        messages = data["entry"]["changes"][0]["value"].get("messages", [])

    if messages:
        for message in messages:
            frappe.log_error("UserMessage", message)

            message_type = message['type']
            is_reply = True if message.get('context') else False
            reply_to_message_id = message['context']['id'] if is_reply else None

            text = message.get('text', {}).get('body', '').strip()
            parts = text.split(' ')
            command = parts[0] if len(parts) > 0 else None

            if command and command.lower() == "signup" and len(parts) >= 2:
                handle_signup_command(text, message['from'])
                return

            user = is_valid_user(message['from'])

            if not user:
                send_and_log_whatsapp_message(
                    message['from'],
                    "👋 Hi there! It seems your number is not registered in our system. To get started, please create an account with us. 🌟\n\nYou can sign up by providing the following details:\n\n📧 Email: mail@gmail.com\n📝 Full Name: YourName\n\nWe’re here to help if you have any questions! 😊"
                )

                send_and_log_whatsapp_message(
                    message['from'], "signup {\"email\": \"mail@gmail.com\",\"full_name\": \"YourName\"}\n\n🌟 Replace the placeholders with your correct details and send it back to us to complete your sign-up. 😊"
                )

                return

            frappe.set_user(user)

            if message_type == 'text':
                frappe.get_doc({
                    "doctype": "WhatsApp Message",
                    "type": "Incoming",
                    "from": message['from'],
                    "message": message['text']['body'],
                    "message_id": message['id'],
                    "reply_to_message_id": reply_to_message_id,
                    "is_reply": is_reply,
                    "content_type": message_type,
                }).insert(ignore_permissions=True)

                handle_whatsapp_message(message, user)

            elif message_type == 'reaction':
                frappe.get_doc({
                    "doctype": "WhatsApp Message",
                    "type": "Incoming",
                    "from": message['from'],
                    "message": message['reaction']['emoji'],
                    "reply_to_message_id": message['reaction']['message_id'],
                    "message_id": message['id'],
                    "content_type": "reaction"
                }).insert(ignore_permissions=True)

            elif message_type == 'location':
                location = frappe.get_doc({
                    "doctype": "Location",
                    "location_name": message['timestamp'],
                    "custom_user_phone_number": message['from'],
                    "latitude": message['location']['latitude'],
                    "longitude": message['location']['longitude']
                })

                location.insert(ignore_permissions=True)

                frappe.get_doc({
                    "doctype": "WhatsApp Message",
                    "type": "Incoming",
                    "from": message['from'],
                    "message_id": message['id'],
                    "message": message[message_type].get(message_type),
                    "content_type": message_type,
                    "reference_doctype": "Location",
                    "reference_name": location,
                }).insert(ignore_permissions=True)

                latest_outgoing_message = frappe.get_all(
                    "WhatsApp Message",
                    filters={
                        "type": "Outgoing",
                        "content_type": 'text',
                        "to": message['from'],
                        "owner": user
                    },
                    fields=["name", "message", "creation"],
                    order_by="creation desc",
                    limit_page_length=1
                )

                if latest_outgoing_message:
                    frappe.log_error("latest_outgoing_message", f"Latest message: {latest_outgoing_message[0].message}")
                    if "please select your location" in latest_outgoing_message[0].message.lower():
                        so = latest_outgoing_message[0].message.split(" ")[0]
                        ask_for_the_payment(message['from'], so)
                else:
                    frappe.msgprint("No outgoing messages found.")

            elif message_type == 'interactive':
                try:
                    frappe.log_error("Interactive message saving", message)
                    frappe.get_doc({
                        "doctype": "WhatsApp Message",
                        "type": "Incoming",
                        "from": message['from'],
                        "message": str(message['interactive']),
                        "message_id": message['id'],
                        "content_type": "interactive"
                    }).insert(ignore_permissions=True)

                    user_interactive_msg = message['interactive']
                    if user_interactive_msg['type'] == 'list_reply':
                        actions_details = parse_interactive_message(user_interactive_msg['list_reply']['id'])
                        frappe.log_error("actions_details", actions_details)

                        if user_interactive_msg['list_reply']['title'] == 'Show Items':
                            handle_list_response_show_items(message['from'])

                        elif user_interactive_msg['list_reply']['title'] == 'Category Wise':
                            send_item_group_message(message['from'])

                        elif actions_details and actions_details[0]['selection'] == "Item Group":
                            handle_show_item_command(message['from'], user, user_interactive_msg['list_reply']['title'])

                    elif user_interactive_msg['type'] == 'button_reply':
                        actions_details = parse_interactive_message(user_interactive_msg['button_reply']['id'])

                        if user_interactive_msg['button_reply']['id'].startswith("so_confirm_yes"):
                            send_and_log_whatsapp_message(message['from'], "Thank You For Ordering 🥹😃")
                            ask_for_the_address(message['from'], actions_details[0])

                        elif user_interactive_msg['button_reply']['id'].startswith("so_confirm_no"):
                            send_and_log_whatsapp_message(message['from'], "Why you are not ordering ?? Any Issue from our side 😟 ? \n\n This is Last chance to purchase it so be fast else someone else will purchase :)")
                            
                        elif user_interactive_msg['button_reply']['id'].startswith("cod"):
                            from erpnext.erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

                            response = make_sales_invoice(actions_details[0]['order_id'], ignore_permissions=True)

                            sales_invoice = frappe.get_doc(response['message'])
                            sales_invoice.insert(ignore_permissions=True)

                            send_and_log_whatsapp_message(message['from'], "Cash On Delivery Selected !!")
                        elif user_interactive_msg['button_reply']['id'].startswith("pay_now"):
                            send_and_log_whatsapp_message(message['from'], "Pay NOW Selected !!")
                        elif user_interactive_msg['button_reply']['title'] == "Show More":
                            handle_show_item_command(message['from'], user, actions_details[0]['item_group'], None)

                except Exception as e:
                    frappe.log_error("Incoming message interactive error", str(e))

            elif message_type in ["image", "audio", "video", "document"]:
                settings = frappe.get_doc("WhatsApp Settings", "WhatsApp Settings")
                token = settings.get_password("token")
                url = f"{settings.url}/{settings.version}/"

                media_id = message[message_type]["id"]
                headers = {
                    'Authorization': 'Bearer ' + token
                }
                response = requests.get(f'{url}{media_id}/', headers=headers)

                if response.status_code == 200:
                    media_data = response.json()
                    media_url = media_data.get("url")
                    mime_type = media_data.get("mime_type")
                    file_extension = mime_type.split('/')[1]

                    media_response = requests.get(media_url, headers=headers)
                    if media_response.status_code == 200:
                        file_data = media_response.content
                        file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

                        message_doc = frappe.get_doc({
                            "doctype": "WhatsApp Message",
                            "type": "Incoming",
                            "from": message['from'],
                            "message_id": message['id'],
                            "reply_to_message_id": reply_to_message_id,
                            "is_reply": is_reply,
                            "message": message[message_type].get("caption", f"/files/{file_name}"),
                            "content_type": message_type
                        }).insert(ignore_permissions=True)

                        file = frappe.get_doc({
                            "doctype": "File",
                            "file_name": file_name,
                            "attached_to_doctype": "WhatsApp Message",
                            "attached_to_name": message_doc.name,
                            "content": file_data,
                            "attached_to_field": "attach"
                        }).save(ignore_permissions=True)

                        message_doc.attach = file.file_url
                        message_doc.save()
            else:
                frappe.get_doc({
                    "doctype": "WhatsApp Message",
                    "type": "Incoming",
                    "from": message['from'],
                    "message_id": message['id'],
                    "message": message[message_type].get(message_type),
                    "content_type": message_type
                }).insert(ignore_permissions=True)

def update_status(data):
    if data.get("field") == "message_template_status_update":
        update_template_status(data['value'])

    elif data.get("field") == "messages":
        update_message_status(data['value'])


def update_template_status(data):
    """Update template status."""
    frappe.db.sql(
        """UPDATE `tabWhatsApp Templates`
        SET status = %(event)s
        WHERE id = %(message_template_id)s""",
        data
    )

def update_message_status(data):
    """Update message status."""
    id = data['statuses'][0]['id']
    status = data['statuses'][0]['status']
    conversation = data['statuses'][0].get('conversation', {}).get('id')
    name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

    doc = frappe.get_doc("WhatsApp Message", name)
    doc.status = status
    if conversation:
        doc.conversation_id = conversation
    doc.save(ignore_permissions=True)



def handle_show_command_1(text, recipient, user):
    """
    Handle 'Show' command.
    """
    try:
        parts = text.split(' ')
        frappe.log_error("parts",parts)
        if len(parts) >= 2:
            doctype = parts[1].title()
            docname = parts[2] if len(parts) >= 3 else None

            if not check_doctype_permissions(user, doctype):
                return "No permissions for this document type."

            
            if docname and len(docname) > 0:
                # Fetch specific record
                # data = frappe.get_doc(doctype, docname, fields=["name"])
                data = frappe.db.get_value(doctype, docname, ["name"], as_dict=True)
                return f"Record Details:\n{frappe.as_json(data)}"
            else:
                # Fetch all records
                # data = frappe.get_all(doctype, fields=["name"], filters={'owner': 'ajaymahi545@gmail.com'})
                data = frappe.get_list(doctype, user=user,ignore_permissions=False)

                # names = "\n".join([d['name'] for d in data])
                return f"Records in {doctype}:\n{data}"
        else:
            return "Invalid command format for 'Show'."
        
        # send_and_log_whatsapp_message(recipient, message_body)
        # return "Something Went Wrong in the show command."
    except Exception as e:
        error_message = f"Error fetching data: {str(e)}"
        return error_message

def handle_create_command(recipient, user, doctype=None):
    """
    Handle 'Create' command.
    """
    try:

        if not doctype:
            send_and_log_whatsapp_message(recipient, "Invalid command format for 'Create'. Please specify what to create.")
            return
        if frappe.has_permission(doctype, "create", user=user):
            mandatory_fields = get_mandatory_fields(doctype)
            frappe.log_error("mandatory_fields", mandatory_fields)

            message_body = (
                            f"Save {doctype} Data {mandatory_fields} \n\n"
                            f"Fill the value of fields. Send this message back to me to Save this.😊"
                            )
            send_and_log_whatsapp_message(recipient, message_body)
            return
        else:
            raise frappe.PermissionError(f"You don't have permission to create {doctype}.")
    except frappe.PermissionError as e:
        send_and_log_whatsapp_message(recipient, str(e))
    except Exception as e:
        error_message = f"Error creating data: {str(e)}"
        send_and_log_whatsapp_message(recipient, error_message)


def handle_save_command_1(recipient, user, doctype=None, data_to_save=None):
    try:
        if not doctype or not data_to_save:
            message_body = "Invalid command format for 'Save the Document.'"
            send_and_log_whatsapp_message(recipient, message_body)
            return

        # Convert the string to a dictionary (use ast.literal_eval for safety)
        parsed_data = ast.literal_eval(data_to_save)  # Convert string to dictionary
        parsed_data["doctype"] = doctype

        # Create a new document using the parsed data
        doc = frappe.get_doc({**parsed_data})  # Unpack the dictionary into keyword arguments

        frappe.log_error("err", ({"doctype":doctype, **parsed_data}, doc))

        # Insert the new document into the database
        doc.insert(ignore_permissions=True)
        # doc.insert()

        # Send a success message
        message_body = f"New {doctype} record created successfully with ID: {doc.name}"
        send_and_log_whatsapp_message(recipient, message_body)
        return

    except Exception as e:
        error_message = f"Error while saving data: {str(e)}"
        send_and_log_whatsapp_message(recipient, error_message)



def send_and_log_whatsapp_message(recipient, message_body):
    """
    Send a WhatsApp message and log it as an outgoing message in the database.
    """
    try:
        # Placeholder: Implement your WhatsApp API integration here
        print(f"Sending WhatsApp message to {recipient}: {message_body}")
        
        # Log the outgoing message in WhatsApp Message Doctype
        frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": recipient,
            "message": message_body,
            "content_type": "text",
        }).insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(message=f"Failed to send WhatsApp message: {str(e)}", title="WhatsApp Message Error")





def check_doctype_permissions(user, doctype_name):
    """
    Check if the user has access to a specific DocType.
    """
    return frappe.has_permission(doctype=doctype_name, user=user)


def check_document_permissions(user, doctype_name, doc_id):
    """
    Check if the user has access to a specific document.
    """
    return frappe.has_permission(doctype=doctype_name, doc=doc_id, user=user)




def is_valid_user_1(user_contact_no):
    try:
        user = frappe.db.get_value("User", {
            "phone": user_contact_no
        }, ["name"]) or frappe.db.get_value("User", {
            "mobile_no": user_contact_no
        }, ["name"])

        if not user:
            return None
        
        return user
    except Exception as e:
        frappe.log_error(message=f"Error in is_valid_user: {str(e)}", title="is_valid_user")


def is_valid_user(user_contact_no):
    """
    Validate and return user based on contact number.
    Returns user's email/name or None if not found.
    """
    try:
        user = frappe.db.get_value("User", {
            "phone": user_contact_no
        }, ["name"]) or frappe.db.get_value("User", {
            "mobile_no": user_contact_no
        }, ["name"])
        
        if not user:
            frappe.log_error(
                title="User Not Found", 
                message=f"No user found for contact: {user_contact_no}"
            )
            return None
            
        return user
        
    except Exception as e:
        frappe.log_error(
            message=f"Error in is_valid_user: {str(e)}", 
            title="is_valid_user"
        )
        return None


def handle_show_command(recipient, user, doctype=None, docname=None):
    """
    Handle Show command with user impersonation for proper permissions.
    """
    original_user = frappe.session.user
    try:
        # Set user context to the WhatsApp user
        frappe.set_user(user)
        # frappe.log_error(
        #     title="User Context", 
        #     message=f"Switched to user: {user}, Original: {original_user}"
        # )
        
        if not doctype:
            send_and_log_whatsapp_message(recipient, "Invalid command format for 'Show'. Please specify what to show.")
            return
    
        # Using DatabaseQuery for permission-aware querying
        from frappe.model.db_query import DatabaseQuery
        
        if docname and not extract_number_from_list_string(docname):
            # if docname is list-* then dont run this code then give list of documents
            try:
                # data = frappe.get_doc(doctype, docname)
                # if not data.has_permission("read"):
                #     raise frappe.PermissionError(f"You don't have permission to access this {doctype} record.")
                
                domain = get_url()
                
                # JSON DATA 
                # send_and_log_whatsapp_message(recipient,f"Record Details:\n{frappe.as_json(data.as_dict())}\n\n\n http://localhost:8000/api/method/frappe.utils.print_format.download_pdf?doctype={doctype}&name={docname}&format=Standard&no_letterhead=1&letterhead=No%20Letterhead&settings=%7B%7D&_lang=en")

                send_and_log_whatsapp_message(recipient,f"http://localhost:8000/api/method/frappe.utils.print_format.download_pdf?doctype={doctype}&name={docname}&format=Standard&no_letterhead=1&letterhead=No%20Letterhead&settings=%7B%7D&_lang=en")
                return
            except frappe.PermissionError as e:
                 send_and_log_whatsapp_message(recipient,str(e))
            except frappe.DoesNotExistError:
                 send_and_log_whatsapp_message(recipient,f"Record {docname} not found in {doctype}.")
                
        else:
            limit_page_length = extract_number_from_list_string(docname) if docname else 10
            query = DatabaseQuery(doctype)
            query.user = user
            query.ignore_permissions = False
            
            if doctype == 'Item':
                data = query.execute(
                    fields=["name", "stock_uom"],
                    filters={},
                    order_by="creation desc",
                    limit_start=0,
                    limit_page_length=limit_page_length
                )
            else:
                data = query.execute(
                    fields=["name", "creation"],
                    filters={},
                    order_by="creation desc",
                    limit_start=0,
                    limit_page_length=10
                )

            if not data:
                return f"No accessible records found in {doctype}"
            
            if doctype == 'Item':
                records = [
                    f"- {d.get('name')} "
                    f"({d.get('stock_uom')})"
                    # f"({frappe.utils.format_datetime(d.get('creation'), 'MMM dd, YYYY')})"
                    for d in data
                ]
            else:
                records = [
                    f"- {d.get('name')} "
                    # f"({frappe.utils.format_datetime(d.get('creation'), 'MMM dd, YYYY')})"
                    for d in data
                ]
            
            
            message_body = (f"Your accessible {doctype} records:\n"
                f"{chr(10).join(records)}\n"
                f"Showing {len(data)} records")
            
            send_and_log_whatsapp_message(recipient, message_body)
            return

    except Exception as e:
        frappe.log_error(
            title="Show Command Error",
            message=f"Error for user {user}: {str(e)}"
        )
        message_body = f"Error processing your request. Please try again."
        send_and_log_whatsapp_message(recipient, message_body)
        return
        
    finally:
        # Always reset to original user
        frappe.set_user(original_user)
        # frappe.log_error(
        #     title="User Context Reset",
        #     message=f"Reset user context to: {original_user}"
        # )

def handle_show_item_command(recipient, user, item_group, limit_page_length=15):
    """
    Handle Show command with user impersonation for proper permissions.
    """
    original_user = frappe.session.user
    doctype = "Item"
    filters = {}
    try:
        # Set user context to the WhatsApp user
        frappe.set_user(user)

        if not doctype:
            send_and_log_whatsapp_message(recipient, "Invalid command format for 'Show'. Please specify what to show.")
            return
    
        # Using DatabaseQuery for permission-aware querying
        from frappe.model.db_query import DatabaseQuery

        query = DatabaseQuery(doctype)
        query.user = user
        query.ignore_permissions = False
        frappe.log_error("Item Group Name", item_group)
        if doctype == 'Item':
                filters['item_group'] = item_group
                if limit_page_length:
                    data = query.execute(
                        fields=["name", "stock_uom"],
                        filters=filters,
                        order_by="creation desc",
                        limit_start=0,
                        limit_page_length=limit_page_length
                    )
                else:
                    data = query.execute(
                        fields=["name", "stock_uom"],
                        filters=filters,
                        order_by="creation desc",
                    )

                total_count = frappe.db.count('Item', filters=filters)

            
        # else:
        #         data = query.execute(
        #             fields=["name", "creation"],
        #             filters=filters,
        #             order_by="creation desc",
        #             limit_start=0,
        #             limit_page_length=10
        #         )

        if not data:
                return f"No accessible records found in {doctype}"
            
        if doctype == 'Item':
                records = [
                    f"- {d.get('name')} "
                    f"({d.get('stock_uom')})"
                    # f"({frappe.utils.format_datetime(d.get('creation'), 'MMM dd, YYYY')})"
                    for d in data
                ]
        else:
                records = [
                    f"- {d.get('name')} "
                    # f"({frappe.utils.format_datetime(d.get('creation'), 'MMM dd, YYYY')})"
                    for d in data
                ]
            
        if limit_page_length and total_count > limit_page_length:
            message_body = (f"Your accessible {doctype} records:\n"
                    f"{chr(10).join(records)}\n"
                    f"Showing {len(data)} records")
        else:
            message_body = (f"Your accessible {doctype} records:\n"
                    f"{chr(10).join(records)}\n\n"
                    f"Showing All the records.")
            
        send_and_log_whatsapp_message(recipient, message_body)

        # Add extra details into the filters which we can use to give next reponse
        if limit_page_length and total_count > limit_page_length:
            filters['doctype'] = 'Item'
            send_whatsapp_button_message(recipient, filters)
        

    except Exception as e:
        frappe.log_error(
            title="Show Command Error",
            message=f"Error for user {user}: {str(e)}"
        )
        message_body = f"Error processing your request. Please try again."
        send_and_log_whatsapp_message(recipient, message_body)
        return
        
    finally:
        # Always reset to original user
        frappe.set_user(original_user)

def extract_number_from_list_string(input_string):
    # Regular expression to match 'list' or 'List' followed by a dash and a number
    match = re.match(r'(?i)^list-(\d+)$', input_string)
    
    if match:
        # If a match is found, return the number as an integer
        return int(match.group(1))
    else:
        # Return None if no match is found
        return None


def handle_whatsapp_message(message, user):
    """
    Main handler for WhatsApp messages.
    """
    try:
        if message.get('type') == 'text':
            
            user_name = frappe.db.get_value("User", user, ["full_name"]) or "User"

            text = message.get('text', {}).get('body', '').strip()
            parts = text.split(' ')

            # doctype = parts[1].title() if len(parts) > 1 else None
            command = parts[0] if len(parts) > 0 else None
            doctype = parts[1] if len(parts) > 1 else None
            docname = parts[2] if len(parts) > 2 else None
            data_to_save = parts[3] if len(parts) > 3 else None

            frappe.log_error("User Message", text)
            if doctype:
                doctype = split_capitalized_words(doctype)
                # frappe.log_error("Parts", parts)

            
            if text.lower() == "whoami":
                send_and_log_whatsapp_message(message['from'], f"Hello {user_name}, Your username is {user}.")
            elif text.lower() == "options":
                response = send_options_message(message['from'])
                # frappe.log_error("List Message error", response)
            elif text.lower() in ["hii", "hi", "hello", "hola", "namaste", "hare krishna", "raadhe raadhe"]:
                send_and_log_whatsapp_message(message['from'], f"{text.title()} {user_name} 😊")
            elif command.lower() == "purchase":
                # doctype, data_to_save = parse_save_message(text)
                handle_purchase_command(message['from'], user, "Sales Order", text)
            elif command.lower() == "save" and data_to_save:
                doctype, data_to_save = parse_save_message(text)
                handle_save_command(message['from'], user, doctype, data_to_save)
            elif command.lower() == "show":
                handle_show_command(message['from'], user, doctype, docname)
            elif command.lower() ==  "create":
                handle_create_command(message['from'], user, doctype)
            elif text.lower() == "help":
                 # Response message in case the input doesn't match any command
                message_body = (
                    f"Hello {user_name}! 😊\n\n"
                    "Welcome to our system. Here's how you can interact with me:\n\n"
                    "*Commands:*\n"
                    "- To show data, type:\n"
                    "  `show <doctype_name>` or `show <doctype_name> <doc_id>`\n\n"
                    "- To see all main options, type:\n"
                    "  `options`\n\n"
                    # "- To save data, type:\n"
                    # "- To create something, type:\n"
                    # "  `create <doctype_name>`\n\n"
                    # "- To save data, type:\n"
                    # "  `save <doctype_name> {data}`\n\n"
                    # "For example:\n\n"
                    # "`show Customer`\n\n"
                    # "`create Lead`\n\n"
                    # "`save Lead {\"country\": \"India\", \"first_name\": \"Ram\"}`\n\n"
                    "Feel free to try any of these commands. Let me know how I can assist you!"
                )
                send_and_log_whatsapp_message(message['from'], message_body)
            else:
                message_body = (
                    f"Hello {user_name}! 😊\n\n"
                    "Type `help`  (To know how to talk with me.)"
                )
                send_and_log_whatsapp_message(message['from'], message_body)

    except Exception as e:
        frappe.log_error(
            title="WhatsApp Handler Error",
            message=f"Error processing message: {str(e)}"
        )



@frappe.whitelist(allow_guest=True)
def get_mandatory_fields(doctype_name=None):
    if not doctype_name:
        doctype_name = frappe.local.form_dict.get("doctype_name")

    meta = frappe.get_meta(doctype_name)  # Get the metadata of the DocType
    mandatory_fields = [
        field.fieldname for field in meta.fields if field.reqd
        # field.fieldname for field in meta.fields if field.fieldname
    ]  # Filter fields where `reqd` is True
    data =  frappe.db.get_all(
        "DocField",
        filters={
            "parent": doctype_name,  # The DocType name passed as query param
            "reqd": 1               # Only mandatory fields
        },
        fields=["fieldname", "fieldtype"]  # Fields to fetch
    )

    json_object = json.dumps({field: None for field in mandatory_fields})

    return json_object


def parse_save_message(message):
    # Remove 'Save' and any leading spaces
    message = message[5:].strip()
    
    # Split the message into Doctype and Data part
    parts = message.split(" Data ", 1)  # Split by ' Data ' to get Doctype and JSON part
    
    if len(parts) != 2:
        return None, None  # If the format is incorrect, return None
    
    doctype_name = parts[0].strip()  # The first part is the Doctype name
    data_to_save = parts[1].strip()  # The second part is the JSON string

    try:
        match = re.match(r'\{.*\}', data_to_save.strip())  # Match the JSON part from the beginning
        
        if match:
            return doctype_name, match.group(0)  # Return the matched JSON part
        return None, None
    except json.JSONDecodeError:
        return None, None  # If the JSON is invalid, return None
    

def handle_save_command(recipient, user, doctype=None, data_to_save=None):
    original_user = frappe.session.user

    try:
        # Basic input validation
        if not doctype or not data_to_save:
            message_body = "Invalid command format for 'Save the Document.'"
            send_and_log_whatsapp_message(recipient, message_body)
            return

        # Check if user has permission to create this doctype
        if not frappe.has_permission(doctype, "create", user=user):
            message_body = f"You don't have permission to create {doctype}"
            send_and_log_whatsapp_message(recipient, message_body)
            return

        # Convert string to dictionary safely
        try:
            import ast
            parsed_data = ast.literal_eval(data_to_save)
            parsed_data["doctype"] = doctype
            
            # Set owner and modified_by to current user
            parsed_data["owner"] = user
            parsed_data["modified_by"] = user
            
        except (ValueError, SyntaxError) as e:
            message_body = f"Invalid data format: {str(e)}"
            send_and_log_whatsapp_message(recipient, message_body)
            return

        # Create document with provided data
        try:
            if frappe.has_permission(doctype, "create", user=user):
                doc = frappe.get_doc({**parsed_data})

                # Log document creation attempt
                frappe.log_error("Document Creation Attempt", {
                    "doctype": doctype,
                    "data": parsed_data,
                    "user": user,
                    "doc": doc
                })

                # Insert with user context
                frappe.set_user(user)
                doc.insert(ignore_permissions=True)

                message_body = f"New {doctype} record created successfully with ID: {doc.name}"
                send_and_log_whatsapp_message(recipient, message_body)
                return doc.name

        except frappe.PermissionError:
            message_body = f"Permission denied: Unable to create {doctype}"
            send_and_log_whatsapp_message(recipient, message_body)
            return
            
        except frappe.DuplicateEntryError:
            message_body = f"A {doctype} with these details already exists"
            send_and_log_whatsapp_message(recipient, message_body)
            return
            
        except frappe.ValidationError as e:
            message_body = f"Validation failed: {str(e)}"
            send_and_log_whatsapp_message(recipient, message_body)
            return

    except Exception as e:
        error_message = f"Error while saving data: {str(e)}"
        frappe.log_error(f"WhatsApp Save Command Error: {str(e)}", "WhatsApp Integration")
        send_and_log_whatsapp_message(recipient, error_message)
    
    finally:
        # Reset user context
        frappe.set_user(original_user)

def split_capitalized_words(input_string):
    return re.sub(r'(?<!^)(?=[A-Z])', ' ', input_string)



def handle_signup_command(text, recipient):
    from frappe_whatsapp.utils.actions import sign_up
    command, user_data = parse_signup_message(text)
    if command.lower() == "signup":
        try:
            parsed_data = ast.literal_eval(user_data)  # Convert string to dictionary

            # Extract required fields
            email = parsed_data.get("email")
            full_name = parsed_data.get("full_name")
            # phone_number = parsed_data.get("phone_number")

            # Check if all required fields are present
            if not email or not full_name:
                raise ValueError("Missing required fields in signup data.")

            # Call the sign-up function with extracted data
            try:
                sign_up(email, full_name, recipient)
            except Exception as e:
                frappe.log_error("SignUp Failed", "In handle_signup_command")
                send_and_log_whatsapp_message(recipient, "SignUp Operation Failed !! We are fixing it sorry for this.")   
        except (ValueError, SyntaxError) as e:
            frappe.log_error("signup error",f"Error in parsing or processing signup data: {e}")
                        # Handle invalid input or send error message to the recipient
            send_and_log_whatsapp_message(recipient, "Invalid signup data. Please check your input and try again.")            



def parse_signup_message(message):
    # Remove leading spaces and ensure the message starts cleanly
    message = message.strip()
    
    # Match the pattern for `doctype_name` and JSON body
    match = re.match(r'^(\w+)\s+(\{.*?\})', message)
    
    if match:
        command = match.group(1)  # Extract the word before the JSON
        user_data = match.group(2)    # Extract the JSON body

        try:
            # Validate the JSON body
            # user_data = json.loads(user_data)
            return command, user_data
        except json.JSONDecodeError:
            send_and_log_whatsapp_message()
    return None, None  # Return None if the format doesn't match


def find_customer(recipient):
    customer = frappe.db.get_value("Customer", {
                    "custom_phone_number": recipient
                }, ["name"])
    
    return customer or None


def handle_purchase_command(recipient, user, doctype=None, data_to_save=None):
    original_user = frappe.session.user

    try:
        # Basic input validation
        if not doctype or not data_to_save:
            message_body = "Invalid command format for 'Save the Document.'"
            send_and_log_whatsapp_message(recipient, message_body)
            return

        # Check if user has permission to create this doctype
        if not frappe.has_permission(doctype, "create", user=user):
            message_body = f"You don't have permission to create {doctype}"
            send_and_log_whatsapp_message(recipient, message_body)
            return

        # Parse the items from the input
        try:
            # item_codes = parse_item_codes(data_to_save)
            item_codes = extract_items_and_quantities(data_to_save)

            if not item_codes:
                message_body = "No valid item codes found in the message. Please make sure you enter the qty for all the items.\n\n Eg. purchase 1.SKU001 qty:10 2.SKU034 qty:7"
                send_and_log_whatsapp_message(recipient, message_body)
                return
            
            # frappe.log_error("items", item_codes)
            # valid_items = item_validation(item_codes)
            valid_items = validate_items_one_by_one(item_codes)

            # ask_qty_for_the_items(item)
            frappe.log_error("Create SO", valid_items)
            customer = find_customer(recipient)

            if not customer:
                current_user = frappe.get_doc("User", user)
                customer = frappe.get_doc({
                    "doctype": "Customer",
                    "customer_name": current_user.full_name,
                    "customer_type": "Individual", 
                    "custom_phone_number": recipient,
                    'email_id': current_user.email
                }).insert(ignore_permissions=True)

            # Prepare data for the Sales Order
            sales_order_data = {
                "doctype": "Sales Order",
                "customer": customer,  # Example customer, you can change this or extract from the message
                "transaction_date": frappe.utils.today(),  # You can adjust this or extract from the message
                "items": [],
                "delivery_date": frappe.utils.today(),
                "set_warehouse": "Stores - TIPLD"   
            }


            for item in valid_items:
                rate = get_item_price(item['item_code'])
                stock_uom = frappe.db.get_value("Item", item['item_code'], "stock_uom")
                if rate:
                    sales_order_data["items"].append({
                        "item_code": item['item_code'],
                        "qty": item['qty'],  # Default quantity, you can change this based on user input
                        "rate": rate,  # Default rate, adjust as needed
                        "uom": stock_uom  # Default unit of measure, adjust if needed
                    })
                else:
                    send_and_log_whatsapp_message(recipient, f"Can't select the item: {item['item_code']} because no item price is present.")
            # Now create the Sales Order
            if len(sales_order_data['items']) > 0:
                doc = frappe.get_doc(sales_order_data)
            else:
                raise Exception("Item List it empty.")

            # Log document creation attempt
            frappe.log_error("Sales Order Creation Attempt", {
                "doctype": doctype,
                "data": sales_order_data,
                "user": user,
                "doc": doc
            })

            # Insert with user context
            frappe.set_user(user)
            doc.insert(ignore_permissions=True)

            message_body = f"New {doctype} record created successfully with ID: {doc.name}"
            send_and_log_whatsapp_message(recipient, message_body)

            send_order_confirmation_message(recipient, doc.name)
            handle_show_command(recipient, user, doctype, doc.name)
            return doc.name

        except Exception as e:
            message_body = f"Error while processing items: {str(e)}"
            send_and_log_whatsapp_message(recipient, message_body)
            return

    except Exception as e:
        error_message = f"Error while saving data: {str(e)}"
        frappe.log_error(f"WhatsApp Save Command Error: {str(e)}", "WhatsApp Integration")
        send_and_log_whatsapp_message(recipient, error_message)
    
    finally:
        # Reset user context
        frappe.set_user(original_user)


def validate_items_one_by_one(item_codes):
    """Validate item codes one by one and return a unique list of valid items."""
    valid_items = []
    seen_items = set()  # Track unique item codes to avoid duplicates
    
    # Log the input item codes for debugging
    frappe.log_error("Item codes input", item_codes)

    # Iterate through each item code
    for item in item_codes:
        item_code = item['item_code']
        frappe.log_error("Item", item_code)
        
        if not item_code:
            frappe.log_error(f"Item code missing for item: {item}", item)
            continue  # Skip this item if no item_code exists

        # Check if the item code is already processed to avoid duplicates
        if item_code in seen_items:
            continue
        
        # Query the database for each item code to check if it's valid
        existing_item = frappe.get_doc(
            "Item",
            item_code,
            filters={"disabled": 0},  # Ensure the item is not disabled
        )
        frappe.log_error("existing_item", existing_item)
        # If the item exists and is valid (i.e., it is not disabled)
        if existing_item:
            valid_items.append(item)  # Add the item to valid_items
            seen_items.add(item_code)  # Add to seen to prevent duplicates
            frappe.log_error(f"Valid item: {item_code}", item)
        else:
            frappe.log_error(f"Invalid item: {item_code}", item)

    # Log the valid items after validation
    frappe.log_error("Valid items after validation", valid_items)
    
    return valid_items


def get_item_price(item_code):
    """Get the selling price of a specific item from the active Item Price list."""
    from frappe.query_builder import Order

    ip = frappe.qb.DocType("Item Price")
    pl = frappe.qb.DocType("Price List")

    # Query to fetch the price list rate
    result = (
        frappe.qb.from_(ip)
        .join(pl)
        .on(ip.price_list == pl.name)
        .select(ip.price_list_rate)
        .where(
            (ip.item_code == item_code)
            & (ip.selling == 1)  # Ensure it is a selling price
            & (pl.enabled == 1)  # Ensure the price list is active
        )
        .orderby(ip.creation, order=Order.desc)  # Fetch the most recent price list entry
        .limit(1)  # Limit to the latest price list rate
    ).run(as_dict=True)

    # Return the price_list_rate or None if not found
    return result[0]["price_list_rate"] if result else None


def parse_item_codes(data_to_save):
    """
    This function will extract item codes from the user's message.
    The expected format is: '1. ItemCode-1', '2. ItemCode-2', etc.
    """
    item_codes = []
    
    # Regular expression to match item codes from the format like "1. ItemCode-1", "2. ItemCode-2"
    item_code_pattern = r'\d+\.\s*([a-zA-Z0-9\-]+)'

    # Find all matches for item codes
    matches = re.findall(item_code_pattern, data_to_save)

    # Add matches to the list
    if matches:
        item_codes = matches

    return item_codes


def extract_items_and_quantities(input_string):
    """Extract item names and quantities from the input string."""
    # Remove the word 'purchase' from the string if present
    input_string = input_string.lower().strip()
    if input_string.startswith("purchase"):
        input_string = input_string[len("purchase"):].strip()
    
    # Replace spaces between items with newlines for consistent processing
    input_string = re.sub(r'(\d+\.)', r'\n\1', input_string)

    # Regular expression to match 'Item_Name qty:number' allowing spaces around qty:
    item_pattern = r'(\d+\.)\s*([^\n]+?)\s+qty:\s*(\d+)'
    
    # Find all matches
    matches = re.findall(item_pattern, input_string)

    extracted_items = []
    frappe.log_error("extracted_items", extracted_items)
    for match in matches:
        item_name = match[1].strip()  # The item name (after the number)
        quantity = int(match[2].strip())  # The quantity after qty:
        frappe.log_error("extracted_items_1", {"item":item_name,"qty": quantity})

        extracted_items.append({
            'item_code': item_name,
            'qty': quantity
        })
    
    frappe.log_error("extracted_items", extracted_items)
    return extracted_items

# # Test cases
# input_string_1 = "purchase 1.Paracetamol200mg qty:5"
# input_string_2 = "purchase 1. Paracetamol200mg qty:5"
# input_string_3 = "purchase 1.Paracetamol200mg qty: 5"
# input_string_4 = "purchase 1.Paracetamol200mg qty: 5 2.Orange qty: 10"





def send_whatsapp_list_message(recipient, options, header_text="Options", body_text="Choose an action from the list below:", footer_text="Tap the button below to see all actions.", filters=None):
    """
    Send a WhatsApp list message to the recipient and log it in the database.
    """
    try:
        # Construct the sections with the provided options
        
        sections = [
            {
                "title": "Main Actions",
                "rows": [{"id": f"action_{i}_[{filters}]" if filters else f"action_{i}_[]", "title": opt['title'], "description": opt.get('description', ""), } for i, opt in enumerate(options)]
            }
        ]
        
        # WhatsApp API payload
        payload = {
            "interactive": {
                "type": "list",
                "header": {
                    "type": "text",
                    "text": header_text
                },
                "body": {
                    "text": body_text
                },
                "footer": {
                    "text": footer_text
                },
                "action": {
                    "button": "View Options",
                    "sections": sections
                }
            }
        }


        frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": recipient,
            "message": str(payload),
            "content_type": "interactive",
            # "message_id": res_json["messages"][0]["id"],
            # "skip_actions":True
            "data": payload
        }).insert(ignore_permissions=True)


        # return response.json()

    except Exception as e:
        frappe.log_error(message=f"Failed to send WhatsApp list message: {str(e)}", title="WhatsApp Message Error")
        return {"error": str(e)}

# Example usage
def send_options_message(recipient):
    """
    Prepare and send a list of options to the recipient.
    """
    options = [
        {"title": "Show Items", "description": "View the list of available items."},
        # {"title": "Purchase Item", "description": "Buy an item from our store."},
        # {"title": "View Cart", "description": "Check the items in your cart."},
        # {"title": "Track Order", "description": "See the status of your orders."},
        {"title": "Support", "description": "Contact our support team for assistance."},
        # {"title": "FAQ", "description": "Read frequently asked questions."},
        # {"title": "Promotions", "description": "Explore ongoing promotions and offers."},
        {"title": "Feedback", "description": "Share your feedback with us."}
        # Add more options here as needed, keeping within the WhatsApp API's limits
    ]

    return send_whatsapp_list_message(recipient, options)

def send_item_group_message(recipient):
    """
    Fetch all item groups with is_group = 0 and send them as a WhatsApp list message.
    """
    try:
        # Fetch item groups from the database where is_group = 0
        item_groups = frappe.get_all(
            "Item Group",
            filters={"is_group": 0},
            fields=["name"]
        )

        # Construct options from the fetched item groups
        options = [
            {"title": item["name"], "description": "item description here we will write", "doctype":"Item Group"} 
            for item in item_groups
        ]

        # If no options are available, handle gracefully
        if not options:
            frappe.log_error("No item groups found where is_group = 0", "WhatsApp Message Error")
            return {"error": "No item groups available to send as options."}

        # Send the WhatsApp list message
        filters = {"selection": "Item Group"}
        return send_whatsapp_list_message(recipient, options, "Item Category", "Choose an category to see that items:", "Tap the button below to see all actions.", filters)


    except Exception as e:
        frappe.log_error(message=f"Failed to fetch or send item groups: {str(e)}", title="WhatsApp Message Error")
        return {"error": str(e)}


def handle_list_response_show_items(recipient):
    try:
        options = [
            {"title": "Category Wise", "description": "It will show you all the category of items."},
            {"title": "Show All Item", "description": "It will show you all the items."},
        ]
        return send_whatsapp_list_message(recipient, options, "Items", "Choose an option to see that items:", "Tap the button below to see all actions.")
    except Exception as e:
        frappe.log_error("Failed in list response show item", str(e))


def send_whatsapp_button_message(recipient,filters, button_text="Show More", body_text="Here are your options:", footer_text="Tap the button below to load more options."):
    """
    Send a WhatsApp button message to the recipient.
    """
    try:

        payload = {
            "recipient_type": "individual",
            "to": recipient,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": " Tap the button below to load more options."  # Leave this blank if you don't want a body text
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"show_more_filter[{filters}]",
                                "title": button_text
                            }
                        }
                    ]
                }
            }
        }


        

        # Log the outgoing message
        frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": recipient,
            "message": str(payload),
            "content_type": "interactive",
            "data": payload
        }).insert(ignore_permissions=True)

        frappe.log_error("Show MOre Button Working", "okay")

        return "Done"

    except Exception as e:
        frappe.log_error(message=f"Failed to send WhatsApp button message: {str(e)}", title="WhatsApp Button Message Error")
        return {"error": str(e)}





def parse_interactive_message(message):

    # Your input string (it can start with anything)
    try:
        # Use regex to extract the part within the square brackets
        match = re.search(r'\[.*\]', message)  # This will match everything inside the square brackets

        if match:
            # Extract the matched string (the list part)
            list_str = match.group(0)  # Get the matched part (the entire list string)

            # Convert the string representation of the list into an actual Python object (list)
            parsed_data = ast.literal_eval(list_str)
            frappe.log_error("parse_interactive_message", parsed_data)
            return parsed_data
        else:
            frappe.log_error("parse_interactive_message","No matching data found.")
    except Exception as e:
            frappe.log("parse_interactive_message", "parse_interactive_message")



def send_order_confirmation_message(recipient, order_id, order_details=None):
    """
    Send a WhatsApp button message to the recipient to confirm an order.
    """
    try:
        # Customize the message body with order details
        body_text = f"Please confirm your order:\n\nOrder ID: {order_id}\n\nDo you want to confirm this order?"

        filters = {'order_id': order_id}
        # WhatsApp interactive button payload
        payload = {
            "recipient_type": "individual",
            "to": recipient,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": body_text  # Include the order details in the body text
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"so_confirm_yes_filter[{filters}]",
                                "title": "Yes"
                            }
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"so_confirm_no_filter[{filters}]",
                                "title": "No"
                            }
                        }
                    ]
                }
            }
        }

        # Log the outgoing message in your database
        frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": recipient,
            "message": str(payload),
            "content_type": "interactive",
            "data": payload
        }).insert(ignore_permissions=True)

        # Optionally log for debugging
        frappe.log_error(f"Order Confirmation Message Sent to {recipient}", "Order Confirmation")

        return "Order confirmation message sent successfully."

    except Exception as e:
        frappe.log_error(message=f"Failed to send order confirmation message: {str(e)}", title="WhatsApp Order Confirmation Error")
        return {"error": str(e)}


def ask_for_the_address(recipient, filters):
    try:
        
        body_text = f"{filters['order_id']} - Please select your location from the given list or send a new location !!"

        send_and_log_whatsapp_message(recipient, body_text)

        return "address message sent successfully."
    
    except Exception as e:
        frappe.log_error("Error in ask_for_the_address function", str(e))


def ask_for_the_payment(recipient, order_id):
    try:
        # Customize the message body with order details
        body_text = f"Please select the payment method for the Order ID: {order_id}.\n"

        filters = {'order_id': order_id}
        # WhatsApp interactive button payload
        payload = {
            "recipient_type": "individual",
            "to": recipient,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": body_text  # Include the order details in the body text
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"cod_filter[{filters}]",
                                "title": "COD"
                            }
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"pay_now_filter[{filters}]",
                                "title": "Pay Now"
                            }
                        }
                    ]
                }
            }
        }

        # Log the outgoing message in your database
        frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": recipient,
            "message": str(payload),
            "content_type": "interactive",
            "data": payload
        }).insert(ignore_permissions=True)

        # Optionally log for debugging

        return "message sent successfully."

    except Exception as e:
        frappe.log_error(message=f"Failed to send payment method asking message: {str(e)}", title="payment method asking")
        return {"error": str(e)}