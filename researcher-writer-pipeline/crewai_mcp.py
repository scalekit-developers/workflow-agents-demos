"""
CREWAI & MCP INTEGRATION TEMPLATE
=================================
This file demonstrates a modular Researcher→Writer pipeline using CrewAI orchestration.

How to use:
- CrewAI is used for agent orchestration and task management.
- MCP integration can be added later if needed.

To add MCP:
- Install the MCP client library (see https://modelcontextprotocol.org/)
- Import MCPClient and initialize it with your model/context settings
- Use MCPClient to manage context, agent communication, or replace Crew.run() with MCP-driven orchestration
- Example:
    from mcp import MCPClient
    mcp_client = MCPClient(...)
    mcp_client.run({"query": args.query})

See:
- CrewAI docs: https://docs.crewai.com/
- MCP docs: https://modelcontextprotocol.org/
"""

from crewai import Crew, Agent, Task, LLM
import argparse
from dotenv import load_dotenv
import os

# Define Researcher Agent (no tools)
researcher = Agent(
    role="Researcher",
    goal="Find and synthesize the most relevant information for a given query.",
    backstory="An expert at web research and summarization.",
    verbose=True,
)

# Define Writer Agent (no tools)
writer = Agent(
    role="Writer",
    goal="Transform research notes into a concise, publishable Markdown brief.",
    backstory="A skilled technical writer who creates clear, engaging summaries.",
    verbose=True,
)

# Define Research Task
research_task = Task(
    description="Research the query and synthesize findings with citations.",
    agent=researcher,
    expected_output="A summary with 3-5 key findings and cited URLs."
)

# Define Writing Task
writing_task = Task(
    description="Rewrite research notes into a Markdown brief with heading, bullet points, and conclusion.",
    agent=writer,
    expected_output="A ready-to-publish Markdown brief."
)

def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="CrewAI Researcher→Writer pipeline")
    parser.add_argument("--query", default="What are the latest advancements in AI?", help="User query to research and summarize")
    args = parser.parse_args()

    # Set up CrewAI LLM using environment variable
    openai_api_key = os.getenv("OPENAI_API_KEY")
    llm = LLM(model="gpt-4o-mini", api_key=openai_api_key)
    researcher.llm = llm
    writer.llm = llm

    crew = Crew(
        agents=[researcher, writer],
        tasks=[research_task, writing_task],
        verbose=True,
    )

    result = crew.kickoff({"query": args.query})
    print("\n✅ Final Writer Output (CrewAI):\n")
    print(result)

if __name__ == "__main__":
    main()
