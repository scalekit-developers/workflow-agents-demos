from crewai.tools.base_tool import Tool
from tools.web_search import web_search_tool
from tools.fetch_url import fetch_url_tool

# Convert LangChain StructuredTool to CrewAI Tool
crew_web_search_tool = Tool.from_langchain(web_search_tool)
crew_fetch_url_tool = Tool.from_langchain(fetch_url_tool)

