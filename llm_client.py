"""
Thin wrapper around the OpenAI-compatible client pointed at the local
Ollama daemon. Isolated from the rest of the pipeline so the HTTP layer
(app.py/routes), the orchestration layer (query_service.py), and the
actual model-calling layer can be reasoned about -- and mocked in tests
-- independently of each other.
"""

import os
from openai import OpenAI

# Configurable via OLLAMA_BASE_URL: "localhost" only resolves to the host
# machine when this process itself runs on the host -- inside a Docker
# container, "localhost" is the container, not the host, so the compose
# setup overrides this to http://host.docker.internal:11434/v1 instead.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
client = OpenAI(
    base_url=OLLAMA_BASE_URL,
    api_key="local-bypass"  # The library requires a string here, but Ollama ignores it
)

# qwen2.5-coder:14b, not a general chat model, because this task is
# text-to-SQL specifically. A smaller general-purpose model (llama3.2:3b
# was tried here previously) can hallucinate a column and then -- despite
# the self-healing retry loop's explicit "this column does not exist"
# correction -- regenerate the exact same invalid column reference on
# every retry, because it's not strong enough at code/SQL reasoning to
# use the correction. Override via LLM_MODEL if you've pulled a
# different model and verified it against this schema.
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder:14b")


def call_llm_api(system_prompt, user_query, temperature=0.0):
    """
    Executes a live inference call to the local Ollama engine.

    temperature=0.0 is used for the first attempt (deterministic, most
    reliable when it's right). On retries, a small amount of temperature
    is used instead -- at temperature=0.0 the model is fully
    deterministic, so if its first guess contained a reasoning mistake
    (e.g. attributing a column to the wrong table), every "self-healing"
    retry was observed to regenerate the exact same wrong SQL verbatim,
    guaranteeing failure even though the error message correctly
    described the problem. A little randomness lets each retry actually
    have a chance to reconsider instead of robotically repeating itself.
    """
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query}
        ],
        temperature=temperature
    )
    return response.choices[0].message.content
