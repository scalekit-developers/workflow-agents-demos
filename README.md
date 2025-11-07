# 🔧 Scalekit Agent Action Examples

This repository contains a collection of **Scalekit agent action** examples, demonstrating how to implement workflow automation and integrations across different applications. Scalekit provides production-ready connectors so you can integrate services like **Slack, GitHub, Gmail, Google Calendar, Notion, and more** with minimal effort.

Explore how to integrate **agent actions and workflows** into your applications using Scalekit.

## Overview

The examples in this repository showcase different automation and agent use cases powered by **Scalekit connectors and agent actions**. Each project demonstrates real-world patterns for building reliable actions, separating core logic from integration glue, and running locally or under your preferred orchestration framework.

## Example Use Cases

These examples highlight common workflows using **Scalekit agent actions**:

- **Slack Triage to GitHub Issues**: Create issues from Slack messages and confirm back in channel.
- **Email to Calendar Scheduling**: Parse invitation emails, find free slots, and create Google Calendar events.
- **Release Notes Automation**: Detect merged PRs on GitHub, create Notion pages, and send Slack notifications.
- **Create Task via Frameworks**: Expose a single task creation action through LangChain tools, CrewAI runners, or an MCP server.

## Getting Started

To explore and run an example project:

1. **Clone the repository**:

   ```bash
   git clone git@github.com:scalekit-developers/workflow-agents-demos.git
   cd workflow-agents-demos
   ```

2. Navigate to an example folder:

   ```bash
   cd example-project-folder
   ```

3. Follow the specific **README** inside each project folder for setup instructions.

## Principles

- Keep actions small, reliable, and composable
- Separate core logic from connectors and framework glue
- Use environment variables for configuration; do not commit secrets

## Documentation & Support

- 📖 Read the official Scalekit documentation: [Scalekit Docs](https://docs.scalekit.com/)
- ❓ Need help? Contact the Scalekit support team or open an issue in this repository.
