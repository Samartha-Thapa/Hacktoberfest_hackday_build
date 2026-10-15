from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Talkie Mail"
    app_env: str = "development"
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    session_secret: str | None = None
    session_cookie_secure: bool = False
    session_max_age: int = 3600
    oauth_state_max_age: int = 600
    llama_cpp_base_url: str = "http://127.0.0.1:8080"
    llama_cpp_model: str = "gemma"
    llama_cpp_timeout: float = 60.0
    llama_cpp_context_size: int = 2048
    llama_cpp_max_tokens: int = 600

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_cookie_security(self) -> "Settings":
        local_environments = {"development", "dev", "local", "test"}
        if (
            self.app_env.strip().lower() not in local_environments
            and not self.session_cookie_secure
        ):
            raise ValueError(
                "SESSION_COOKIE_SECURE must be true outside local development."
            )
        if self.session_max_age <= 0 or self.oauth_state_max_age <= 0:
            raise ValueError("Session and OAuth state ages must be positive.")
        if self.llama_cpp_timeout <= 0:
            raise ValueError("LLAMA_CPP_TIMEOUT must be positive.")
        if self.llama_cpp_context_size <= 0 or self.llama_cpp_max_tokens <= 0:
            raise ValueError("LLAMA_CPP_CONTEXT_SIZE and LLAMA_CPP_MAX_TOKENS must be positive.")
        if self.llama_cpp_max_tokens >= self.llama_cpp_context_size:
            raise ValueError("LLAMA_CPP_MAX_TOKENS must be smaller than LLAMA_CPP_CONTEXT_SIZE.")
        return self

    @property
    def google_oauth_configured(self) -> bool:
        return all(
            (
                self.google_oauth_client_id,
                self.google_oauth_client_secret,
                self.google_oauth_redirect_uri,
                self.session_secret,
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
