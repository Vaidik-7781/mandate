"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

VI_COMMIT = "356c29635f1c44df7de02edb58699ca9f29bece6"

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = "WEBHOOK_ID"
    paypal_env: str = "sandbox"  # sandbox only for this project
    paypal_base_url: str = ""  # override; empty -> derived from paypal_env

    database_url: str = "sqlite:///./mandate.db"

    model_name: str = "claude-sonnet-5-5"
    llm_api_key: str = ""
    llm_backend: str = "auto"  # auto | offline | anthropic | openai
    llm_base_url: str = (
        ""  # openai-compatible endpoint (vLLM, Ollama, OpenRouter, ...); empty = api.openai.com
    )
    llm_record: bool = False  # record live answers as replay fixtures (needs LLM_API_KEY)
    llm_max_calls: int = 500  # hard cost cap per process
    public_base_url: str = ""  # where PayPal sends the buyer back after approval; empty = no return URLs
    rate_limit_per_minute: int = 120  # write requests per client address per minute (0 disables)

    # Website: canonical URLs, analytics, search console, contact form (see docs/launch-checklist.md)
    force_https: bool = False  # 301 http to https when the proxy says the request came in over http
    ga_measurement_id: str = ""  # e.g. G-XXXXXXXXXX; empty = no analytics code is ever loaded
    google_site_verification: str = ""  # token for Google Search Console's HTML-tag verification
    contact_email: str = ""  # shown on the contact page as a fallback address (optional)
    contact_to: str = ""  # where new contact messages are emailed (optional; they are always stored)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    admin_token: str = ""  # bearer token for reading contact messages; empty disables that endpoint
    replay_mode: bool | None = None  # None -> true unless PayPal credentials are present
    fixtures_dir: str = str(Path(__file__).resolve().parents[3] / "fixtures")  # repo root, not the cwd
    task_runner: str = "local"  # local | render (render is an unverified optional adapter)

    vi_commit: str = VI_COMMIT
    secret_seed: str = "mandate-demo-seed-change-me"  # derives demo keys and the guard token HMAC key

    # Guard policy knobs
    velocity_max_attempts: int = 8
    velocity_window_seconds: int = 60
    step_up_threshold_minor: int = 0  # 0 disables the amount-based step-up
    dispute_human_threshold_minor: int = 10_000  # $100
    dispute_fee_minor: int = 0
    dispute_cost_to_fight_minor: int = 500
    evidence_min_completeness: float = 0.8

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    @property
    def is_replay(self) -> bool:
        if self.replay_mode is not None:
            return self.replay_mode
        return not (self.paypal_client_id and self.paypal_client_secret)

    @property
    def effective_paypal_base(self) -> str:
        if self.paypal_base_url:
            return self.paypal_base_url.rstrip("/")
        return LIVE_BASE if self.paypal_env == "live" else SANDBOX_BASE


@lru_cache
def get_settings() -> Settings:
    return Settings()
