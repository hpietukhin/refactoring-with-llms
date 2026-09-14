i want to have deepagent
see example
https://docs.langchain.com/oss/python/deepagents/rubric#example-generate-vetted-python-code
but for rubric i want you to use existing langgraph tools that validating code
and create in this folder separate file where you will invoke this agent
like this
from langchain.messages import HumanMessage

result = agent.invoke(
    {
        "messages": [
            HumanMessage(
                content=(
                    "Refactor java code smell."
                )
            )
        ],
        "rubric": (
            "- All tests pass in run_test_suite\n" - there will be name of our tools
            "targeted code smell is fixed" - here agent will get 
        ),
    },
    config={"configurable": {"case_id": "PhiCode Philib:3b7222e9b466"}}, - here will be case_id from experiments.main
)
print(result["messages"][-1].text)
in rubric middleware use this tools:
run_ck_metrics - use for RubricMiddleware as on_evaluation param
run_java_test_analysis — низкоуровневая функция: Maven detect → mvn test → при fail опциональный LLM
 repair. Возвращает dict с summary/error.

 java_verification — нода workflow вокруг неё: вызывает run_java_test_analysis, пишет tests_failed в
 state, и если тесты прошли — ещё собирает CK metrics.
 do not use internet_search tool, do not add it

 system prompt must be something like this:
 let prompt = `You are an expert coding assistant operating inside deepagents, a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files. Your only objective is refactoring Java code to fix code smells. You will be provided with a rubric that defines the success criteria for the refactoring task. You must ensure that all tests pass and that the targeted code smell is fixed. You have access to the following tools to assist you in this task:
 
 ${toolsList}
 
 
 Guidelines:
 ${guidelines}

and here will be success criteria for refactoring:
