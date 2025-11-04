import argparse
import asyncio
from typing import List, Optional

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage

from tools.web_search import web_search_tool
from tools.fetch_url import fetch_url_tool
from tools.summarize_research import summarize_tool


async def run_with_tools(llm: ChatOpenAI, tools, system_prompt: str, user_text: str, max_iters: int = 4) -> str:
    llm_with_tools = llm.bind_tools(tools)
    messages: List = [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
    for _ in range(max_iters):
        ai: AIMessage = await llm_with_tools.ainvoke(messages)
        tool_calls = getattr(ai, "tool_calls", None)
        if tool_calls:
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
                    result = tool_obj.invoke(args)
                except Exception as e:
                    result = f"Error running tool '{name}': {e}"
                messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
            continue
        return ai.content if ai.content else ""
    return "Reached maximum tool-use steps without a final answer."


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="MCP-style Researcher→Writer pipeline (Crew variant)")
    parser.add_argument("--query", default="What are the latest advancements in AI?", help="User query to research and summarize")
    parser.add_argument("--researcher-system", default=None, help="Override researcher system prompt")
    parser.add_argument("--writer-system", default=None, help="Override writer system prompt")
    args = parser.parse_args()

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    async def run_all():
        # Step 1: Web search
        print(f"🔍 [Researcher] Searching: {args.query}")
        web_results = web_search_tool.invoke(args.query)
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
            f"Research query: {args.query}\n\n" +
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
        return summary

    try:
        result = asyncio.run(asyncio.wait_for(run_all(),90))
        print("\n✅ Final Writer Output (Crew variant):\n")
        print(result)
    except asyncio.TimeoutError:
        print("❌ Timeout reached, cancelling tasks.")


if __name__ == "__main__":
    main()
