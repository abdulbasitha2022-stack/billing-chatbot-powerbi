"""
llm.py - the pluggable LLM layer.

Business logic (pipeline.py) only ever sees `LLMProvider.complete()`. Which vendor sits behind
it is decided by `llm.provider` in config.yaml, so a client can pick their own AI agent without
any code change. To add a vendor: subclass LLMProvider, implement complete(), register it in
PROVIDERS. Nothing else needs to know.

The interface is deliberately tiny: (system prompt, message list) -> text + token usage.
Prompt *quality* is NOT portable across vendors, so expect to re-tune prompt.py per provider.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# What it does: sends a prompt to an AI model over the internet and returns its text reply.
# Who calls it: build_llm() is called by chatbot_api.py (create_app) and run.py. The returned
#   provider's complete() is called by pipeline.py (for planning the conversation and for
#   writing SQL). Config: the `llm:` section of config_billing.yaml (billing uses "groq").
# What it calls: the vendor SDKs (Software Development Kits = official Python client libraries):
#   * anthropic  - Anthropic's SDK for Claude models. anthropic.Anthropic() reads the API key
#                  from the ANTHROPIC_API_KEY environment variable.
#   * openai     - OpenAI's SDK. Groq offers an "OpenAI-compatible" API, so the same SDK is
#                  reused for Groq just by pointing base_url at Groq's server.
#   The SDKs handle HTTPS, JSON, authentication headers and automatic retries for us.
# API keys always come from environment variables and are never logged.
# ----------------------------------------------------------------------------------------------
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger(__name__)   # "app.llm" -> logs/backend_billing.log


# What every provider returns: the reply text plus token counts (used for cost/usage logging
# in pipeline.py). field(default_factory=dict) gives each response its own new empty dict.
@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict = field(default_factory=dict)      # e.g. cache read/write tokens


# Abstract base class (the "interface"). Each vendor class below inherits from it and must
# implement complete(system, messages) -> LLMResponse.
class LLMProvider(ABC):
    name: str = "base"

    # cfg = the vendor's sub-section of the config, e.g. llm.groq (model, max_tokens, ...).
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model = cfg["model"]

    @abstractmethod
    def complete(self, system: str, messages: list) -> LLMResponse:
        """messages: [{"role": "user" | "assistant", "content": str}, ...]"""


# Anthropic Claude (selected with llm.provider: claude). Not the billing default.
class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, cfg):
        super().__init__(cfg)
        import anthropic                                   # lazy: only needed if selected
        # Reads ANTHROPIC_API_KEY from the environment. Never hard-code the key.
        self.client = anthropic.Anthropic()

    # system = the instructions/schema prompt; messages = the conversation so far.
    # Builds the request arguments in `kwargs` and calls the Messages API.
    def complete(self, system, messages):
        kwargs = dict(model=self.model, max_tokens=self.cfg.get("max_tokens", 1500),
                      messages=messages)
        # The schema + data dictionary is a big, identical prefix on every request. Marking it
        # cacheable makes repeat calls cheaper/faster (only kicks in above the model's minimum
        # prompt size; below that it is silently ignored).
        if self.cfg.get("prompt_caching", True):
            kwargs["system"] = [{"type": "text", "text": system,
                                 "cache_control": {"type": "ephemeral"}}]
        else:
            kwargs["system"] = system
        # Optional: some models reject sampling parameters. Only sent if you set it in config.
        if self.cfg.get("temperature") is not None:
            kwargs["temperature"] = self.cfg["temperature"]

        # Network call. Errors (bad key, rate limit, outage) are raised by the SDK and are not
        # caught here - the caller (pipeline.py) decides how to handle them.
        resp = self.client.messages.create(**kwargs)
        # A Claude reply is a list of content blocks; join the text blocks into one string.
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        return LLMResponse(
            text=text, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            extra={"cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                   "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0})


class OpenAIProvider(LLMProvider):
    """Second vendor, to prove the layer is swappable. NOT yet tested against the live API."""
    name = "openai"

    def __init__(self, cfg):
        super().__init__(cfg)
        from openai import OpenAI                          # lazy: pip install openai
        self.client = OpenAI()                             # reads OPENAI_API_KEY

    def complete(self, system, messages):
        # No max_tokens / temperature passed on purpose: parameter names and support differ
        # between OpenAI model families, and the defaults are fine for a POC.
        resp = self.client.chat.completions.create(
            model=self.model, messages=[{"role": "system", "content": system}] + messages)
        return LLMResponse(text=resp.choices[0].message.content or "",
                           input_tokens=resp.usage.prompt_tokens,
                           output_tokens=resp.usage.completion_tokens)


# THE PROVIDER USED BY config_billing.yaml (llm.provider: groq). Groq hosts open models such as
# openai/gpt-oss-120b and speaks the same HTTP API as OpenAI, so the openai SDK is reused.
class GroqProvider(LLMProvider):
    """Groq provider using the OpenAI-compatible API."""

    name = "groq"

    def __init__(self, cfg):
        super().__init__(cfg)

        from openai import OpenAI   # imported here (lazily) so it is only needed when Groq is used

        # Fail fast at startup with a clear message if the key is missing, rather than failing
        # later on the first chat message.
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            log.error("GROQ_API_KEY is not set; the Groq provider cannot start")
            raise RuntimeError("GROQ_API_KEY environment variable is not set.")

        # max_retries: the SDK backs off and honours Retry-After on HTTP 429 (per-minute token
        # limits on Groq's free/on-demand tiers are easy to hit with two calls per request).
        self.client = OpenAI(
            api_key=api_key,
            base_url=cfg.get("base_url", "https://api.groq.com/openai/v1"),
            max_retries=int(cfg.get("max_retries", 4)),
        )

    # Same contract as the other providers. The system prompt is sent as the first message
    # with role "system" (OpenAI-style), followed by the conversation (*messages unpacks the list).
    def complete(self, system, messages):
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            max_completion_tokens=int(self.cfg.get("max_tokens", 1500)),
        )
        # gpt-oss models spend completion tokens on hidden reasoning; "low" keeps enough budget
        # for the actual answer. Set llm.groq.reasoning_effort in the config (omit for other models).
        if self.cfg.get("reasoning_effort"):
            kwargs["extra_body"] = {"reasoning_effort": self.cfg["reasoning_effort"]}

        response = self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]          # we only ask for one answer
        text = choice.message.content or ""
        # finish_reason "length" + empty text = the token budget ran out before any answer was
        # written. Raise a clear error instead of returning "" (which would look like a bad reply).
        if not text.strip() and choice.finish_reason == "length":
            log.warning("Groq reply was empty: the model used all %s output tokens on reasoning",
                        kwargs["max_completion_tokens"])
            raise RuntimeError("The model ran out of output tokens before answering "
                               "(raise llm.groq.max_tokens or lower reasoning_effort).")
        usage = response.usage
        return LLMResponse(
            text=text,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


# Registry: config value of llm.provider -> class to create.
PROVIDERS = {"claude": ClaudeProvider, "openai": OpenAIProvider, "groq": GroqProvider}



# Factory. llm_cfg = the whole `llm:` config section. Picks the class named by llm.provider and
# passes it that vendor's own sub-section (e.g. llm.groq). Called by chatbot_api.py and run.py.
def build_llm(llm_cfg: dict) -> LLMProvider:
    name = llm_cfg["provider"]
    if name not in PROVIDERS:
        raise ValueError(f"Unknown llm.provider '{name}'. Available: {', '.join(PROVIDERS)}")
    provider = PROVIDERS[name](llm_cfg[name])
    # Provider and model only - the API key is never logged.
    log.info("LLM provider ready: %s, model %s", provider.name, provider.model)
    return provider
