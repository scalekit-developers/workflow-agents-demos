"""
main_polling.py - Scalekit-Native Slack Triage Agent (Polling Mode)

This module implements a pure Scalekit approach for monitoring Slack channels.
Instead of webhooks, it periodically polls Slack channels via Scalekit's API
to fetch new messages and process them.

Architecture:
1. Poll Slack channels via Scalekit's slack_fetch_conversation_history tool
2. Track processed messages to avoid duplicates
3. Process new messages through routing logic
4. Execute actions via Scalekit (GitHub issues, Zendesk tickets)
5. Post responses back to Slack via Scalekit's slack_send_message tool

Benefits:
- No separate Slack bot needed
- Pure Scalekit integration
- Simpler OAuth setup
- Unified API for all services
"""

import json
import time
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Set, Optional

from flask import Flask, jsonify
from markupsafe import escape

from logging_config import setup_logging
from routing import get_router
from settings import Settings
from sk_connectors import get_connector

# Setup logging
logger = setup_logging(__name__)

# Global state
_shutdown_requested = False

def _signal_handler(sig, frame):
    """Handle Ctrl+C and SIGTERM gracefully."""
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True

# Initialize Flask app for health checks and auth endpoints
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# Initialize router
try:
    use_llm_routing = bool(Settings.OPENAI_API_KEY)
    router = get_router(use_llm=use_llm_routing)
    logger.info(f"Router initialized (LLM: {'enabled' if use_llm_routing else 'disabled'})")
except Exception as e:
    logger.error(f"Failed to initialize router: {e}")
    router = None

# Initialize Scalekit connector
try:
    connector = get_connector()
    logger.info("Scalekit connector initialized")
except Exception as e:
    logger.error(f"Failed to initialize Scalekit connector: {e}")
    connector = None

# Track processed message timestamps to avoid duplicates
processed_messages: Dict[str, Set[str]] = {}  # channel_id -> set of message timestamps

# Store last poll time for each channel
last_poll_time: Dict[str, float] = {}  # channel_id -> timestamp


def load_user_mappings() -> Dict[str, Any]:
    """Load user mappings from JSON file."""
    mapping_file = Path(Settings.USER_MAPPING_FILE)
    if not mapping_file.exists():
        logger.warning(f"User mapping file not found: {Settings.USER_MAPPING_FILE}")
        return {}

    try:
        with open(mapping_file, 'r') as f:
            data = json.load(f)

        # Filter out metadata keys (starting with _)
        mappings = {k: v for k, v in data.items() if not k.startswith('_')}

        logger.info(f"Loaded {len(mappings)} user mappings from {Settings.USER_MAPPING_FILE}")
        return mappings
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in {Settings.USER_MAPPING_FILE}: {e}")
        return {}
    except Exception as e:
        logger.error(f"Failed to load user mappings: {e}")
        return {}


def get_slack_identifier(user_id: str, user_mappings: Dict) -> Optional[str]:
    """
    Get Scalekit identifier for a Slack user.

    Args:
        user_id: Slack user ID (e.g., U01234567)
        user_mappings: Dictionary of user mappings

    Returns:
        Scalekit identifier (email or custom ID), or None if not found
    """
    if user_id not in user_mappings:
        logger.debug(f"User {user_id} not mapped in {Settings.USER_MAPPING_FILE}")
        return None

    identifier = user_mappings[user_id].get('scalekit_identifier')
    if not identifier:
        logger.debug(f"No scalekit_identifier for user {user_id}")
        return None

    return identifier


def fetch_channel_messages(channel_id: str, identifier: str, limit: int = 10) -> List[Dict]:
    """
    Fetch recent messages from a Slack channel via Scalekit, including all paginated pages.

    Args:
        channel_id: Slack channel ID
        identifier: Scalekit user identifier
        limit: Number of messages per page to fetch

    Returns:
        List of message dictionaries (all pages combined)
    """
    try:
        # Use last poll time if available, otherwise use lookback period
        if channel_id in last_poll_time:
            oldest_time = last_poll_time[channel_id]
            readable_time = datetime.fromtimestamp(oldest_time).strftime('%Y-%m-%d %H:%M:%S')
            logger.debug(f"Fetching messages from {channel_id} since {readable_time}")
        else:
            # First poll - use lookback period to catch recent messages
            lookback_seconds = Settings.RESYNC_LOOKBACK_SECONDS if Settings.RESYNC_ON_START else Settings.POLL_LOOKBACK_SECONDS
            oldest_time = time.time() - lookback_seconds
            readable_time = datetime.fromtimestamp(oldest_time).strftime('%Y-%m-%d %H:%M:%S')
            logger.debug(f"First poll for {channel_id}, looking back {lookback_seconds}s to {readable_time}")

        if not connector:
            logger.error("Connector not initialized")
            return []

        all_messages = []
        cursor = None
        page_count = 0

        # Paginate through all available messages
        while True:
            page_count += 1
            params = {
                'channel': channel_id,
                'limit': limit,
                'oldest': str(oldest_time)
            }
            if cursor:
                params['cursor'] = cursor

            result = connector.execute_action_with_retry(
                identifier=identifier,
                tool='slack_fetch_conversation_history',
                connection_name=Settings.SCALEKIT_SLACK_CONNECTION,
                parameters=params
            )

            if not result:
                logger.debug(f"No result from Slack fetch page {page_count} for {channel_id}")
                break

            # Extract messages from result
            messages = []
            if hasattr(result, 'data') and isinstance(result.data, dict):
                messages = result.data.get('messages', [])
            elif isinstance(result, dict):
                messages = result.get('messages', [])

            if messages:
                all_messages.extend(messages)
                logger.debug(f"Fetched page {page_count}: {len(messages)} messages from {channel_id}")

            # Check for pagination cursor
            has_more = False
            if hasattr(result, 'data') and isinstance(result.data, dict):
                cursor = result.data.get('response_metadata', {}).get('next_cursor')
                has_more = bool(cursor)
            elif isinstance(result, dict):
                cursor = result.get('response_metadata', {}).get('next_cursor')
                has_more = bool(cursor)

            if not has_more:
                break

        if all_messages:
            logger.info(f"Fetched {len(all_messages)} total messages from {channel_id} ({page_count} pages)")

        # Don't update checkpoint here - defer to caller after processing completes
        return all_messages

    except Exception as e:
        logger.error(f"Error fetching messages from {channel_id}: {e}", exc_info=True)
        return []


def _update_channel_checkpoint(channel_id: str, messages: List[Dict]) -> None:
    """
    Update the poll checkpoint for a channel after messages are processed.

    Should only be called after successful processing of all messages.
    """
    if not messages:
        # No messages: keep a small overlap to avoid racing with new arrivals
        boundary = time.time() - float(Settings.POLL_OVERLAP_SECONDS)
        last_poll_time[channel_id] = max(0.0, boundary)
        return

    try:
        max_ts = max(float(m.get('ts')) for m in messages if m.get('ts'))
        last_poll_time[channel_id] = max_ts - float(Settings.POLL_OVERLAP_SECONDS)
        readable_next = datetime.fromtimestamp(last_poll_time[channel_id]).strftime('%Y-%m-%d %H:%M:%S')
        logger.debug(f"Next poll for {channel_id} will start at {readable_next}")
    except Exception as e:
        logger.debug(f"Error updating poll checkpoint: {e}, using current time")
        last_poll_time[channel_id] = time.time() - float(Settings.POLL_OVERLAP_SECONDS)

    except Exception as e:
        logger.error(f"Error fetching messages from {channel_id}: {e}", exc_info=True)
        return []


def is_message_processed(channel_id: str, message_ts: str) -> bool:
    """Check if a message has already been processed."""
    if channel_id not in processed_messages:
        processed_messages[channel_id] = set()

    return message_ts in processed_messages[channel_id]


def mark_message_processed(channel_id: str, message_ts: str):
    """Mark a message as processed."""
    if channel_id not in processed_messages:
        processed_messages[channel_id] = set()

    processed_messages[channel_id].add(message_ts)


def should_process_message(message: Dict) -> bool:
    """
    Determine if a message should be processed.

    Filters out:
    - Bot messages (unless from a real user)
    - Message edits/deletes
    - Empty messages
    - Thread replies (optional)
    """
    # Must have text content
    if not message.get('text'):
        return False

    # Ignore message edits/deletes
    if message.get('subtype') in ['message_changed', 'message_deleted']:
        return False

    # Allow messages from real users (has 'user' field)
    # Even if posted via API (might have bot_id but user takes precedence)
    if message.get('user'):
        return True

    # Ignore pure bot messages (no user field)
    if message.get('bot_id') or message.get('subtype') == 'bot_message':
        return False

    # Ignore thread replies (optional - remove this to process thread messages)
    if message.get('thread_ts') and message.get('thread_ts') != message.get('ts'):
        return False

    return True


def process_message(message: Dict, channel_id: str, user_mappings: Dict) -> bool:
    """
    Process a single Slack message through the routing logic.

    Args:
        message: Slack message dictionary
        channel_id: Channel ID where message was posted
        user_mappings: User mapping dictionary

    Returns:
        True if processed successfully, False otherwise
    """
    try:
        user_id = message.get('user')
        message_text = message.get('text', '')
        message_ts = message.get('ts')
        message_length = len(message_text)

        logger.debug(f"Processing message from {user_id} in {channel_id} ({message_length} chars)")

        # Get Scalekit identifier for user
        identifier = get_slack_identifier(user_id, user_mappings)
        if not identifier:
            logger.debug(f"User {user_id} not mapped - skipping message")
            return False

        # Route the message
        if not router:
            logger.error("Router not initialized")
            return False

        routing_result = router.route_message(
            message=message_text,
            user_id=user_id,
            channel_id=channel_id
        )

        # routing_result is an ActionResult object with success/message/data
        if not routing_result or not routing_result.success:
            logger.debug(f"Routing failed: {routing_result.error if routing_result else 'Unknown error'}")
            return False

        # Get the action from the result data
        action = routing_result.data.get('action') if routing_result.data else None
        logger.debug(f"Routing decision: {action}")

        if not action or action == 'ignore' or action == 'none':
            logger.debug(f"No action required - message ignored")
            return True

        # Execute the action
        action_result = execute_action(
            action=action,
            message_text=message_text,
            user_id=user_id,
            identifier=identifier,
            channel_id=channel_id,
            thread_ts=message_ts,
            user_mappings=user_mappings
        )

        return action_result.get('success', False) if action_result else False

    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        return False


def execute_action(
    action: str,
    message_text: str,
    user_id: str,
    identifier: str,
    channel_id: str,
    thread_ts: str,
    user_mappings: Dict
) -> Dict:
    """
    Execute an action based on routing decision.

    Args:
        action: Action to execute (e.g., 'github_issue_create')
        message_text: Original message text
        user_id: Slack user ID
        identifier: Scalekit identifier
        channel_id: Slack channel ID
        thread_ts: Message timestamp (for threading replies)
        user_mappings: User mappings dictionary

    Returns:
        Dictionary with action result
    """
    try:
        logger.info(f"Executing action: {action} for user {user_id}")

        if action == 'github_issue_create':
            return execute_github_action(
                message_text, identifier, user_mappings,
                user_id, channel_id, thread_ts
            )
        elif action == 'zendesk_create_ticket':
            return execute_zendesk_action(
                message_text, identifier, user_mappings,
                user_id, channel_id, thread_ts
            )
        else:
            logger.warning(f"Unknown action: {action}")
            return {'success': False, 'error': 'Unknown action'}

    except Exception as e:
        logger.error(f"Error executing action {action}: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


def execute_github_action(
    message_text: str,
    identifier: str,
    user_mappings: Dict,
    user_id: str,
    channel_id: str,
    thread_ts: str
) -> Dict:
    """Execute GitHub issue creation."""
    try:
        logger.debug(f"Creating GitHub issue from user {user_id} in {channel_id}")

        if not connector:
            logger.error("Connector not initialized")
            return {"success": False, "error": "Connector not initialized"}

        # Get user's GitHub username
        github_username = user_mappings.get(user_id, {}).get('github_username')

        # Create issue via Scalekit
        result = connector.execute_action_with_retry(
            identifier=identifier,
            tool='github_issue_create',
            connection_name=Settings.SCALEKIT_GITHUB_CONNECTION,
            parameters={
                'title': f"[Slack Triage] {message_text[:50]}",
                'body': f"**From Slack:**\n\n{message_text}\n\n**Reporter:** {github_username or user_id}",
                'assignees': [github_username] if github_username else []
            }
        )

        if not result:
            logger.error("Failed to create GitHub issue: no result returned")
            send_slack_message(
                channel_id=channel_id,
                identifier=identifier,
                text="❌ Failed to create GitHub issue",
                thread_ts=thread_ts
            )
            return {"success": False, "error": "empty result"}

        # Handle common shapes: raw GitHub issue dict or nested variants
        issue_number = (
            result.get("number")
            or result.get("issue", {}).get("number")
            or result.get("data", {}).get("number")
        )
        issue_url = (
            result.get("html_url")
            or result.get("issue", {}).get("html_url")
            or result.get("data", {}).get("html_url")
        )

        if issue_number and issue_url:
            logger.info(f"GitHub issue #{issue_number} created successfully")
            send_slack_message(
                channel_id=channel_id,
                identifier=identifier,
                text=f"✅ GitHub Issue #{issue_number}\n🔗 {issue_url}",
                thread_ts=thread_ts
            )
            return {"success": True, "issue_number": issue_number, "issue_url": issue_url}
        else:
            logger.error(f"Unexpected GitHub result shape: {list(result.keys()) if isinstance(result, dict) else type(result)}")
            send_slack_message(
                channel_id=channel_id,
                identifier=identifier,
                text="❌ GitHub issue creation failed (unexpected response)",
                thread_ts=thread_ts
            )
            return {"success": False, "error": "unexpected result shape"}

    except Exception as e:
        logger.error(f"Error creating GitHub issue: {e}", exc_info=True)
        send_slack_message(
            channel_id=channel_id,
            identifier=identifier,
            text="❌ GitHub issue creation failed",
            thread_ts=thread_ts
        )
        return {"success": False, "error": "issue creation failed"}


def execute_zendesk_action(
    message_text: str,
    identifier: str,
    user_mappings: Dict,
    user_id: str,
    channel_id: str,
    thread_ts: str
) -> Dict:
    """Execute Zendesk ticket creation."""
    try:
        logger.debug(f"Creating Zendesk ticket from user {user_id} in {channel_id}")

        if not connector:
            logger.error("Connector not initialized")
            return {"success": False, "error": "Connector not initialized"}

        # Get requester email from user mapping - must be a real email
        requester_email = user_mappings.get(user_id, {}).get('scalekit_identifier')
        if not requester_email or '@' not in requester_email:
            logger.error(f"Invalid requester email for user {user_id}: {requester_email}")
            return {"success": False, "error": "User email not configured"}

        # Create ticket via Scalekit
        result = connector.execute_action_with_retry(
            identifier=identifier,
            tool='zendesk_create_ticket',
            connection_name=Settings.SCALEKIT_ZENDESK_CONNECTION,
            parameters={
                'subject': f"[Slack Triage] {message_text[:80]}",
                'description': message_text,
                'requester_email': requester_email,
                'priority': 'normal'
            }
        )

        if not result:
            logger.error("Failed to create Zendesk ticket: no result returned")
            send_slack_message(
                channel_id=channel_id,
                identifier=identifier,
                text="❌ Failed to create Zendesk ticket",
                thread_ts=thread_ts
            )
            return {"success": False, "error": "empty result"}

        # Handle common shapes: raw Zendesk ticket dict or nested variants
        ticket_id = (
            result.get("id")
            or result.get("ticket", {}).get("id")
            or result.get("data", {}).get("id")
        )
        ticket_url = (
            result.get("url")
            or result.get("ticket", {}).get("url")
            or result.get("data", {}).get("url")
        )

        if ticket_id:
            logger.info(f"Zendesk ticket #{ticket_id} created successfully")
            # Format message with link if available
            if ticket_url:
                slack_text = f"✅ Zendesk Ticket #{ticket_id}\n🔗 {ticket_url}"
            else:
                slack_text = f"✅ Zendesk Ticket #{ticket_id} created"

            send_slack_message(
                channel_id=channel_id,
                identifier=identifier,
                text=slack_text,
                thread_ts=thread_ts
            )
            return {"success": True, "ticket_id": ticket_id, "ticket_url": ticket_url}
        else:
            logger.error(f"Unexpected Zendesk result shape: {list(result.keys()) if isinstance(result, dict) else type(result)}")
            send_slack_message(
                channel_id=channel_id,
                identifier=identifier,
                text="❌ Zendesk ticket creation failed (unexpected response)",
                thread_ts=thread_ts
            )
            return {"success": False, "error": "unexpected result shape"}

    except Exception as e:
        logger.error(f"Error creating Zendesk ticket: {e}", exc_info=True)
        send_slack_message(
            channel_id=channel_id,
            identifier=identifier,
            text="❌ Zendesk ticket creation failed",
            thread_ts=thread_ts
        )
        return {"success": False, "error": "ticket creation failed"}


def send_slack_message(channel_id: str, identifier: str, text: str, thread_ts: Optional[str] = None) -> bool:
    """
    Send a message to Slack via Scalekit.

    Args:
        channel_id: Channel to post to
        identifier: Scalekit user identifier
        text: Message text
        thread_ts: Optional thread timestamp for replies

    Returns:
        True if successful, False otherwise
    """
    try:
        if not connector:
            logger.error("Connector not initialized")
            return False

        params = {
            'channel': channel_id,
            'text': text
        }

        if thread_ts:
            params['thread_ts'] = thread_ts

        result = connector.execute_action_with_retry(
            identifier=identifier,
            tool='slack_send_message',
            connection_name=Settings.SCALEKIT_SLACK_CONNECTION,
            parameters=params
        )

        if result:
            logger.debug(f"Posted message to Slack channel {channel_id}")
            return True
        else:
            logger.debug(f"Failed to post to Slack channel {channel_id}")
            return False

    except Exception as e:
        logger.error(f"Error sending Slack message: {e}", exc_info=True)
        return False


def poll_channels(user_mappings: Dict):
    """
    Poll all configured Slack channels for new messages.

    This runs continuously in a loop, checking each channel
    at regular intervals.

    Raises:
        ValueError: If required configuration is missing
    """
    global _shutdown_requested

    if not Settings.ALLOWED_CHANNELS:
        raise ValueError("No channels configured in ALLOWED_CHANNELS")

    if not user_mappings:
        raise ValueError("No users configured in user_mapping.json")

    # Get first user's identifier for fetching messages
    first_user = list(user_mappings.values())[0]
    identifier = first_user.get('scalekit_identifier')
    if not identifier:
        raise ValueError("No scalekit_identifier found for first user")

    logger.info(f"Starting polling loop for {len(Settings.ALLOWED_CHANNELS)} channels")
    logger.info(f"Poll interval: {Settings.POLL_INTERVAL_SECONDS}s")

    poll_cycle = 0

    while not _shutdown_requested:
        try:
            poll_cycle += 1
            logger.debug(f"Polling cycle #{poll_cycle} starting...")

            processed_count = 0

            for channel_id in Settings.ALLOWED_CHANNELS:
                # Skip denied channels
                if channel_id in Settings.DENIED_CHANNELS:
                    logger.debug(f"Skipping denied channel {channel_id}")
                    continue

                # Fetch messages
                first_fetch = channel_id not in last_poll_time
                messages = fetch_channel_messages(channel_id, identifier)

                # One-time fallback: If first fetch returns 0 messages and a larger
                # fallback window is configured, attempt a single wider fetch
                if first_fetch and not messages and Settings.POLL_EMPTY_FALLBACK_SECONDS > Settings.POLL_LOOKBACK_SECONDS:
                    try:
                        lookback_seconds = Settings.POLL_EMPTY_FALLBACK_SECONDS
                        oldest_time = time.time() - lookback_seconds
                        logger.debug(f"First poll empty for {channel_id}; retrying with wider lookback {lookback_seconds}s")

                        if not connector:
                            continue

                        result = connector.execute_action_with_retry(
                            identifier=identifier,
                            tool='slack_fetch_conversation_history',
                            connection_name=Settings.SCALEKIT_SLACK_CONNECTION,
                            parameters={
                                'channel': channel_id,
                                'limit': 10,
                                'oldest': str(oldest_time)
                            }
                        )
                        alt_messages = []
                        if result:
                            if hasattr(result, 'data') and isinstance(result.data, dict):
                                alt_messages = result.data.get('messages', [])
                            elif isinstance(result, dict):
                                alt_messages = result.get('messages', [])

                        if alt_messages:
                            logger.debug(f"Fallback fetch returned {len(alt_messages)} messages for {channel_id}")
                            messages = alt_messages
                    except Exception as e:
                        logger.debug(f"Fallback fetch error for {channel_id}: {e}")

                # Process each message
                for message in reversed(messages):  # Process oldest first
                    if _shutdown_requested:
                        break

                    message_ts = message.get('ts')
                    message_text = message.get('text', '')[:80]

                    # Skip if already processed
                    if is_message_processed(channel_id, message_ts):
                        logger.debug(f"Already processed message {message_ts} in {channel_id}")
                        continue

                    # Check if message should be processed
                    if not should_process_message(message):
                        logger.debug(f"Skipping non-user message in {channel_id}")
                        mark_message_processed(channel_id, message_ts)
                        continue

                    # Process the message
                    success = process_message(message, channel_id, user_mappings)
                    if success:
                        processed_count += 1

                    # Mark as processed
                    mark_message_processed(channel_id, message_ts)

                # Update checkpoint only after all messages for this channel are processed
                _update_channel_checkpoint(channel_id, messages)

            if processed_count > 0:
                logger.info(f"Polling cycle #{poll_cycle}: processed {processed_count} message(s)")
            else:
                logger.debug(f"Polling cycle #{poll_cycle}: no messages to process")

            # Wait before next poll (with shutdown flag check)
            remaining_sleep = Settings.POLL_INTERVAL_SECONDS
            while remaining_sleep > 0 and not _shutdown_requested:
                sleep_chunk = min(1.0, remaining_sleep)
                time.sleep(sleep_chunk)
                remaining_sleep -= sleep_chunk

        except Exception as e:
            logger.error(f"Error in polling cycle #{poll_cycle}: {e}", exc_info=True)
            # Wait before retry (respecting shutdown)
            remaining_sleep = Settings.POLL_INTERVAL_SECONDS
            while remaining_sleep > 0 and not _shutdown_requested:
                sleep_chunk = min(1.0, remaining_sleep)
                time.sleep(sleep_chunk)
                remaining_sleep -= sleep_chunk

    logger.info("Polling loop stopped")
    logger.info(f"Processed {poll_cycle} polling cycles total")


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'service': 'slack-triage-agent-polling',
        'mode': 'polling',
        'config': Settings.get_summary()
    }), 200


@app.route('/auth/init', methods=['GET'])
def auth_init():
    """
    Initialize OAuth flow for connecting user accounts.

    Query params:
        user_id: Slack user ID
        service: Service to connect (slack, github, zendesk)
    """
    from flask import request
    from markupsafe import escape

    user_id = request.args.get('user_id')
    service = request.args.get('service')

    if not user_id or not service:
        return jsonify({'error': 'Missing user_id or service parameter'}), 400

    # Load user mappings
    user_mappings = load_user_mappings()
    identifier = get_slack_identifier(user_id, user_mappings)

    if not identifier:
        return jsonify({'error': f'User {user_id} not found in user_mapping.json'}), 404

    # Build redirect URI (where Scalekit will redirect after OAuth)
    # Use configured redirect URI, or default to localhost
    redirect_uri = Settings.OAUTH_REDIRECT_URI or f"http://localhost:{Settings.FLASK_PORT}/auth/callback"

    # Generate auth URL
    auth_url = connector.get_authorization_url(service, identifier, redirect_uri)

    if not auth_url:
        return jsonify({'error': 'Failed to generate authorization URL'}), 500

    # Redirect to auth URL
    from flask import redirect
    return redirect(auth_url)


@app.route('/auth/callback', methods=['GET'])
@app.route('/callback', methods=['GET'])
def auth_callback():
    """
    OAuth callback endpoint.

    After user authorizes a service, Scalekit redirects here with authorization code.
    The code is automatically handled by Scalekit - we just show success message.

    Supports both /auth/callback and /callback paths for flexibility.
    """
    from flask import request

    # Get query parameters from OAuth callback
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description', '')

    if error:
        return f"""
        <html>
            <body style="font-family: Arial, sans-serif; padding: 50px; text-align: center;">
                <h1 style="color: #d32f2f;">❌ Authorization Failed</h1>
                <p><strong>Error:</strong> {escape(error)}</p>
                <p><strong>Description:</strong> {escape(error_description)}</p>
                <p><a href="/">Go back</a></p>
            </body>
        </html>
        """, 400

    if code:
        return """
        <html>
            <body style="font-family: Arial, sans-serif; padding: 50px; text-align: center;">
                <h1 style="color: #4caf50;">✅ Authorization Successful!</h1>
                <p>You have successfully authorized the connection.</p>
                <p>You can now close this window and return to Slack.</p>
                <p><a href="/">Go to home</a></p>
            </body>
        </html>
        """

    return "Invalid callback", 400


@app.route('/users', methods=['GET'])
def list_users():
    """List all mapped users."""
    user_mappings = load_user_mappings()
    return jsonify({
        'count': len(user_mappings),
        'users': list(user_mappings.keys())
    }), 200


def run_polling_mode():
    """Run the agent in polling mode."""
    logger.info("Starting Slack Triage Agent (Polling Mode)")
    logger.info(f"Configuration: {Settings.get_summary()}")

    # Load user mappings
    user_mappings = load_user_mappings()

    if not user_mappings:
        logger.error("No users configured in user_mapping.json")
        logger.error("Please create/populate user_mapping.json with Slack user mappings")
        return False

    logger.info(f"Loaded {len(user_mappings)} user mappings")

    # Validate connectors
    if not connector:
        logger.error("Scalekit connector not initialized - check your configuration")
        return False

    if not router:
        logger.error("Message router not initialized")
        return False

    try:
        # Start Flask server in background thread for health checks and auth
        import threading
        import socket

        flask_ready = threading.Event()

        def run_flask():
            try:
                # Verify port is available before binding
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex((Settings.FLASK_HOST, Settings.FLASK_PORT))
                sock.close()
                if result == 0:
                    raise RuntimeError(f"Port {Settings.FLASK_PORT} already in use")

                flask_ready.set()
                app.run(
                    host=Settings.FLASK_HOST,
                    port=Settings.FLASK_PORT,
                    debug=False,
                    use_reloader=False,
                    threaded=True
                )
            except Exception as e:
                logger.error(f"Flask startup failed: {e}", exc_info=True)
                raise

        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()

        # Wait for Flask to be ready with timeout
        if flask_ready.wait(timeout=5.0):
            logger.info(f"Flask server running on http://{Settings.FLASK_HOST}:{Settings.FLASK_PORT}")
        else:
            logger.error("Flask server startup timeout")
            raise RuntimeError("Flask server failed to start within timeout")

    except Exception as e:
        logger.error(f"Failed to start Flask server: {e}", exc_info=True)
        return False

    # Start polling
    try:
        poll_channels(user_mappings)
        return True
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return False
    except Exception as e:
        logger.error(f"Fatal error in polling loop: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    # Register signal handlers only in main thread
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    exit_code = 0
    try:
        success = run_polling_mode()
        exit_code = 0 if success else 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        exit_code = 130
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        exit_code = 1
    finally:
        logger.info("Agent shutdown complete")

    import sys
    sys.exit(exit_code)
