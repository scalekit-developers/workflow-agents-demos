import argparse
import asyncio
import os
from typing import List

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage

from tools.web_search import web_search_tool
from tools.fetch_url import fetch_url_tool
from tools.summarize_research import summarize_tool


# Load environment variables
load_dotenv()


async def run_with_tools(llm: ChatOpenAI, tools, system_prompt: str, user_text: str, max_iters: int = 4) -> str:
    """Minimal tool-calling loop using ChatOpenAI.bind_tools and ToolMessage.

    - llm: ChatOpenAI instance
    - tools: list of @tool-decorated tools
    - system_prompt: role/system instruction
    - user_text: initial user input
    - max_iters: safety cap on tool-use rounds
    """
    llm_with_tools = llm.bind_tools(tools)
    messages: List = [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]

    for _ in range(max_iters):
        ai: AIMessage = await llm_with_tools.ainvoke(messages)

        # If the model calls tools, execute them and continue the loop
        tool_calls = getattr(ai, "tool_calls", None)
        if tool_calls:
            # The tools must respond to this exact AI message, so persist it
            messages.append(ai)
            for call in tool_calls:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                args = call.get("args", {}) if isinstance(call, dict) else getattr(call, "args", {})
                call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)

                tool_obj = next((t for t in tools if getattr(t, "name", None) == name), None)
                if tool_obj is None:
                    messages.append(ToolMessage(content=f"Tool '{name}' not found.", tool_call_id=call_id))
                    continue

                try:
                    # Tools are LangChain Runnables; .invoke is synchronous and fine here
                    result = tool_obj.invoke(args)
                except Exception as e:
                    result = f"Error running tool '{name}': {e}"

                messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
            # Continue letting the model read tool outputs
            continue

        # No tool calls -> finalize
        return ai.content if ai.content else ""

    # Max iterations reached
    return "Reached maximum tool-use steps without a final answer."


async def run_langchain_mcp(query: str):
    # Initialize the OpenAI LLM (reads OPENAI_API_KEY from env)
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    # Step 1: Web search
    print(f"🔍 [Researcher] Searching: {query}")
    web_results = web_search_tool.invoke(query)
    print("\nTop search results:\n", web_results)

    # Step 2: Extract URLs and fetch main content
    urls = []
    for line in web_results.splitlines():
        if line.strip().startswith("http"):
            urls.append(line.strip())
    fetched_contents = []
    for url in urls[:3]:  # Limit to top 3 URLs for brevity
        print(f"\n[Researcher] Fetching: {url}")
        content = fetch_url_tool.invoke(url)
        fetched_contents.append(f"URL: {url}\n{content}")

    # Step 3: Synthesize research output with LLM
    research_input = (
        f"Research query: {query}\n\n" +
        "\n\n".join(fetched_contents)
    )
    synth_prompt = (
        "You are a research agent. Given the query and fetched web content below, synthesize a research summary with 3-5 key findings and cite URLs inline."
    )
    print("\n[Researcher] Synthesizing research output with LLM...")
    notes = await run_with_tools(llm, [], synth_prompt, research_input)
    print("\n[Researcher] Final synthesized notes:\n", notes)

    # Step 4: Writer step — concise Markdown brief
    writer_prompt = (
        "You are a technical writer. Rewrite the following research notes into a ready-to-publish Markdown brief."
        " Add a heading, bullet points for key findings, and a short conclusion."
    )
    print("\n✍️ [Writer] Generating Markdown brief …")
    summary = await run_with_tools(llm, [], writer_prompt, notes)
    print("\nFinal Writer Output:\n")
    print(summary)
    return {"research_notes": notes, "summary": summary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LangChain Researcher→Writer pipeline")
    parser.add_argument("--query", default="What are the latest advancements in AI?", help="User query to research and summarize")
    args = parser.parse_args()

    try:
        result = asyncio.run(asyncio.wait_for(
            run_langchain_mcp(args.query),
            timeout=90,
        ))
    except asyncio.TimeoutError:
        print("❌ Timeout reached, cancelling tasks.")
