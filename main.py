"""
hoteldo-quoter: Microservico FastAPI para cotacao automatizada de hoteis no HotelDo.

Integra com o bot MARIA (WhatsApp/Zenvia) da LC Turismo para gerar cotacoes
de hotel automaticamente a partir dos dados de qualificacao de leads.

Deploy: Railway
Infra: HotelDo (Despegar/Decolar) via browser automation (Playwright)
"""

import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import settings
from hoteldo_client import HotelDoClient
from maria_bridge import MariaBridge, MariaQuoteRequest, MariaQuoteResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hoteldo_quoter")

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

hoteldo_client = HotelDoClient()
maria_bridge = MariaBridge(hoteldo_client)

# ---------------------------------------------------------------------------
# Lifespan (startup/shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerencia o ciclo de vida do browser Playwright."""
    logger.info("=== hoteldo-quoter iniciando ===")
    logger.info("Base URL: %s", settings.hoteldo_base_url)
    logger.info("Agency ID: %s", settings.hoteldo_agency_id)

    await hoteldo_client.start()

    # Tentar login automatico se credenciais estao configuradas
    if settings.hoteldo_email and settings.hoteldo_password:
        try:
            await hoteldo_client.login()
            logger.info("Login automatico realizado com sucesso")
        except Exception as exc:
            logger.warning("Login automatico falhou (sera tentado sob demanda): %s", exc)

    yield

    logger.info("=== hoteldo-quoter encerrando ===")
    await hoteldo_client.stop()


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="hoteldo-quoter",
    description=(
        "Microservico de cotacao automatizada de hoteis via HotelDo "
        "para integracao com o bot MARIA (LC Turismo)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class QuoteRequest(BaseModel):
    """Requisicao de cotacao de hotel."""

    destino: str = Field(..., description="Destino desejado (nome da cidade)")
    checkin: str = Field(..., description="Data de checkin (YYYY-MM-DD ou DD/MM/YYYY)")
    checkout: str = Field(..., description="Data de checkout (YYYY-MM-DD ou DD/MM/YYYY)")
    adults: int = Field(default=2, ge=1, le=10, description="Adultos por quarto")
    children: int = Field(default=0, ge=0, le=6, description="Criancas por quarto")
    children_ages: list[int] = Field(default_factory=list, description="Idades das criancas")
    rooms: int = Field(default=1, ge=1, le=5, description="Numero de quartos")

    # Dados do cliente (opccionais)
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None

    # Metadata MARIA
    lead_id: Optional[str] = None
    conversation_id: Optional[str] = None


class SearchRequest(BaseModel):
    """Requisicao de busca de hoteis (sem criar cotacao)."""

    destination_id: str = Field(..., description="ID do destino HotelDo (ex: CIT_6624)")
    checkin: str = Field(..., description="Data de checkin (YYYY-MM-DD)")
    checkout: str = Field(..., description="Data de checkout (YYYY-MM-DD)")
    adults: int = Field(default=2, ge=1, le=10)
    children: int = Field(default=0, ge=0, le=6)
    children_ages: list[int] = Field(default_factory=list)
    rooms: int = Field(default=1, ge=1, le=5)


class HealthResponse(BaseModel):
    """Resposta do health check."""

    status: str
    service: str = "hoteldo-quoter"
    version: str = "0.1.0"
    browser_running: bool
    session_authenticated: bool
    suggestions_api: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check do servico.
    Verifica browser Playwright, sessao autenticada e conectividade com APIs.
    """
    health = await hoteldo_client.check_health()
    all_ok = all(health.values())

    return HealthResponse(
        status="healthy" if all_ok else "degraded",
        browser_running=health["browser_running"],
        session_authenticated=health["session_authenticated"],
        suggestions_api=health["suggestions_api"],
    )


@app.post("/quote", response_model=MariaQuoteResponse)
async def create_quote(request: QuoteRequest):
    """
    Gera uma cotacao de hotel no HotelDo.

    Fluxo:
    1. Resolve o destino para um ID do HotelDo
    2. Busca hoteis disponiveis
    3. Formata resposta para envio via MARIA no WhatsApp

    Este endpoint eh chamado pela MARIA quando um lead pede cotacao de hotel.
    """
    logger.info(
        "POST /quote: destino=%s, checkin=%s, checkout=%s",
        request.destino, request.checkin, request.checkout,
    )

    maria_request = MariaQuoteRequest(
        destino=request.destino,
        checkin=request.checkin,
        checkout=request.checkout,
        adults=request.adults,
        children=request.children,
        children_ages=request.children_ages,
        rooms=request.rooms,
        client_name=request.client_name,
        client_email=request.client_email,
        client_phone=request.client_phone,
        lead_id=request.lead_id,
        conversation_id=request.conversation_id,
    )

    try:
        response = await maria_bridge.generate_quote(maria_request)
        return response
    except Exception as exc:
        logger.error("Erro ao gerar cotacao: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erro interno: {exc}")


@app.post("/search")
async def search_hotels(request: SearchRequest):
    """
    Busca hoteis para um destino especifico.
    Usa o destination_id diretamente (sem resolver nome).
    """
    logger.info(
        "POST /search: destination_id=%s, checkin=%s, checkout=%s",
        request.destination_id, request.checkin, request.checkout,
    )

    try:
        checkin = date.fromisoformat(request.checkin)
        checkout = date.fromisoformat(request.checkout)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Data invalida: {exc}")

    try:
        results = await hoteldo_client.search_hotels(
            destination_id=request.destination_id,
            checkin=checkin,
            checkout=checkout,
            adults=request.adults,
            children=request.children,
            children_ages=request.children_ages,
            rooms=request.rooms,
        )
        return results
    except Exception as exc:
        logger.error("Erro na busca: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erro na busca: {exc}")


@app.get("/suggestions/{query}")
async def get_suggestions(query: str):
    """
    Proxy para a API de sugestoes do HotelDo.

    Retorna cidades e hoteis que correspondem ao termo de busca.
    Util para resolver nomes de destinos para IDs (ex: "Santiago" -> CIT_6624).
    """
    logger.info("GET /suggestions/%s", query)

    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Query deve ter pelo menos 2 caracteres")

    results = await hoteldo_client.get_suggestions(query)
    return {"query": query, "results": results, "count": len(results)}


@app.get("/destinations")
async def list_destinations():
    """
    Lista os destinos mapeados com seus IDs do HotelDo.
    Util para debug e para a MARIA saber quais destinos estao disponiveis.
    """
    from maria_bridge import DESTINATION_MAP

    mapped = {k: v for k, v in DESTINATION_MAP.items() if v}
    unmapped = [k for k, v in DESTINATION_MAP.items() if not v]

    return {
        "mapped": mapped,
        "unmapped": unmapped,
        "total_mapped": len(mapped),
        "total_unmapped": len(unmapped),
    }


# ---------------------------------------------------------------------------
# Error handler global
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Erro nao tratado: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Erro interno do servidor", "error": str(exc)},
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=settings.debug,
    )
