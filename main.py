"""
hoteldo-quoter: Microservico FastAPI para cotacao automatizada de hoteis no HotelDo.

Integra com o bot MARIA (WhatsApp/Zenvia) da LC Turismo para gerar cotacoes
de hotel automaticamente a partir dos dados de qualificacao de leads.

Deploy: Railway
Infra: HotelDo (Despegar/Decolar) via browser automation (Playwright)
"""

import asyncio
import logging
import os
import time
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

    # KEEPALIVE (05/11): a sessao do HotelDo expirava por inatividade e o re-login cai no
    # reCAPTCHA. Agora uma task renova a sessao a cada N minutos numa aba separada.
    _ka_task = asyncio.create_task(_session_keepalive())

    yield

    _ka_task.cancel()

    logger.info("=== hoteldo-quoter encerrando ===")
    await hoteldo_client.stop()


# ---------------------------------------------------------------------------
# Keepalive da sessao HotelDo
# ---------------------------------------------------------------------------

_keepalive = {"last_ok": None, "last_check": None, "fails": 0, "interval_min": int(os.environ.get("KEEPALIVE_MINUTES", "10"))}


async def _keepalive_once() -> bool:
    """Renova a sessao numa aba separada (nao mexe na aba das cotacoes)."""
    ctx = hoteldo_client._context
    if not ctx:
        return False
    page = await ctx.new_page()
    try:
        await page.goto(settings.hoteldo_base_url, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
        await page.wait_for_timeout(2000)
        if "/login" in page.url or "agent-not-logged" in page.url:
            return False
        ok = await page.evaluate("""async () => { try { const r = await fetch('/socrates/api/current-user/status'); return r.ok; } catch { return false; } }""")
        if not ok:
            await page.goto(f"{settings.hoteldo_base_url}/socrates/auth/social", wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
            await page.wait_for_timeout(2000)
            if "/login" in page.url:
                return False
            ok = await page.evaluate("""async () => { try { const r = await fetch('/socrates/api/current-user/status'); return r.ok; } catch { return false; } }""")
        if ok:
            await hoteldo_client._save_state()
        return bool(ok)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _session_keepalive() -> None:
    interval = max(3, _keepalive["interval_min"]) * 60
    while True:
        await asyncio.sleep(interval)
        _keepalive["last_check"] = time.time()
        try:
            if not hoteldo_client._authenticated:
                continue
            if await _keepalive_once():
                _keepalive["last_ok"] = time.time()
                _keepalive["fails"] = 0
                logger.info("KEEPALIVE: sessao HotelDo renovada")
            else:
                _keepalive["fails"] += 1
                logger.warning("KEEPALIVE: sessao nao respondeu (%d)", _keepalive["fails"])
                if _keepalive["fails"] >= 2:
                    hoteldo_client._authenticated = False
                    logger.error("KEEPALIVE: sessao HotelDo CAIU. Precisa de novo login + inject-cookies.")
        except Exception as exc:
            logger.warning("KEEPALIVE erro: %s", exc)


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
    product: str = Field(default="hotel", description="hotel | flight | package")
    origem: Optional[str] = Field(default=None, description="Cidade/aeroporto de saida")
    infants: int = Field(default=0, ge=0, le=4)

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
    # KEEPALIVE: se a sessao marcada como autenticada nao renova ha muito tempo, nao confiar
    if health.get("session_authenticated") and _keepalive["last_check"] and _keepalive["fails"] >= 2:
        health["session_authenticated"] = False
        all_ok = False

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
        "POST /quote: product=%s destino=%s origem=%s checkin=%s checkout=%s",
        request.product, request.destino, request.origem, request.checkin, request.checkout,
    )

    maria_request = MariaQuoteRequest(
        destino=request.destino,
        checkin=request.checkin,
        checkout=request.checkout,
        adults=request.adults,
        children=request.children,
        children_ages=request.children_ages,
        rooms=request.rooms,
        product=request.product,
        origem=request.origem,
        infants=request.infants,
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
# Admin Endpoints — Cookie Injection
# ---------------------------------------------------------------------------

ADMIN_COOKIE_EXTRACT_SCRIPT = """
// Rode este script no Console do Chrome enquanto logado no HotelDo
// (em qualquer pagina de pt-hoteldo.e-agencias.com ou ag00124871-hoteldo.e-agencias.com)
//
// Ele copia os cookies pro clipboard. Depois cole no endpoint:
// POST https://hoteldo-quoter-production.up.railway.app/admin/inject-cookies

(async () => {
    const cookies = document.cookie.split('; ').map(c => {
        const [name, ...rest] = c.split('=');
        return {
            name: name,
            value: rest.join('='),
            domain: '.' + location.hostname.split('.').slice(-3).join('.'),
            path: '/',
            secure: location.protocol === 'https:',
            sameSite: 'Lax'
        };
    });
    const json = JSON.stringify(cookies, null, 2);
    try {
        await navigator.clipboard.writeText(json);
        console.log('Cookies copiados! (' + cookies.length + ' cookies)');
        console.log('Cole no POST /admin/inject-cookies');
    } catch {
        console.log(json);
        console.log('Copie o JSON acima manualmente.');
    }
})();
""".strip()


class InjectCookiesRequest(BaseModel):
    """Request para injetar cookies."""
    cookies: list[dict[str, Any]]
    password: str = Field(..., description="Senha admin (lcviagens2024)")


@app.get("/admin/extract-script")
async def get_extract_script():
    """
    Retorna o script JavaScript que Lucas deve rodar no Console do Chrome
    enquanto logado no HotelDo para extrair os cookies de sessao.
    """
    return {
        "instructions": (
            "1. Abra o HotelDo no Chrome (ja logado)\n"
            "2. Abra o Console (F12 → Console)\n"
            "3. Cole e execute o script abaixo\n"
            "4. Os cookies serao copiados pro clipboard\n"
            "5. Faca POST /admin/inject-cookies com {cookies: <COLAR>, password: 'lcviagens2024'}"
        ),
        "script": ADMIN_COOKIE_EXTRACT_SCRIPT,
    }


@app.post("/admin/inject-cookies")
async def inject_cookies(request: InjectCookiesRequest):
    """
    Injeta cookies de sessao do Chrome no Playwright.
    Isso bypassa o reCAPTCHA — o servico usa a sessao do Lucas.
    """
    import os

    expected_pw = os.environ.get("ADMIN_PASSWORD", "lcviagens2024")
    if request.password != expected_pw:
        raise HTTPException(status_code=401, detail="Senha incorreta")

    if not request.cookies:
        raise HTTPException(status_code=400, detail="Lista de cookies vazia")

    logger.info("Recebido POST /admin/inject-cookies com %d cookies", len(request.cookies))

    # Salvar cookies em disco para reutilizacao em restarts
    from pathlib import Path
    cookies_path = str(Path(settings.browser_state_path) / "cookies.json")
    hoteldo_client.save_cookies_to_file(request.cookies, cookies_path)

    # Injetar no contexto Playwright ativo
    try:
        success = await hoteldo_client.inject_cookies(request.cookies)
    except Exception as exc:
        logger.error("Erro ao injetar cookies: %s", exc)
        raise HTTPException(status_code=500, detail=f"Erro: {exc}")

    if success:
        return {
            "status": "ok",
            "message": "Cookies injetados — sessao autenticada!",
            "authenticated": True,
        }
    else:
        return {
            "status": "partial",
            "message": (
                "Cookies salvos mas sessao nao autenticou. "
                "Possivel que os cookies ja expiraram. "
                "Relogue no Chrome e extraia novamente."
            ),
            "authenticated": False,
        }


class TokenLoginRequest(BaseModel):
    password: str
    status_token: str
    login_token: str


@app.post("/admin/token-login")
async def token_login(request: TokenLoginRequest):
    """
    Login via HTTP direto usando tokens reCAPTCHA gerados num Chrome real.
    Os cookies httpOnly retornados pelo HotelDo sao injetados no Playwright.

    Para gerar os tokens: GET /admin/token-script e rodar no console de
    qualquer pagina *.e-agencias.com. O script gera os dois tokens e ja
    faz o POST aqui.
    """
    import os

    expected_pw = os.environ.get("ADMIN_PASSWORD", "lcviagens2024")
    if request.password != expected_pw:
        raise HTTPException(status_code=401, detail="Senha incorreta")

    logger.info("Recebido POST /admin/token-login")
    try:
        result = await hoteldo_client.login_with_tokens(
            status_token=request.status_token,
            login_token=request.login_token,
        )
    except Exception as exc:
        logger.exception("Erro no token-login")
        raise HTTPException(status_code=500, detail=f"Erro: {exc}")

    return {
        "status": "ok" if result.get("authenticated") else "fail",
        **result,
    }


TOKEN_LOGIN_SCRIPT = r"""
// Rodar no console de qualquer pagina *.e-agencias.com (ex: ag00124871-hoteldo).
// Gera os dois tokens reCAPTCHA e envia pro hoteldo-quoter fazer o login.
(async () => {
  const SERVICE = 'https://hoteldo-quoter-production.up.railway.app';
  const PW = prompt('Senha admin do hoteldo-quoter:');
  const KEY_CB  = '6Lf8DV4UAAAAAASCK2DF67REViA95WPoXf-1vZsH';   // checkbox (user/status)
  const KEY_INV = '6Lf-DV4UAAAAAAYyG-Mh_BArXmHQUiutk7kN1FRg';   // invisivel (login)
  await new Promise((res, rej) => {
    if (window.grecaptcha && grecaptcha.render) return res();
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/api.js?render=explicit';
    s.onload = () => setTimeout(res, 1500); s.onerror = rej;
    document.head.appendChild(s);
  });
  const mk = (id, css) => { const d = document.createElement('div'); d.id = id; if (css) d.style.cssText = css; document.body.appendChild(d); return d; };
  mk('lc-inv');
  mk('lc-cb', 'position:fixed;top:80px;left:20px;z-index:99999;background:#fff;padding:8px;border:2px solid #0100FE');
  const tok = {};
  const wInv = grecaptcha.render('lc-inv', { sitekey: KEY_INV, size: 'invisible', callback: t => tok.inv = t });
  grecaptcha.render('lc-cb', { sitekey: KEY_CB, callback: t => tok.cb = t });
  grecaptcha.execute(wInv);
  console.log('Clique no checkbox "Nao sou um robo" que apareceu no canto superior esquerdo...');
  while (!tok.cb || !tok.inv) await new Promise(r => setTimeout(r, 500));
  console.log('Tokens prontos, enviando...');
  const r = await fetch(SERVICE + '/admin/token-login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: PW, status_token: tok.cb, login_token: tok.inv })
  });
  const j = await r.json(); console.log(j); alert('hoteldo-quoter: ' + j.status);
  document.getElementById('lc-cb').remove();
})();
"""


@app.get("/admin/token-script")
async def token_script():
    """Retorna o script JS que gera tokens reCAPTCHA e dispara o token-login."""
    return {"script": TOKEN_LOGIN_SCRIPT}


@app.get("/admin/keepalive")
async def keepalive_status():
    return {**_keepalive, "authenticated": hoteldo_client._authenticated}


@app.get("/admin/debug-session")
async def debug_session():
    """Diagnostico da sessao Playwright (cookies, URL, status API)."""
    return await hoteldo_client.debug_session()


@app.get("/admin/debug-search")
async def debug_search(destination_id: str = "CIT_6624", checkin: str = "2027-01-15",
                       checkout: str = "2027-01-20", max_chars: int = 6000):
    """Devolve a resposta bruta da API de availability (pra ajustar o parser)."""
    return await hoteldo_client.debug_search_raw(destination_id, checkin, checkout, max_chars)


@app.get("/admin/debug-capture")
async def debug_capture(url: str, match: str, max_chars: int = 8000, wait_s: int = 45,
                        click_text: str = ""):
    """Navega pra uma URL do HotelDo e devolve responses cujo URL contem `match`."""
    return await hoteldo_client.debug_capture(url, match, max_chars, wait_s, click_text)


@app.get("/admin/session-status")
async def session_status():
    """Verifica status da sessao atual."""
    health = await hoteldo_client.check_health()
    from pathlib import Path
    cookies_file = Path(settings.browser_state_path) / "cookies.json"
    state_file = Path(settings.browser_state_path) / "state.json"

    return {
        "authenticated": health.get("session_authenticated", False),
        "browser_running": health.get("browser_running", False),
        "cookies_file_exists": cookies_file.exists(),
        "state_file_exists": state_file.exists(),
        "suggestion": (
            "Sessao OK" if health.get("session_authenticated")
            else "Use GET /admin/extract-script para obter o script de extracao de cookies"
        ),
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
