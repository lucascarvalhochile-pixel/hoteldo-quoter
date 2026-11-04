"""
Configuracao centralizada do hoteldo-quoter.
Todas as variaveis vem de environment variables para deploy seguro no Railway.
"""

import os
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuracoes do servico hoteldo-quoter."""

    # --- HotelDo Portal ---
    hoteldo_base_url: str = "https://ag00124871-hoteldo.e-agencias.com"
    hoteldo_login_url: str = "https://pt-hoteldo.e-agencias.com/login/signin"
    hoteldo_email: str = ""
    hoteldo_password: str = ""
    hoteldo_agency_id: str = "AG00124871"
    # E-mail usado no "Gerar cotacao" quando o cliente nao tem e-mail
    # (a cotacao fica em Minhas Cotacoes do Socrates de qualquer forma)
    quote_default_email: str = "camilagusmao@lucascarvalhoturismo.com.br"
    quote_default_phone: str = "11999999999"

    # --- API Endpoints (Despegar/fenix backend) ---
    hoteldo_suggestions_url: str = (
        "https://ag00124871-hoteldo.e-agencias.com/suggestions"
    )
    hoteldo_socrates_url: str = (
        "https://ag00124871-hoteldo.e-agencias.com/socrates/api/interaction-draft"
    )

    # --- Playwright ---
    headless: bool = True
    browser_timeout_ms: int = 30_000
    navigation_timeout_ms: int = 60_000
    # Caminho para persistir estado do browser (cookies, localStorage)
    browser_state_path: str = "/tmp/hoteldo-browser-state"

    # --- FastAPI ---
    host: str = "0.0.0.0"
    port: int = int(os.environ.get("PORT", "8000"))
    log_level: str = "info"
    debug: bool = False

    # --- MARIA Integration ---
    maria_webhook_secret: Optional[str] = None  # HMAC para validar chamadas da MARIA
    maria_api_url: Optional[str] = None  # URL do bot MARIA para callbacks

    model_config = {
        "env_prefix": "",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


settings = Settings()
