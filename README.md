# Gmail Server for Model Context Protocol (MCP)

This MCP server integrates with Gmail to enable sending, removing, reading, drafting, and responding to emails.

> Note: This server enables an MCP client to read, remove, and send emails. However, the client prompts the user before conducting such activities. 

https://github.com/user-attachments/assets/5794cd16-00d2-45a2-884a-8ba0c3a90c90


## Components

### Tools

- **send-email**
  - Sends email to email address recipient 
  - Input:
    - `recipient_id` (string): Email address of addressee
    - `subject` (string): Email subject
    - `message` (string): Email content
  - Returns status and message_id

- **trash-email**
  - Moves email to trash 
  - Input:
    - `email_id` (string): Auto-generated ID of email
  - Returns success message

- **trash-emails**
  - Moves 1 to 100 messages to Gmail Trash without permanently deleting them
  - Input:
    - `email_ids` (array of strings, required): Message IDs to move to Trash
  - Returns authoritative totals and a success or failure result for every message ID

- **archive-email**
  - Removes a message from the Inbox without deleting it or marking it as read
  - Input:
    - `email_id` (string, required): ID of the message to archive
  - Returns a success response containing the archived message ID

- **archive-emails**
  - Archives 1 to 100 messages by removing only the `INBOX` label
  - Archiving does not mark messages as read and does not delete messages
  - Input:
    - `email_ids` (array of non-empty strings, required): Message IDs to archive
  - Returns authoritative totals and a success or failure result for every message ID

- **mark-email-as-read**
  - Marks email as read 
  - Input:
    - `email_id` (string): Auto-generated ID of email
  - Returns success message

- **get-unread-emails**
  - Retrieves unread messages from the Primary Inbox without modifying or marking them as read
  - Input:
    - `max_results` (integer, optional): Maximum messages to return (default: 20, maximum: 100)
  - Returns a compact list containing `id`, `thread_id`, `from`, `subject`, `date`, and `snippet`

- **search-emails**
  - Searches messages using Gmail search syntax without retrieving full bodies or modifying messages
  - Input:
    - `query` (string, required): Gmail search query, such as `from:unraid.sherros@gmail.com newer_than:30d`, `older_than:1y`, `from:example@example.com`, or `subject:invoice`
    - `max_results` (integer, optional): Maximum messages to return (default: 20, maximum: 100)
  - Returns a compact list containing `id`, `thread_id`, `from`, `subject`, `date`, and `snippet`

- **read-email**
  - Retrieves given email content
  - Input:
    - `email_id` (string): Auto-generated ID of email
  - Returns dictionary of email metadata without modifying the email

- **open-email**
  - Open email in browser
  - Input:
    - `email_id` (string): Auto-generated ID of email
  - Returns success message and opens given email in default browser


## Setup

### Gmail API Setup

1. [Create a new Google Cloud project](https://console.cloud.google.com/projectcreate)
2. [Enable the Gmail API](https://console.cloud.google.com/workspace-api/products)
3. [Configure an OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent) 
    - Select "external". However, we will not publish the app.
    - Add your personal email address as a "Test user".
4. Add OAuth scope `https://www.googleapis.com/auth/gmail/modify`
5. [Create an OAuth Client ID](https://console.cloud.google.com/apis/credentials/oauthclient) for application type "Desktop App"
6. Download the JSON file of your client's OAuth keys
7. Rename the key file and save it to your local machine in a secure location. Take note of the location.
    - The absolute path to this file will be passed as parameter `--creds-file-path` when the server is started. 

### Authentication and Docker setup

Authorization is an explicit manual operation. Normal `serve` mode requires an existing token and never opens a browser or starts an OAuth callback server.

Build the local image:

```bash
docker build -t gmail-mcp:local .
```

Create the host directories and protect them with owner-only permissions:

```bash
install -d -m 0700 "$HOME/.config/gmail-mcp"
install -d -m 0700 "$HOME/.local/share/gmail-mcp"
install -m 0600 /path/to/downloaded-client-secret.json \
  "$HOME/.config/gmail-mcp/client-secret.json"
```

The client-secret file is mounted read-only. The complete token directory is mounted read/write so token refreshes can use atomic replacement. Do not mount only `token.json`.

#### First authorization

Run a one-off authorization container. Port 8765 is published only on host loopback and only for the lifetime of this command:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --publish 127.0.0.1:8765:8765 \
  --mount type=bind,src="$HOME/.config/gmail-mcp/client-secret.json",dst=/run/credentials/client-secret.json,readonly \
  --mount type=bind,src="$HOME/.local/share/gmail-mcp",dst=/data \
  gmail-mcp:local authorize \
  --creds-file-path /run/credentials/client-secret.json \
  --token-path /data/token.json \
  --callback-host localhost \
  --callback-bind-address 0.0.0.0 \
  --callback-port 8765
```

Open the printed Google authorization URL in the Titan's browser and complete consent. The command saves the token and exits; it does not initialize Gmail or start MCP.

Confirm the token exists with owner-only permissions:

```bash
stat -c '%a %n' "$HOME/.local/share/gmail-mcp/token.json"
```

The expected mode is `600`.

#### Normal stdio operation

The image's default command is `serve`, so the following is suitable as a local stdio MCP subprocess command:

```bash
docker run --rm -i \
  --user "$(id -u):$(id -g)" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --mount type=bind,src="$HOME/.config/gmail-mcp/client-secret.json",dst=/run/credentials/client-secret.json,readonly \
  --mount type=bind,src="$HOME/.local/share/gmail-mcp",dst=/data \
  gmail-mcp:local
```

Normal operation publishes no ports, launches no browser, and performs no interactive OAuth. Access-token refresh is automatic and updates `/data/token.json` through the host-mounted directory. The `-i` option keeps stdin attached for MCP; no TTY is required.

If the token is missing, unusable, lacks a refresh token, or Google rejects its refresh, serve mode exits with an operator-facing error on stderr. Stop normal Gmail MCP usage, rerun the authorization command, and then restart the MCP client.

Google OAuth apps in Testing status can issue refresh tokens that expire after seven days for Gmail scopes. Review the OAuth app publishing status separately before relying on long-lived authorization.

Hermes configuration is intentionally not included yet. It can use `docker` as the MCP command and the normal-operation arguments above without any published port.

### Native usage

Using [uv](https://docs.astral.sh/uv/) is recommended. Authorization and serving are also available as explicit native commands:

```bash
uv run gmail authorize \
  --creds-file-path /absolute/path/to/client-secret.json \
  --token-path /absolute/path/to/token.json \
  --callback-bind-address 127.0.0.1

uv run gmail serve \
  --creds-file-path /absolute/path/to/client-secret.json \
  --token-path /absolute/path/to/token.json
```

### Troubleshooting with MCP Inspector

To test the server, use [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector).
From the git repo, run the below changing the parameter arguments accordingly.

```bash
npx @modelcontextprotocol/inspector uv run [absolute-path-to-git-repo]/src/gmail/server.py serve --creds-file-path [absolute-path-to-credentials-file] --token-path [absolute-path-to-access-tokens-file]
```
