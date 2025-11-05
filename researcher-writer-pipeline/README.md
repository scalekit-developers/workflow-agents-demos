# Researcher-Writer Pipeline (LangChain & CrewAI)

## Overview
This project demonstrates an apples-to-apples comparison of LangChain and CrewAI for a two-agent pipeline: **Researcher → Writer**. Both agents use shared tools to search the web, fetch main content from URLs, and summarize research into a ready-to-publish Markdown brief.

- **Researcher**: Uses web search and fetch tools to gather and synthesize information.
- **Writer**: Summarizes research notes into a concise, well-structured Markdown brief.

## Features
- Shared tool implementations for both LangChain and CrewAI
- Explicit agent handoff and per-agent tool permissions
- Timeout/cancellation support
- Custom CLI prompts for query and agent instructions

## Quickstart

### 1. Install dependencies
```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables
Copy `.env.example` to `.env` and fill in your API keys:
```sh
cp .env.example .env
```
Edit `.env`:
```
OPENAI_API_KEY=your-openai-api-key
SERPAPI_API_KEY=your-serpapi-key
```

### 3. Run LangChain pipeline
```sh
python langchain_mcp.py --query "Your research topic here" --timeout 90
```

### 4. Run CrewAI pipeline
```sh
python crewai_mcp.py --query "Your research topic here" --timeout 90
```

## Tools
- `web_search_tool`: Searches Google via SerpAPI and returns top results.
- `fetch_url_tool`: Fetches and extracts main readable content from a URL.
- `summarize_tool`: Summarizes research notes into a concise Markdown brief.

## File Structure
```
researcher-writer-pipeline/
├── langchain_mcp.py         # LangChain pipeline
├── crewai_mcp.py           # CrewAI pipeline
├── tools/
│   ├── web_search.py       # Web search tool
│   ├── fetch_url.py        # Fetch URL tool
│   ├── summarize_research.py # Summarizer tool
│   └── crewai_tools.py     # CrewAI tool wrappers
├── requirements.txt        # Python dependencies
├── .env.example           # Environment variable template
├── README.md               # This file
```

## Requirements
See `requirements.txt` for all dependencies. Key packages:
- langchain
- langchain-core
- langchain-openai
- crewai
- crewai-tools
- openai
- requests
- python-dotenv
- readability-lxml
- beautifulsoup4

## Environment Variables
See `.env.example` for required keys:
- `OPENAI_API_KEY`: Your OpenAI API key
- `SERPAPI_API_KEY`: Your SerpAPI key for web search

## Customization
- Change the query/topic via CLI: `--query "Your topic"`
- Override agent prompts: `--researcher-system "..." --writer-system "..."`
- Adjust timeouts: `--timeout 90`

## Troubleshooting
- If web search returns a configuration error, check your `SERPAPI_API_KEY` in `.env`.
- If LLM calls fail, check your `OPENAI_API_KEY` in `.env`.
- For tool errors, ensure all dependencies are installed and environment variables are loaded.

## MCP Integration

You can easily extend this pipeline to use Model Context Protocol (MCP) for advanced context management and agent orchestration.

### How to Integrate MCP
1. Install the MCP client library (see https://modelcontextprotocol.org/)
2. Import MCPClient and initialize it with your model/context settings
3. Use MCPClient to manage context, agent communication, or replace the LLM orchestration

#### Example:
```python
from mcp import MCPClient
mcp_client = MCPClient(api_key=os.getenv("MCP_API_KEY"), model="gpt-4o-mini")
result = mcp_client.run({"query": "Your research topic here"})
```
You can replace the LLM calls or tool orchestration with MCPClient for advanced context management.

Add `MCP_API_KEY` to your `.env` file if required.

## License
MIT
