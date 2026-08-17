"""Runtime configuration. Secrets come from the environment, never from code."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

# Load .env without clobbering anything already exported in the shell.
load_dotenv(ROOT / ".env", override=False)


def _clean(name: str) -> str | None:
    """Treat blank env vars as absent — a blank key is not a key."""
    v = os.environ.get(name)
    return v.strip() or None if v else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # --- Composio (search/fetch layer) ---
    composio_api_key: str | None = None
    composio_user_id: str = "toolkit-recon"

    # --- LLM (structured extraction), OpenAI-compatible endpoint ---
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"

    # --- Pipeline knobs ---
    concurrency: int = 8
    domain_delay: float = 1.5
    max_docs_per_app: int = 3
    # A page that extracts to a couple of sentences (nav shell, JS-rendered
    # body, cookie wall) is not evidence. Counting it would let an empty page
    # satisfy "official docs reached" and inflate the confidence score.
    min_doc_chars: int = 250
    # Higher bar for Layer 3: a live docs page that renders correctly yields
    # thousands of characters, so a few hundred means we got a stub or a wall.
    min_browser_chars: int = 2_500
    max_doc_chars: int = 14_000  # what we archive to data/raw (full evidence)
    request_timeout: float = 120.0
    max_retries: int = 4

    # --- LLM budget ---
    # The extraction endpoint's TPM quota is the true bottleneck, so the LLM is
    # governed separately from fetch concurrency. Defaults are sized for the
    # 8,000 TPM tier this was developed against; raise them on a bigger tier.
    llm_tokens_per_minute: int = 8_000
    llm_concurrency: int = 2
    llm_retries: int = 6
    # Measured: a full extraction lands near 900 completion tokens, so a 900
    # cap truncates roughly half the time. Headroom here is far cheaper than
    # a retry, which pays for the whole prompt again.
    llm_max_completion_tokens: int = 2_000
    llm_expected_completion_tokens: int = 1_050  # what we reserve per call
    # Sized so that TWO extractions fit in one 8k window. At 6,000 + 1,200 a
    # single call reserves ~3.9k tokens, two exceed the cap, and the governor
    # admits one per minute — halving throughput for ~10% more context.
    prompt_doc_budget: int = 5_400  # chars of doc text per call, split across docs
    hint_chars: int = 600

    # --- Paths ---
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    raw_dir: Path = ROOT / "data" / "raw"
    cache_dir: Path = ROOT / "data" / "cache"
    checkpoint_dir: Path = ROOT / "data" / "checkpoints"
    logs_dir: Path = ROOT / "logs"

    @classmethod
    def load(cls) -> "Settings":
        s = cls(
            composio_api_key=_clean("COMPOSIO_API_KEY"),
            composio_user_id=_clean("COMPOSIO_USER_ID") or "toolkit-recon",
            # GROQ_API_KEY is the concrete provider here; LLM_API_KEY lets you
            # point the same code at any OpenAI-compatible endpoint.
            llm_api_key=_clean("LLM_API_KEY") or _clean("GROQ_API_KEY"),
            llm_base_url=_clean("LLM_BASE_URL") or "https://api.groq.com/openai/v1",
            llm_model=_clean("LLM_MODEL") or "openai/gpt-oss-120b",
            concurrency=int(_clean("TOOLKIT_RECON_CONCURRENCY") or 8),
            domain_delay=float(_clean("TOOLKIT_RECON_DOMAIN_DELAY") or 1.5),
            llm_tokens_per_minute=int(_clean("LLM_TOKENS_PER_MINUTE") or 8_000),
            llm_concurrency=int(_clean("LLM_CONCURRENCY") or 2),
        )
        for d in (s.data_dir, s.raw_dir, s.cache_dir, s.checkpoint_dir, s.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        return s


settings = Settings.load()
