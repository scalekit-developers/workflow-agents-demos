from crewai.tools import structured_tool
from tools.web_search import web_search_tool
from tools.fetch_url import fetch_url_tool
from tools.summarize_research import summarize_tool

crew_web_search_tool = structured_tool.from_function(web_search_tool)
crew_fetch_url_tool = structured_tool.from_function(fetch_url_tool)
crew_summarize_tool = structured_tool.from_function(summarize_tool)
