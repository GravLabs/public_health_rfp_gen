"""Shared base for all APHL specialist agents."""
from __future__ import annotations
from dataclasses import dataclass, field
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
import os


@dataclass
class AgentConfig:
    name: str
    instructions: str
    model: str = "gpt-4o-finetuned"
    tools: list[dict] = field(default_factory=list)


class BaseAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._client = AIProjectClient(
            endpoint=os.environ["AZURE_AI_FOUNDRY_PROJECT_ENDPOINT"],
            credential=DefaultAzureCredential(),
        )
        self._agent = self._client.agents.create_agent(
            model=config.model,
            name=config.name,
            instructions=config.instructions,
            tools=config.tools,
        )

    def run(self, user_message: str) -> str:
        thread = self._client.agents.create_thread()
        self._client.agents.create_message(
            thread_id=thread.id,
            role="user",
            content=user_message,
        )
        run = self._client.agents.create_and_process_run(
            thread_id=thread.id,
            agent_id=self._agent.id,
        )
        messages = self._client.agents.list_messages(thread_id=thread.id)
        return next(
            m.content[0].text.value
            for m in messages.data
            if m.role == "assistant"
        )

    def delete(self) -> None:
        self._client.agents.delete_agent(self._agent.id)
