"""One guarded transport shared by native SDKs, Answer and Judge."""
from dataclasses import dataclass
import json
import os

from .artifacts import write_json
from .budget import CallBudget, make_transport
from .profile import ALLOWED_MODELS, MODEL_OUTPUT_TOKEN_LIMITS, public_identity, read_profile, validate_profile
from .embedding import encode, settings


@dataclass(frozen=True)
class Reply:
    text: str
    model: str


class FixtureClient:
    def complete(self, prompt, *, stage, **kwargs):
        return Reply('{"label":"WRONG"}' if stage == "judge" else "offline fixture answer",
                     "offline-fixture-NOT-a-baseline")

    def close(self):
        pass


class Gateway:
    def __init__(self, runtime, output, *, expected_identity=None):
        import httpx
        import openai
        self.runtime, self.output = runtime, output
        self.profile = validate_profile(read_profile(runtime["env_file"]), runtime["model"])
        self.identity = public_identity(self.profile, runtime)
        self.embedding_dimensions, self.embedding_batch = settings(self.profile)
        if expected_identity is not None and self.identity != expected_identity:
            raise ValueError("Configured provider identity changed since freeze")
        profile = dict(self.profile)
        rates = runtime["rates_cny_per_million"]
        profile.update(AMI_TEST_INPUT_CNY_PER_MILLION=str(rates["input"]),
                       AMI_TEST_OUTPUT_CNY_PER_MILLION=str(rates["output"]),
                       AMI_TEST_EMBED_CNY_PER_MILLION=str(rates["embedding"]))
        self.budget = CallBudget(profile)
        self.budget.on_change = self.save
        llm_url = self.profile["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions"
        embedding_url = self.profile["AMI_EMBED_BASE_URL"].rstrip("/") + "/embeddings"
        transports = {"llm": make_transport(self.budget, "llm", llm_url, runtime["model"],
                          disable_thinking_required=False,
                          allowed_response_models=ALLOWED_MODELS[runtime["model"]],
                          timeout=runtime["request_timeout_seconds"]),
                      "embedding": make_transport(self.budget, "embedding", embedding_url,
                          self.profile.get("AMI_EMBED_MODEL", "text-embedding-v4"),
                          embedding_dimensions=self.embedding_dimensions, embedding_batch=self.embedding_batch,
                          timeout=runtime["request_timeout_seconds"])}
        self.transports = transports
        class Transport(httpx.BaseTransport):
            def handle_request(self, request):
                kind = "llm" if str(request.url) == llm_url else "embedding"
                if kind == "llm":
                    payload = json.loads(request.content)
                    for key in ("enable_thinking", "chat_template_kwargs", "top_k"):
                        payload.pop(key, None)
                    payload.setdefault("max_tokens", runtime["memory_default_max_tokens"])
                    payload["seed"] = runtime["seed"]
                    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
                    request = httpx.Request(request.method, request.url, headers=headers,
                        content=json.dumps(payload), extensions=request.extensions)
                return transports[kind].handle_request(request)
            def close(self):
                # SDK clients share the gateway's provider transports; stage owner closes them.
                pass
        original = openai.OpenAI
        class GuardedOpenAI(original):
            def __init__(self, *args, **kwargs):
                kwargs.update(max_retries=runtime.get("max_retries", 0), timeout=runtime["request_timeout_seconds"],
                    http_client=httpx.Client(transport=Transport(), follow_redirects=False))
                super().__init__(*args, **kwargs)
        self.original_class, self.client_class = original, GuardedOpenAI
        self.openai_module = openai
        self.client = None
        self.embedding_client = None
        self.judge_response_count = 0
        self.save()

    def install_native(self):
        self.openai_module.OpenAI = self.client_class
        os.environ.update(OPENAI_API_KEY=self.profile["OPENAI_API_KEY"],
            OPENAI_BASE_URL=self.profile["OPENAI_BASE_URL"], OPENAI_API_BASE=self.profile["OPENAI_BASE_URL"],
            MEMMA_API_TIMEOUT=str(self.runtime["request_timeout_seconds"]), MEMMA_API_RETRIES="0",
            MEMMA_INFERENCE_SEED=str(self.runtime["seed"]), MEMMA_LIGHTMEM_WORKERS="1",
            HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", MEM0_TELEMETRY="false",
            TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="2", PYTHONDONTWRITEBYTECODE="1")
        os.environ.pop("OPENROUTER_API_KEY", None)

    def complete(self, prompt, *, stage, max_tokens, temperature):
        if max_tokens is None:
            if stage != "answer":
                raise ValueError("Only Answer supports an unset output cap")
            # Ask for the model's full supported output, rather than relying on
            # provider defaults or the native-memory transport's 2048 fallback.
            max_tokens = MODEL_OUTPUT_TOKEN_LIMITS[self.runtime["model"]]
        self.budget.phase = stage
        if self.client is None:
            self.client = self.client_class(api_key=self.profile["OPENAI_API_KEY"],
                                            base_url=self.profile["OPENAI_BASE_URL"])
        response = self.client.chat.completions.create(model=self.runtime["model"],
            messages=[{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt,
            max_tokens=max_tokens, temperature=temperature)
        if stage == "judge":
            # Persist content, usage and finish reason before parsing or raising
            # for truncation. Each call has its own artifact (e.g. BEAM checks).
            self.judge_response_count += 1
            write_json(self.output.with_name(
                f"{self.output.stem}.judge-{self.judge_response_count:03d}.response.json"),
                response.model_dump(mode="json"))
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise ValueError("Model output was truncated")
        return Reply(choice.message.content.strip(), response.model)

    def save(self):
        write_json(self.output, {"calls": self.budget.rows, "failure": self.budget.failed,
            "estimated_cny": sum(r.get("estimated_cny", 0) for r in self.budget.rows),
            "reserved_cny": self.budget.reserved, "request_and_cost_limits": "none",
            "provider_identity": self.identity})

    def embed(self, texts):
        self.budget.check()
        def request(payload):
            if self.embedding_client is None:
                self.embedding_client = self.client_class(api_key=self.profile["AMI_EMBED_API_KEY"],
                    base_url=self.profile["AMI_EMBED_BASE_URL"])
            response = self.embedding_client.embeddings.create(model="text-embedding-v4", input=list(payload),
                dimensions=self.embedding_dimensions, encoding_format="float")
            return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
        try:
            return encode(texts, request, self.embedding_dimensions, self.embedding_batch)
        except Exception:
            # Native methods sometimes catch provider errors; keep the run failed.
            if self.budget.failed is None:
                self.budget.failed = "Embedding failure; no further requests allowed"
            self.save()
            raise

    def close(self):
        if self.client:
            self.client.close()
        if self.embedding_client:
            self.embedding_client.close()
        self.openai_module.OpenAI = self.original_class
        for transport in self.transports.values():
            transport.close()
        self.save()
