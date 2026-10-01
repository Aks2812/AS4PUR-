#!/usr/bin/env python3
import json
import re
import ssl
import sys
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib import error, parse, request

try:
    import openpyxl
except ImportError:
    openpyxl = None

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_ENTERPRISE_USER_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
TOKEN_FILE_NAME = ".scim_token"
DEFAULT_TENANT = "jakarta.goskope.com"


def read_excel_column_a(file_path: str) -> list[str]:
    """Reads the first column of the first sheet in an Excel file."""
    if openpyxl is None:
        raise ImportError("The 'openpyxl' library is required. Install it via 'pip install openpyxl'.")

    # Clean path from quotes if user pasted them
    file_path = file_path.strip('"').strip("'")

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    values = []
    workbook = openpyxl.load_workbook(file_path, data_only=True)
    sheet = workbook.active  # Gets the first sheet

    # Iterate through column A (column 1)
    for row in range(1, sheet.max_row + 1):
        cell_value = sheet.cell(row=row, column=1).value
        if cell_value:
            values.append(str(cell_value).strip())

    return values


def validate_email(email: str) -> bool:
    return re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email) is not None


def normalize_tenant(tenant: str) -> str:
    tenant = tenant.strip()
    tenant = tenant.replace("https://", "").replace("http://", "")
    return tenant.strip("/")


def validate_tenant(tenant: str) -> bool:
    normalized = normalize_tenant(tenant)
    if not normalized:
        return False
    if any(ch.isspace() for ch in normalized):
        return False
    return re.match(r"^[A-Za-z0-9.-]+$", normalized) is not None


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "group"


@dataclass
class ScimClient:
    tenant: str
    token: str
    verify_ssl: bool = False

    def __post_init__(self) -> None:
        self.base_url = f"https://{normalize_tenant(self.tenant)}/api/v2/scim"
        self.ssl_context = ssl.create_default_context()
        if not self.verify_ssl:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None, generic_path: bool = False) -> \
    tuple[int, Any]:
        # If generic_path is True, we can target API endpoints outside of the /scim namespace
        if generic_path:
            url = f"https://{normalize_tenant(self.tenant)}{path}"
        else:
            url = f"{self.base_url}{path}"

        payload = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        if body is not None:
            payload = json.dumps(body).encode("utf-8")

        req = request.Request(url=url, data=payload, method=method, headers=headers)

        try:
            with request.urlopen(req, context=self.ssl_context, timeout=30) as resp:
                status = resp.getcode()
                raw = resp.read().decode("utf-8") if resp else ""
                if raw:
                    try:
                        return status, json.loads(raw)
                    except json.JSONDecodeError:
                        return status, raw
                return status, None
        except error.HTTPError as exc:
            raw_error = exc.read().decode("utf-8") if exc.fp else ""
            parsed_error: Any = raw_error
            if raw_error:
                try:
                    parsed_error = json.loads(raw_error)
                except json.JSONDecodeError:
                    parsed_error = raw_error
            return exc.code, parsed_error
        except ValueError as exc:
            raise RuntimeError(
                "Invalid URL generated. Check tenant/domain format (example: jakarta.goskope.com)."
            ) from exc
        except error.URLError as exc:
            raise ConnectionError(f"Network error: {exc.reason}") from exc

    def get_user_by_id(self, user_id: str) -> tuple[bool, str, Optional[dict[str, Any]]]:
        status, data = self._request("GET", f"/Users/{user_id}")
        if status >= 400:
            return False, f"Failed to fetch user details. API response: {data}", None
        return True, "User detail fetched successfully.", data

    def get_user_by_email(self, email: str) -> Optional[dict[str, Any]]:
        query = parse.quote(f'userName eq "{email}"')
        status, data = self._request("GET", f"/Users?filter={query}")
        if status >= 400:
            raise RuntimeError(f"Failed to search user. API response: {data}")

        resources = data.get("Resources", []) if isinstance(data, dict) else []
        return resources[0] if resources else None

    def get_user_detail_by_email(self, email: str) -> tuple[bool, str, Optional[dict[str, Any]]]:
        user = self.get_user_by_email(email)
        if not user:
            return False, "User not found.", None
        return True, "User detail fetched successfully.", user

    def get_group_by_name(self, group_name: str) -> Optional[dict[str, Any]]:
        query = parse.quote(f'displayName eq "{group_name}"')
        status, data = self._request("GET", f"/Groups?filter={query}")
        if status >= 400:
            raise RuntimeError(f"Failed to search group. API response: {data}")

        resources = data.get("Resources", []) if isinstance(data, dict) else []
        return resources[0] if resources else None

    def get_group_detail_by_name(self, group_name: str) -> tuple[bool, str, Optional[dict[str, Any]]]:
        group = self.get_group_by_name(group_name)
        if not group:
            return False, "Group not found.", None

        group_id = group.get("id")
        if not group_id:
            return False, "Group ID not found from search result.", None

        status, data = self._request("GET", f"/Groups/{group_id}?attributes=members")
        if status >= 400:
            return False, f"Get group detail failed. API response: {data}", None

        if not isinstance(data, dict):
            return False, "Unexpected response format when fetching group detail.", None

        return True, "Group detail fetched successfully.", data

    def create_user(self, email: str) -> tuple[bool, str]:
        existing = self.get_user_by_email(email)
        if existing:
            return False, f"User already exists with ID: {existing.get('id')}"

        local_name = email.split("@")[0]
        display_name = local_name.replace(".", " ").replace("_", " ").title() or email
        name_parts = display_name.split(maxsplit=1)
        given_name = name_parts[0]
        family_name = name_parts[1] if len(name_parts) > 1 else "User"

        body = {
            "schemas": [SCIM_USER_SCHEMA, SCIM_ENTERPRISE_USER_SCHEMA],
            "externalId": email,
            "userName": email,
            "active": True,
            "displayName": display_name,
            "emails": [{"primary": True, "value": email, "type": "work"}],
            "name": {
                "formatted": display_name,
                "familyName": family_name,
                "givenName": given_name,
            },
            "meta": {"resourceType": "User"},
        }

        status, data = self._request("POST", "/Users", body)
        if status >= 400:
            return False, f"Create user failed. API response: {data}"
        return True, f"User created successfully. ID: {data.get('id', 'unknown')}"

    def delete_user_by_email(self, email: str) -> tuple[bool, str]:
        user = self.get_user_by_email(email)
        if not user:
            return False, "User not found."

        user_id = user.get("id")
        status, data = self._request("DELETE", f"/Users/{user_id}")
        if status >= 400:
            return False, f"Delete user failed. API response: {data}"
        return True, f"User deleted successfully. ID: {user_id}"

    def create_group(self, group_name: str) -> tuple[bool, str]:
        existing = self.get_group_by_name(group_name)
        if existing:
            return False, f"Group already exists with ID: {existing.get('id')}"

        body = {
            "schemas": [SCIM_GROUP_SCHEMA],
            "externalId": slugify(group_name),
            "displayName": group_name,
            "members": [],
            "meta": {"resourceType": "Group"},
        }

        status, data = self._request("POST", "/Groups", body)
        if status >= 400:
            return False, f"Create group failed. API response: {data}"
        return True, f"Group created successfully. ID: {data.get('id', 'unknown')}"

    def add_user_to_group(self, group_name: str, email: str) -> tuple[bool, str]:
        group = self.get_group_by_name(group_name)
        if not group:
            return False, f"Group '{group_name}' not found."

        user = self.get_user_by_email(email)
        if not user:
            return False, f"User '{email}' not found."

        group_id = group.get("id")
        user_id = user.get("id")

        body = {
            "schemas": [SCIM_PATCH_SCHEMA],
            "Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}],
        }

        status, data = self._request("PATCH", f"/Groups/{group_id}", body)
        if status >= 400:
            return False, f"Insert into group failed. API response: {data}"
        return True, f"User {email} added to group {group_name}."

    def remove_user_from_group(self, group_name: str, email: str) -> tuple[bool, str]:
        group = self.get_group_by_name(group_name)
        if not group:
            return False, "Group not found."

        user = self.get_user_by_email(email)
        if not user:
            return False, "User not found."

        group_id = group.get("id")
        user_id = user.get("id")

        body = {
            "schemas": [SCIM_PATCH_SCHEMA],
            "Operations": [
                {
                    "op": "remove",
                    "path": "members",
                    "value": [{"value": user_id}],
                }
            ],
        }

        status, data = self._request("PATCH", f"/Groups/{group_id}", body)
        if status >= 400:
            return False, f"Delete from group failed. API response: {data}"
        return True, f"User {email} removed from group {group_name}."

    def delete_group_by_name(self, group_name: str) -> tuple[bool, str]:
        group = self.get_group_by_name(group_name)
        if not group:
            return False, "Group not found."

        group_id = group.get("id")
        status, data = self._request("DELETE", f"/Groups/{group_id}")
        if status >= 400:
            return False, f"Delete group failed. API response: {data}"
        return True, f"Group deleted successfully. ID: {group_id}"

    def list_users(self, count: int = 100) -> tuple[bool, str, list[dict[str, Any]]]:
        status, data = self._request("GET", f"/Users?count={count}&startIndex=1")
        if status >= 400:
            return False, f"List users failed. API response: {data}", []

        resources = data.get("Resources", []) if isinstance(data, dict) else []
        return True, f"Fetched {len(resources)} user(s).", resources

    def list_groups(self, count: int = 100) -> tuple[bool, str, list[dict[str, Any]]]:
        status, data = self._request("GET", f"/Groups?count={count}&startIndex=1")
        if status >= 400:
            return False, f"List groups failed. API response: {data}", []

        resources = data.get("Resources", []) if isinstance(data, dict) else []
        return True, f"Fetched {len(resources)} group(s).", resources

    def send_client_invite(self, emails: list[str]) -> tuple[bool, str]:
        """Triggers Netskope Client Invitations for the given emails via REST API v2."""
        if not emails:
            return False, "No emails to invite."

        body = {"emails": emails}
        status, data = self._request("POST", "/api/v2/infrastructure/client/invite", body, generic_path=True)
        if status >= 400:
            return False, f"Failed to send client invitations. API response: {data}"
        return True, "Netskope Client invitation emails sent successfully."


def ask_non_empty(prompt_text: str) -> str:
    while True:
        value = input(prompt_text).strip()
        if value:
            return value
        print("Input cannot be empty. Please try again.")


def ask_email(prompt_text: str) -> str:
    while True:
        email = ask_non_empty(prompt_text)
        if validate_email(email):
            return email
        print("Invalid email format. Example: user@example.com")


def load_saved_token() -> Optional[str]:
    try:
        with open(TOKEN_FILE_NAME, "r", encoding="utf-8") as token_file:
            token = token_file.read().strip()
            return token or None
    except FileNotFoundError:
        return None
    except OSError:
        return None


def save_token(token: str) -> tuple[bool, str]:
    try:
        with open(TOKEN_FILE_NAME, "w", encoding="utf-8") as token_file:
            token_file.write(f"{token}\n")
        return True, f"Token saved to {TOKEN_FILE_NAME}."
    except OSError as exc:
        return False, f"Failed to save token: {exc}"


def print_menu() -> None:
    print("\n========== USER & GROUP MANAGEMENT ==========")
    print("1. Create users (input user email)")
    print("2. Delete users (input user email)")
    print("3. Create groups (input user group)")
    print("4. Insert into group (input group name + user email)")
    print("5. Delete from group (input group name + user email)")
    print("6. Delete groups (input user group)")
    print("7. List users")
    print("8. List groups")
    print("9. Get group details (input group name)")
    print("a. Get detail user (input user email)")
    print("b. Bulk Create users from Excel (Column A)")
    print("c. Bulk Insert users into Group from Excel (Column A)")
    print("d. Send client invite to group members (uninstalled only)")
    print("z. Exit")
    print("=============================================")


def show_result(success: bool, message: str) -> None:
    icon = "✅" if success else "❌"
    print(f"{icon} {message}")


def print_users(users: list[dict[str, Any]]) -> None:
    if not users:
        print("No users found.")
        return

    print("\n--- Users ---")
    for index, user in enumerate(users, start=1):
        user_id = user.get("id", "-")
        email = user.get("userName", "-")
        display_name = user.get("displayName", "-")
        print(f"{index}. {display_name} | {email} | id={user_id}")


def print_groups(groups: list[dict[str, Any]]) -> None:
    if not groups:
        print("No groups found.")
        return

    print("\n--- Groups ---")
    for index, group in enumerate(groups, start=1):
        group_id = group.get("id", "-")
        group_name = group.get("displayName", "-")
        print(f"{index}. {group_name} | id={group_id}")


def print_user_detail(user: dict[str, Any]) -> None:
    user_id = user.get("id", "-")
    user_name = user.get("userName", "-")
    display_name = user.get("displayName", "-")
    active = user.get("active", "-")
    external_id = user.get("externalId", "-")

    name_obj = user.get("name") if isinstance(user.get("name"), dict) else {}
    formatted_name = name_obj.get("formatted", "-")
    given_name = name_obj.get("givenName", "-")
    family_name = name_obj.get("familyName", "-")

    emails_obj = user.get("emails") if isinstance(user.get("emails"), list) else []
    emails = [str(item.get("value", "")).strip() for item in emails_obj if isinstance(item, dict)]
    emails = [email for email in emails if email]

    groups_obj = user.get("groups") if isinstance(user.get("groups"), list) else []
    groups = [str(item.get("display", item.get("value", ""))).strip() for item in groups_obj if isinstance(item, dict)]
    groups = [group for group in groups if group]

    print("\n--- User Detail ---")
    print(f"ID           : {user_id}")
    print(f"UserName     : {user_name}")
    print(f"Display Name : {display_name}")
    print(f"Active       : {active}")
    print(f"External ID  : {external_id}")
    print(f"Name         : {formatted_name} (given: {given_name}, family: {family_name})")
    print(f"Emails       : {', '.join(emails) if emails else '-'}")
    print(f"Groups       : {', '.join(groups) if groups else '-'}")


def print_group_detail(group: dict[str, Any]) -> None:
    group_id = group.get("id", "-")
    group_name = group.get("displayName", "-")
    external_id = group.get("externalId", "-")

    members_obj = group.get("members") if isinstance(group.get("members"), list) else []

    print("\n--- Group Detail ---")
    print(f"ID           : {group_id}")
    print(f"Display Name : {group_name}")
    print(f"External ID  : {external_id}")
    print(f"Members      : {len(members_obj)}")

    if not members_obj:
        print("- No members found.")
        return

    for index, member in enumerate(members_obj, start=1):
        if not isinstance(member, dict):
            print(f"{index}. value=- | display=-")
            continue
        member_id = member.get("value", "-")
        member_display = member.get("display", "-")
        print(f"{index}. value={member_id} | display={member_display}")


def main() -> None:
    print("\nNetskope SCIM User & Group Management (Python)")
    print("Please provide connection settings.")

    while True:
        tenant_input = input(f"Tenant domain (default: {DEFAULT_TENANT}): ").strip()
        tenant = tenant_input or DEFAULT_TENANT
        if validate_tenant(tenant):
            break
        print("Invalid tenant/domain. Use only domain format, e.g. jakarta.goskope.com (no spaces).")

    saved_token = load_saved_token()
    if saved_token:
        use_saved = input(f"Use saved token from {TOKEN_FILE_NAME}? (Y/n): ").strip().lower()
        if use_saved in {"", "y", "yes"}:
            token = saved_token
            print("Using saved token.")
        else:
            token = ask_non_empty("SCIM Bearer token: ")
            save_choice = input(f"Save this token to {TOKEN_FILE_NAME}? (y/N): ").strip().lower()
            if save_choice in {"y", "yes"}:
                success, message = save_token(token)
                show_result(success, message)
    else:
        token = ask_non_empty("SCIM Bearer token: ")
        save_choice = input(f"Save this token to {TOKEN_FILE_NAME}? (y/N): ").strip().lower()
        if save_choice in {"y", "yes"}:
            success, message = save_token(token)
            show_result(success, message)

    verify_answer = input("Verify SSL certificate? (y/N): ").strip().lower()
    verify_ssl = verify_answer in {"y", "yes"}

    client = ScimClient(tenant=tenant, token=token, verify_ssl=verify_ssl)

    while True:
        try:
            print_menu()
            choice = ask_non_empty("Choose menu (1-9, a-d, z): ").lower()

            if choice == "z":
                print("Exiting. Goodbye.")
                return

            if choice == "1":
                email = ask_email("Input user email to create: ")
                success, message = client.create_user(email)
                show_result(success, message)

            elif choice == "2":
                email = ask_email("Input user email to delete: ")
                success, message = client.delete_user_by_email(email)
                show_result(success, message)

            elif choice == "3":
                group_name = ask_non_empty("Input group name to create: ")
                success, message = client.create_group(group_name)
                show_result(success, message)

            elif choice == "4":
                group_name = ask_non_empty("Input group name: ")
                email = ask_email("Input user email to insert into group: ")
                success, message = client.add_user_to_group(group_name, email)
                show_result(success, message)

            elif choice == "5":
                group_name = ask_non_empty("Input group name: ")
                email = ask_email("Input user email to delete from group: ")
                success, message = client.remove_user_from_group(group_name, email)
                show_result(success, message)

            elif choice == "6":
                group_name = ask_non_empty("Input group name to delete: ")
                confirm = input(f"Type YES to confirm deleting group '{group_name}': ").strip()
                if confirm != "YES":
                    print("Cancelled.")
                    continue
                success, message = client.delete_group_by_name(group_name)
                show_result(success, message)

            elif choice == "7":
                success, message, users = client.list_users()
                show_result(success, message)
                if success:
                    print_users(users)

            elif choice == "8":
                success, message, groups = client.list_groups()
                show_result(success, message)
                if success:
                    print_groups(groups)

            elif choice == "9":
                group_name = ask_non_empty("Input group name to get detail: ")
                success, message, group = client.get_group_detail_by_name(group_name)
                show_result(success, message)
                if success and group is not None:
                    print_group_detail(group)

            elif choice == "a":
                email = ask_email("Input user email to get detail: ")
                success, message, user = client.get_user_detail_by_email(email)
                show_result(success, message)
                if success and user is not None:
                    print_user_detail(user)

            elif choice == "b":
                if openpyxl is None:
                    print("❌ Error: openpyxl not installed. Run 'pip install openpyxl'")
                    continue

                path = ask_non_empty("Enter path to Excel file: ").strip('"')
                try:
                    emails = read_excel_column_a(path)
                    print(f"Found {len(emails)} entries in Column A.")

                    confirm = input("Proceed with bulk creation? (y/N): ").lower()
                    if confirm == 'y':
                        for email in emails:
                            if validate_email(email):
                                success, message = client.create_user(email)
                                show_result(success, message)
                            else:
                                print(f"⚠️ Skipping invalid email: {email}")
                except Exception as e:
                    print(f"❌ Excel Error: {e}")

            elif choice == "c":
                if openpyxl is None:
                    print("❌ Error: openpyxl not installed. Run 'pip install openpyxl'")
                    continue

                group_name = ask_non_empty("Input group name to add users to: ")
                path = ask_non_empty("Enter path to Excel file: ").strip('"')
                try:
                    emails = read_excel_column_a(path)
                    print(f"Found {len(emails)} entries in Column A.")

                    confirm = input(f"Proceed adding these users to group '{group_name}'? (y/N): ").lower()
                    if confirm == 'y':
                        for email in emails:
                            if validate_email(email):
                                success, message = client.add_user_to_group(group_name, email)
                                show_result(success, message)
                            else:
                                print(f"⚠️ Skipping invalid email: {email}")
                except Exception as e:
                    print(f"❌ Excel Error: {e}")

            elif choice == "d":
                group_name = ask_non_empty("Input group name to target for invitations: ")
                success, message, group = client.get_group_detail_by_name(group_name)
                show_result(success, message)

                if success and group is not None:
                    members_obj = group.get("members", [])
                    if not members_obj:
                        print("ℹ️ This group does not contain any members.")
                        continue

                    print(f"Checking {len(members_obj)} member(s) for Netskope client installations...")
                    emails_to_invite = []

                    for index, member in enumerate(members_obj, start=1):
                        user_id = member.get("value")
                        member_display = member.get("display", "Unknown User")

                        if not user_id:
                            continue

                        # Retrieve the full user identity to check email & custom attributes
                        u_success, _, user_detail = client.get_user_by_id(user_id)
                        if not u_success or not user_detail:
                            print(
                                f"⚠️ [{index}/{len(members_obj)}] Skipping {member_display}: Unable to pull user profile details.")
                            continue

                        # Find primary email address
                        emails_list = user_detail.get("emails", [])
                        primary_email = None
                        if emails_list:
                            primary_item = next((e for e in emails_list if isinstance(e, dict) and e.get("primary")),
                                                emails_list[0])
                            if isinstance(primary_item, dict):
                                primary_email = primary_item.get("value")

                        if not primary_email:
                            primary_email = user_detail.get("userName")

                        if not primary_email or not validate_email(primary_email):
                            print(
                                f"⚠️ [{index}/{len(members_obj)}] Skipping {member_display}: No valid email structure found.")
                            continue

                        # Inspect the Netskope custom extension context
                        netskope_ext = user_detail.get("urn:ietf:params:scim:schemas:extension:netskope:2.0:User", {})
                        client_status = str(netskope_ext.get("clientStatus", "Not Installed")).strip()

                        # Map uninstalled patterns safely
                        status_lower = client_status.lower()
                        if status_lower in ["not installed", "uninstalled", "", "none", "false"]:
                            print(
                                f"📥 [{index}/{len(members_obj)}] {primary_email} -> Status: '{client_status}' (Added to Invite Queue)")
                            emails_to_invite.append(primary_email)
                        else:
                            print(
                                f"✅ [{index}/{len(members_obj)}] {primary_email} -> Status: '{client_status}' (Skipped)")

                    if not emails_to_invite:
                        print(
                            "✨ Clean slate! All active members in this group already have the Netskope Client installed.")
                    else:
                        print(f"\nFound {len(emails_to_invite)} user(s) who need client deployment.")
                        confirm = input(
                            f"Send Netskope Client invitation emails to these {len(emails_to_invite)} users now? (y/N): ").strip().lower()
                        if confirm in {"y", "yes"}:
                            inv_success, inv_msg = client.send_client_invite(emails_to_invite)
                            show_result(inv_success, inv_msg)
                        else:
                            print("❌ Invitation block halted by user.")

            else:
                print("Invalid menu. Please choose 1-9, a-d, or z.")

        except KeyboardInterrupt:
            print("\nInterrupted by user. Exiting.")
            return
        except ConnectionError as exc:
            print(f"❌ Connection problem: {exc}")
        except RuntimeError as exc:
            print(f"❌ API error: {exc}")
        except Exception as exc:
            print(f"❌ Unexpected error: {exc}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Bye.")
        sys.exit(130)