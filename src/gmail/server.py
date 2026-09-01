from typing import Any
import argparse
import os
import asyncio
import logging
import base64
import stat
import tempfile
from email.message import EmailMessage
from email.header import decode_header
from base64 import urlsafe_b64decode
from email import message_from_bytes
import webbrowser

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio


from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.modify']


class AuthorizationRequiredError(RuntimeError):
    """Raised when serve mode cannot authenticate non-interactively."""


def secure_token_permissions(token_path: str) -> None:
    """Ensure an existing token file is accessible only by its owner."""
    mode = stat.S_IMODE(os.stat(token_path).st_mode)
    if mode != 0o600:
        logger.warning(
            "Token file %s has permissions %04o; correcting them to 0600",
            token_path,
            mode,
        )
        os.chmod(token_path, 0o600)


def save_token(token: Credentials, token_path: str) -> None:
    """Atomically save a token with owner-only permissions."""
    token_directory = os.path.dirname(os.path.abspath(token_path))
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=token_directory,
            prefix=f'.{os.path.basename(token_path)}.',
            delete=False,
        ) as token_file:
            temporary_path = token_file.name
            os.fchmod(token_file.fileno(), 0o600)
            token_file.write(token.to_json())
            token_file.flush()
            os.fsync(token_file.fileno())

        os.replace(temporary_path, token_path)
        os.chmod(token_path, 0o600)
    except Exception:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
        raise


def authorize(creds_file_path: str,
              token_path: str,
              callback_host: str,
              callback_bind_address: str,
              callback_port: int) -> None:
    """Run interactive OAuth explicitly and save the resulting token."""
    flow = InstalledAppFlow.from_client_secrets_file(creds_file_path, GMAIL_SCOPES)
    token = flow.run_local_server(
        host=callback_host,
        bind_addr=callback_bind_address,
        port=callback_port,
        open_browser=False,
    )
    save_token(token, token_path)
    logger.info("Token saved to %s", token_path)


EMAIL_ADMIN_PROMPTS = """You are an email administrator. 
You can draft, edit, read, trash, open, and send emails.
You've been given access to a specific gmail account. 
You have the following tools available:
- Send an email (send-email)
- Retrieve unread emails (get-unread-emails)
- Read email content (read-email)
- Trash email (tras-email)
- Open email in browser (open-email)
Never send an email draft or trash an email unless the user confirms first. 
Always ask for approval if not already given.
"""

# Define available prompts
PROMPTS = {
    "manage-email": types.Prompt(
        name="manage-email",
        description="Act like an email administator",
        arguments=None,
    ),
    "draft-email": types.Prompt(
        name="draft-email",
        description="Draft an email with cotent and recipient",
        arguments=[
            types.PromptArgument(
                name="content",
                description="What the email is about",
                required=True
            ),
            types.PromptArgument(
                name="recipient",
                description="Who should the email be addressed to",
                required=True
            ),
            types.PromptArgument(
                name="recipient_email",
                description="Recipient's email address",
                required=True
            ),
        ],
    ),
    "edit-draft": types.Prompt(
        name="edit-draft",
        description="Edit the existing email draft",
        arguments=[
            types.PromptArgument(
                name="changes",
                description="What changes should be made to the draft",
                required=True
            ),
            types.PromptArgument(
                name="current_draft",
                description="The current draft to edit",
                required=True
            ),
        ],
    ),
}


def decode_mime_header(header: str) -> str: 
    """Helper function to decode encoded email headers"""
    
    decoded_parts = decode_header(header)
    decoded_string = ''
    for part, encoding in decoded_parts: 
        if isinstance(part, bytes): 
            # Decode bytes to string using the specified encoding 
            decoded_string += part.decode(encoding or 'utf-8') 
        else: 
            # Already a string 
            decoded_string += part 
    return decoded_string


class GmailService:
    def __init__(self,
                 creds_file_path: str,
                 token_path: str,
                 scopes: list[str] = GMAIL_SCOPES):
        logger.info(f"Initializing GmailService with creds file: {creds_file_path}")
        self.creds_file_path = creds_file_path
        self.token_path = token_path
        self.scopes = scopes
        self.token = self._get_token()
        logger.info("Token retrieved successfully")
        self.service = self._get_service()
        logger.info("Gmail service initialized")
        self.user_email = self._get_user_email()
        logger.info(f"User email retrieved: {self.user_email}")

    def _get_token(self) -> Credentials:
        """Load or refresh a token without initiating interactive OAuth."""
        if not os.path.exists(self.token_path):
            raise AuthorizationRequiredError("OAuth token file is missing")

        try:
            secure_token_permissions(self.token_path)
            logger.info('Loading token from file')
            token = Credentials.from_authorized_user_file(self.token_path, self.scopes)
        except (OSError, ValueError) as error:
            raise AuthorizationRequiredError("OAuth token file is unusable") from error

        if not token.refresh_token:
            raise AuthorizationRequiredError("OAuth token has no refresh token")

        if not token.valid:
            if token.expired:
                logger.info('Refreshing token')
                try:
                    token.refresh(Request())
                except RefreshError as error:
                    raise AuthorizationRequiredError(
                        "OAuth token refresh was rejected"
                    ) from error
                save_token(token, self.token_path)
                logger.info('Token saved to %s', self.token_path)
            else:
                raise AuthorizationRequiredError("OAuth token is unusable")

        return token

    def _get_service(self) -> Any:
        """Initialize Gmail API service"""
        try:
            service = build('gmail', 'v1', credentials=self.token)
            return service
        except HttpError as error:
            logger.error(f'An error occurred building Gmail service: {error}')
            raise ValueError(f'An error occurred: {error}')
    
    def _get_user_email(self) -> str:
        """Get user email address"""
        profile = self.service.users().getProfile(userId='me').execute()
        user_email = profile.get('emailAddress', '')
        return user_email
    
    async def send_email(self, recipient_id: str, subject: str, message: str,) -> dict:
        """Creates and sends an email message"""
        try:
            message_obj = EmailMessage()
            message_obj.set_content(message)
            
            message_obj['To'] = recipient_id
            message_obj['From'] = self.user_email
            message_obj['Subject'] = subject

            encoded_message = base64.urlsafe_b64encode(message_obj.as_bytes()).decode()
            create_message = {'raw': encoded_message}
            
            send_message = await asyncio.to_thread(
                self.service.users().messages().send(userId="me", body=create_message).execute
            )
            logger.info(f"Message sent: {send_message['id']}")
            return {"status": "success", "message_id": send_message["id"]}
        except HttpError as error:
            return {"status": "error", "error_message": str(error)}

    async def open_email(self, email_id: str) -> str:
        """Opens email in browser given ID."""
        try:
            url = f"https://mail.google.com/#all/{email_id}"
            webbrowser.open(url, new=0, autoraise=True)
            return "Email opened in browser successfully."
        except HttpError as error:
            return f"An HttpError occurred: {str(error)}"

    async def get_unread_emails(self, max_results: int = 20) -> list[dict[str, str]] | str:
        """Retrieve compact metadata for unread messages in the Primary Inbox."""
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ValueError("max_results must be an integer")
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100")

        try:
            user_id = 'me'
            query = 'in:inbox is:unread category:primary'

            response = self.service.users().messages().list(
                userId=user_id,
                q=query,
                maxResults=max_results,
            ).execute()

            emails = []
            for message in response.get('messages', []):
                metadata = self.service.users().messages().get(
                    userId=user_id,
                    id=message['id'],
                    format='metadata',
                    metadataHeaders=['From', 'Subject', 'Date'],
                ).execute()
                headers = {
                    header['name'].lower(): header.get('value', '')
                    for header in metadata.get('payload', {}).get('headers', [])
                }
                emails.append({
                    'id': metadata.get('id', message['id']),
                    'thread_id': metadata.get('threadId', message.get('threadId', '')),
                    'from': decode_mime_header(headers.get('from', '')),
                    'subject': decode_mime_header(headers.get('subject', '')),
                    'date': headers.get('date', ''),
                    'snippet': metadata.get('snippet', ''),
                })
            return emails

        except HttpError as error:
            return f"An HttpError occurred: {str(error)}"

    async def read_email(self, email_id: str) -> dict[str, str]| str:
        """Retrieves email contents including to, from, subject, and contents."""
        try:
            msg = self.service.users().messages().get(userId="me", id=email_id, format='raw').execute()
            email_metadata = {}

            # Decode the base64URL encoded raw content
            raw_data = msg['raw']
            decoded_data = urlsafe_b64decode(raw_data)

            # Parse the RFC 2822 email
            mime_message = message_from_bytes(decoded_data)

            # Extract the email body
            body = None
            if mime_message.is_multipart():
                for part in mime_message.walk():
                    # Extract the text/plain part
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode()
                        break
            else:
                # For non-multipart messages
                body = mime_message.get_payload(decode=True).decode()
            email_metadata['content'] = body
            
            # Extract metadata
            email_metadata['subject'] = decode_mime_header(mime_message.get('subject', ''))
            email_metadata['from'] = mime_message.get('from','')
            email_metadata['to'] = mime_message.get('to','')
            email_metadata['date'] = mime_message.get('date','')
            
            logger.info(f"Email read: {email_id}")
            
            return email_metadata
        except HttpError as error:
            return f"An HttpError occurred: {str(error)}"
        
    async def trash_email(self, email_id: str) -> str:
        """Moves email to trash given ID."""
        try:
            self.service.users().messages().trash(userId="me", id=email_id).execute()
            logger.info(f"Email moved to trash: {email_id}")
            return "Email moved to trash successfully."
        except HttpError as error:
            return f"An HttpError occurred: {str(error)}"
        
    async def mark_email_as_read(self, email_id: str) -> str:
        """Marks email as read given ID."""
        try:
            self.service.users().messages().modify(userId="me", id=email_id, body={'removeLabelIds': ['UNREAD']}).execute()
            logger.info(f"Email marked as read: {email_id}")
            return "Email marked as read."
        except HttpError as error:
            return f"An HttpError occurred: {str(error)}"
  
async def main(creds_file_path: str,
               token_path: str):
    
    gmail_service = GmailService(creds_file_path, token_path)
    server = Server("gmail")

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return list(PROMPTS.values())

    @server.get_prompt()
    async def get_prompt(
        name: str, arguments: dict[str, str] | None = None
    ) -> types.GetPromptResult:
        if name not in PROMPTS:
            raise ValueError(f"Prompt not found: {name}")

        if name == "manage-email":
            return types.GetPromptResult(
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(
                            type="text",
                            text=EMAIL_ADMIN_PROMPTS,
                        )
                    )
                ]
            )

        if name == "draft-email":
            content = arguments.get("content", "")
            recipient = arguments.get("recipient", "")
            recipient_email = arguments.get("recipient_email", "")
            
            # First message asks the LLM to create the draft
            return types.GetPromptResult(
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(
                            type="text",
                            text=f"""Please draft an email about {content} for {recipient} ({recipient_email}).
                            Include a subject line starting with 'Subject:' on the first line.
                            Do not send the email yet, just draft it and ask the user for their thoughts."""
                        )
                    )
                ]
            )
        
        elif name == "edit-draft":
            changes = arguments.get("changes", "")
            current_draft = arguments.get("current_draft", "")
            
            # Edit existing draft based on requested changes
            return types.GetPromptResult(
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(
                            type="text",
                            text=f"""Please revise the current email draft:
                            {current_draft}
                            
                            Requested changes:
                            {changes}
                            
                            Please provide the updated draft."""
                        )
                    )
                ]
            )

        raise ValueError("Prompt implementation not found")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="send-email",
                description="""Sends email to recipient. 
                Do not use if user only asked to draft email. 
                Drafts must be approved before sending.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "recipient_id": {
                            "type": "string",
                            "description": "Recipient email address",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject",
                        },
                        "message": {
                            "type": "string",
                            "description": "Email content text",
                        },
                    },
                    "required": ["recipient_id", "subject", "message"],
                },
            ),
            types.Tool(
                name="trash-email",
                description="""Moves email to trash. 
                Confirm before moving email to trash.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "Email ID",
                        },
                    },
                    "required": ["email_id"],
                },
            ),
            types.Tool(
                name="get-unread-emails",
                description=(
                    "Retrieve compact metadata for unread messages in the Primary Inbox "
                    "without marking them read. Returns id, thread_id, from, subject, "
                    "date, and snippet."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of unread emails to return (default 20, maximum 100)",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": []
                },
            ),
            types.Tool(
                name="read-email",
                description="Retrieves given email content without modifying it",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "Email ID",
                        },
                    },
                    "required": ["email_id"],
                },
            ),
            types.Tool(
                name="mark-email-as-read",
                description="Marks given email as read",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "Email ID",
                        },
                    },
                    "required": ["email_id"],
                },
            ),
            types.Tool(
                name="open-email",
                description="Open email in browser",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "Email ID",
                        },
                    },
                    "required": ["email_id"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:

        if name == "send-email":
            recipient = arguments.get("recipient_id")
            if not recipient:
                raise ValueError("Missing recipient parameter")
            subject = arguments.get("subject")
            if not subject:
                raise ValueError("Missing subject parameter")
            message = arguments.get("message")
            if not message:
                raise ValueError("Missing message parameter")
                
            # Extract subject and message content
            email_lines = message.split('\n')
            if email_lines[0].startswith('Subject:'):
                subject = email_lines[0][8:].strip()
                message_content = '\n'.join(email_lines[1:]).strip()
            else:
                message_content = message
                
            send_response = await gmail_service.send_email(recipient, subject, message_content)
            
            if send_response["status"] == "success":
                response_text = f"Email sent successfully. Message ID: {send_response['message_id']}"
            else:
                response_text = f"Failed to send email: {send_response['error_message']}"
            return [types.TextContent(type="text", text=response_text)]

        if name == "get-unread-emails":
            arguments = arguments or {}
            max_results = arguments.get("max_results", 20)
            unread_emails = await gmail_service.get_unread_emails(max_results)
            return [types.TextContent(type="text", text=str(unread_emails),artifact={"type": "json", "data": unread_emails} )]
        
        if name == "read-email":
            email_id = arguments.get("email_id")
            if not email_id:
                raise ValueError("Missing email ID parameter")
                
            retrieved_email = await gmail_service.read_email(email_id)
            return [types.TextContent(type="text", text=str(retrieved_email),artifact={"type": "dictionary", "data": retrieved_email} )]
        if name == "open-email":
            email_id = arguments.get("email_id")
            if not email_id:
                raise ValueError("Missing email ID parameter")
                
            msg = await gmail_service.open_email(email_id)
            return [types.TextContent(type="text", text=str(msg))]
        if name == "trash-email":
            email_id = arguments.get("email_id")
            if not email_id:
                raise ValueError("Missing email ID parameter")
                
            msg = await gmail_service.trash_email(email_id)
            return [types.TextContent(type="text", text=str(msg))]
        if name == "mark-email-as-read":
            email_id = arguments.get("email_id")
            if not email_id:
                raise ValueError("Missing email ID parameter")
                
            msg = await gmail_service.mark_email_as_read(email_id)
            return [types.TextContent(type="text", text=str(msg))]
        else:
            logger.error(f"Unknown tool: {name}")
            raise ValueError(f"Unknown tool: {name}")

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="gmail",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def create_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for authorization and MCP serving."""
    parser = argparse.ArgumentParser(description='Gmail API MCP Server')
    commands = parser.add_subparsers(dest='command', required=True)

    def add_credential_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            '--creds-file-path',
            required=True,
            help='OAuth 2.0 credentials file path',
        )
        command_parser.add_argument(
            '--token-path',
            required=True,
            help='File location to store and retrieve OAuth tokens',
        )

    serve_parser = commands.add_parser(
        'serve',
        help='Run the stdio MCP server using an existing token',
    )
    add_credential_arguments(serve_parser)

    authorize_parser = commands.add_parser(
        'authorize',
        help='Run the interactive OAuth flow without starting MCP',
    )
    add_credential_arguments(authorize_parser)
    authorize_parser.add_argument(
        '--callback-host',
        default='localhost',
        help='Hostname advertised in the OAuth redirect URI',
    )
    authorize_parser.add_argument(
        '--callback-bind-address',
        default='0.0.0.0',
        help='Address on which the callback listener binds',
    )
    authorize_parser.add_argument(
        '--callback-port',
        type=int,
        default=8765,
        help='Fixed OAuth callback port',
    )

    return parser


def cli_main() -> None:
    """Run the selected command-line mode."""
    parser = create_argument_parser()
    args = parser.parse_args()

    if args.command == 'authorize':
        authorize(
            args.creds_file_path,
            args.token_path,
            args.callback_host,
            args.callback_bind_address,
            args.callback_port,
        )
        return

    try:
        asyncio.run(main(args.creds_file_path, args.token_path))
    except AuthorizationRequiredError as error:
        parser.exit(
            1,
            f"Authorization required: {error}. Run the 'gmail authorize' command.\n",
        )


if __name__ == "__main__":
    cli_main()
