
## Zendesk-Slack-Notion Agent

A one-stop, orchestrated support workflow agent using Google ADK, Gemini LLM, Zendesk, Slack, and Notion. This agent fetches new Zendesk tickets, generates suggested replies, posts a daily digest to Slack, and updates a Notion knowledge base for solved tickets—all orchestrated by an LLM agent.

---

## Features
- **End-to-end orchestration**: LLM agent calls all tools in order—no manual loops.
- **Zendesk integration**: Fetches new tickets via API.
- **LLM-powered replies**: Generates concise, friendly support replies using Gemini.
- **Slack digest**: Posts a daily summary of new tickets and suggested replies.
- **Notion KB update**: Adds solved tickets to a Notion database.
- **Stateful**: Remembers which tickets have been posted/updated.

---

## Quickstart (One-Stop Setup)

1. **Clone the repo**
    ```sh
    git clone <your-repo-url>
    cd zendesk-slack-notion-agent
    ```

2. **Install dependencies**
    ```sh
    pip install -r requirements.txt
    ```

3. **Set up environment variables**
    - Copy `.env.example` to `.env` and fill in your credentials:
      - Google API key (for Gemini/ADK)
      - Zendesk API token, email, subdomain
      - Slack bot token, channel ID
      - Notion API key, database ID
    - Example:
      ```env
      GOOGLE_API_KEY=...
      ZENDESK_API_TOKEN=...
      ZENDESK_EMAIL=...
      ZENDESK_SUBDOMAIN=...
      SLACK_BOT_TOKEN=...
      SLACK_CHANNEL_ID=...
      NOTION_API_KEY=...
      NOTION_KB_DATABASE_ID=...
      LOG_LEVEL=INFO
      ```

4. **Run the agent**
    ```sh
    python agent.py
    ```
    The agent will run in a loop, orchestrating the full workflow every 60 seconds.

---

## Environment Variables
| Variable                | Description                        |
|-------------------------|------------------------------------|
| GOOGLE_API_KEY          | Google Gemini/ADK API key          |
| ZENDESK_API_TOKEN       | Zendesk API token                  |
| ZENDESK_EMAIL           | Zendesk account email              |
| ZENDESK_SUBDOMAIN       | Zendesk subdomain                  |
| SLACK_BOT_TOKEN         | Slack bot token                    |
| SLACK_CHANNEL_ID        | Slack channel ID                   |
| NOTION_API_KEY          | Notion integration token           |
| NOTION_KB_DATABASE_ID   | Notion database ID for KB          |
| LOG_LEVEL               | Logging level (INFO/DEBUG/etc)     |

---

## How it Works
1. **Orchestrator LLM agent** runs and calls these tools in order:
    - `fetch_new_tickets`: Gets new Zendesk tickets.
    - `annotate_tickets_with_replies`: Adds LLM-generated replies to each ticket.
    - `post_slack_digest`: Posts a summary to Slack.
    - `update_notion_kb`: Adds solved tickets to Notion.
2. **State** is tracked in `state/ticket_thread_map.json` to avoid duplicate posts.
3. **All logic is in `agent.py`**—no manual steps required after setup.
