"""
Cliente de automacao HotelDo via Playwright.

O HotelDo (ag00124871-hoteldo.e-agencias.com) eh construido sobre a infraestrutura
Despegar/Decolar — um Angular SPA com backend em fenix.despegar.io. Nao existe API
publica; toda interacao depende de sessao autenticada via cookies.

Fluxo descoberto (Etapa 1):
1. Login: formulario padrao de credenciais no portal
2. Suggestions API: GET /suggestions?locale=pt_BR&profile=sbox-hotels-facet&hint={query}
   - Retorna {items: [{group: "CITY", items: [{id: "CIT_6624", ...}]}]}
3. Busca: URL direta /accommodations/results/{CIT_ID}/{checkin}/{checkout}/{guests}
   - Interceptar response de /s-accommodations/api/accommodations/availability
4. Cotacao: selecionar hotel → detalhe → adicionar ao carrinho → Socrates CRM
   - Link compartilhavel: /quotations/{tripId} (PUBLICO, sem autenticacao)
"""

import asyncio
import json
import logging
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Optional

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    async_playwright,
)

from config import settings
from hoteldo_auth import TokenAuthMixin
from hoteldo_quote import HotelDoQuoteError, QuoteMixin

logger = logging.getLogger("hoteldo_quoter.client")

# Timeout para aguardar resultados de busca (hoteis podem demorar)
SEARCH_RESULTS_TIMEOUT_MS = 45_000
# Maximo de hoteis para extrair dos resultados
MAX_HOTELS_TO_EXTRACT = 10
# Timeout para operacoes no carrinho/Socrates
QUOTE_TIMEOUT_MS = 30_000


class HotelDoAuthError(Exception):
    """Falha na autenticacao com o portal HotelDo."""


class HotelDoSearchError(Exception):
    """Falha na busca de hoteis."""


class HotelDoClient(TokenAuthMixin, QuoteMixin):
    """
    Cliente Playwright para automacao do portal HotelDo.

    Mantém uma sessao persistente de browser, reutilizando cookies entre chamadas.
    O estado do browser eh salvo em disco para sobreviver a restarts.

    Estrategia hibrida:
    - Suggestions API: HTTP direto (sem browser, sem autenticacao)
    - Busca de hoteis: Playwright navega para URL direta, intercepta response da API
    - Cotacao: Playwright navega pelo fluxo carrinho → Socrates
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._authenticated: bool = False
        self._login_lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Inicia o browser Playwright e restaura estado salvo se existir."""
        logger.info("Iniciando browser Playwright (headless=%s)", settings.headless)
        self._playwright = await async_playwright().start()

        state_path = Path(settings.browser_state_path)
        state_file = state_path / "state.json"

        self._browser = await self._playwright.chromium.launch(
            headless=settings.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self._context = await self._browser.new_context(
            storage_state=str(state_file) if state_file.exists() else None,
            viewport={"width": 1920, "height": 1080},
            locale="pt-BR",
            timezone_id="America/Santiago",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.browser_timeout_ms)
        self._page.set_default_navigation_timeout(settings.navigation_timeout_ms)

        logger.info("Browser iniciado com sucesso")

    async def stop(self) -> None:
        """Para o browser e salva estado para reutilizacao."""
        if self._context:
            await self._save_state()
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser encerrado")

    async def _save_state(self) -> None:
        """Persiste cookies e localStorage em disco."""
        state_path = Path(settings.browser_state_path)
        state_path.mkdir(parents=True, exist_ok=True)
        if self._context:
            await self._context.storage_state(path=str(state_path / "state.json"))
            logger.debug("Estado do browser salvo em %s", state_path)

    async def ensure_authenticated(self) -> None:
        """Garante que a sessao esta autenticada. Faz login se necessario."""
        if self._authenticated:
            # Verificar se sessao ainda eh valida via Socrates API
            if await self._check_session_valid():
                return
            logger.warning("Sessao expirou, refazendo login...")
            self._authenticated = False
        await self.login()

    async def _socrates_status_ok(self) -> bool:
        try:
            response = await self._page.evaluate("""
                async () => {
                    try {
                        const r = await fetch('/socrates/api/current-user/status');
                        return r.ok;
                    } catch {
                        return false;
                    }
                }
            """)
            return bool(response)
        except Exception:
            return False

    async def _check_session_valid(self) -> bool:
        """
        Verifica se a sessao autenticada ainda eh valida.

        O Socrates (CRM de cotacoes) tem sessao propria, criada pelo handshake
        GET /socrates/auth/social a partir da sessao social (cookie dss).
        Se o status retornar 401, faz o handshake uma vez e tenta de novo.
        """
        if await self._socrates_status_ok():
            return True
        try:
            logger.info("Socrates 401 — fazendo handshake /socrates/auth/social")
            await self._page.goto(
                f"{settings.hoteldo_base_url}/socrates/auth/social",
                wait_until="domcontentloaded",
                timeout=settings.navigation_timeout_ms,
            )
            await self._page.wait_for_timeout(3000)
            if "/login" in self._page.url:
                return False
            return await self._socrates_status_ok()
        except Exception as exc:
            logger.warning("Handshake Socrates falhou: %s", exc)
            return False

    # -------------------------------------------------------------------------
    # Cookie Injection (bypass reCAPTCHA)
    # -------------------------------------------------------------------------

    async def inject_cookies(self, cookies: list[dict]) -> bool:
        """
        Injeta cookies de sessao no contexto Playwright.

        Args:
            cookies: Lista de dicts no formato Playwright/Chrome:
                     [{name, value, domain, path, ...}, ...]

        Returns:
            True se a sessao ficou autenticada apos injecao.
        """
        if not self._context:
            raise HotelDoAuthError("Browser nao iniciado — chame start() primeiro")

        logger.info("Injetando %d cookies no contexto Playwright...", len(cookies))

        # Normalizar cookies pro formato Playwright
        pw_cookies = []
        for c in cookies:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".e-agencias.com"),
                "path": c.get("path", "/"),
            }
            # Playwright exige sameSite valido
            ss = c.get("sameSite", "Lax")
            if ss in ("Strict", "Lax", "None"):
                cookie["sameSite"] = ss
            else:
                cookie["sameSite"] = "Lax"
            # secure e httpOnly
            if c.get("secure"):
                cookie["secure"] = True
            if c.get("httpOnly"):
                cookie["httpOnly"] = True
            pw_cookies.append(cookie)

        await self._context.add_cookies(pw_cookies)
        logger.info("Cookies injetados. Verificando sessao...")

        # Navegar para o portal e verificar se a sessao esta ativa
        await self._page.goto(
            settings.hoteldo_base_url,
            wait_until="domcontentloaded",
            timeout=settings.navigation_timeout_ms,
        )
        await self._page.wait_for_timeout(3000)

        current_url = self._page.url
        if "/login" not in current_url and "/auth" not in current_url:
            if await self._check_session_valid():
                self._authenticated = True
                await self._save_state()
                logger.info("Cookie injection OK — sessao autenticada (URL: %s)", current_url)
                return True

        logger.warning(
            "Cookie injection falhou — sessao nao autenticada (URL: %s)", current_url
        )
        return False

    async def inject_cookies_from_file(self, path: str) -> bool:
        """Carrega cookies de um JSON file e injeta."""
        try:
            with open(path, "r") as f:
                cookies = json.load(f)
            return await self.inject_cookies(cookies)
        except FileNotFoundError:
            logger.debug("Arquivo de cookies nao encontrado: %s", path)
            return False
        except Exception as exc:
            logger.warning("Erro ao carregar cookies de %s: %s", path, exc)
            return False

    def save_cookies_to_file(self, cookies: list[dict], path: str) -> None:
        """Salva cookies brutos em um JSON file para reutilizacao."""
        state_path = Path(path).parent
        state_path.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(cookies, f, indent=2)
        logger.info("Cookies salvos em %s", path)

    # -------------------------------------------------------------------------
    # Login
    # -------------------------------------------------------------------------

    async def login(self) -> None:
        """
        Autentica no portal HotelDo.

        Estrategia em camadas:
        1. Tentar restaurar sessao salva em disco (state.json)
        2. Tentar cookies injetados via /admin/inject-cookies (cookies.json)
        3. Fallback: login via browser (vai falhar se reCAPTCHA bloquear)

        O reCAPTCHA v2 na pagina de login bloqueia automacao headless.
        A forma confiavel de autenticar eh via cookie injection:
        - Lucas loga manualmente no Chrome
        - Roda script no console que extrai cookies
        - POST /admin/inject-cookies com o JSON
        - Servico usa esses cookies ate expirarem
        """
        async with self._login_lock:
            if self._authenticated:
                return

            logger.info("Iniciando login no HotelDo para %s", settings.hoteldo_email)

            # --- Camada 1: estado salvo (cookies de sessao anterior) ---
            try:
                await self._page.goto(
                    settings.hoteldo_base_url,
                    wait_until="domcontentloaded",
                    timeout=settings.navigation_timeout_ms,
                )
                await self._page.wait_for_timeout(3000)

                current_url = self._page.url
                if "/login" not in current_url and "/auth" not in current_url:
                    if await self._check_session_valid():
                        logger.info("Sessao restaurada de estado salvo")
                        self._authenticated = True
                        return
            except Exception as exc:
                logger.warning("Falha ao restaurar estado salvo: %s", exc)

            # --- Camada 2: cookies injetados ---
            cookies_file = str(
                Path(settings.browser_state_path) / "cookies.json"
            )
            if await self.inject_cookies_from_file(cookies_file):
                return

            # --- Camada 3: login via browser (fallback) ---
            if not settings.hoteldo_email or not settings.hoteldo_password:
                raise HotelDoAuthError(
                    "Sessao expirada e credenciais nao configuradas. "
                    "Use POST /admin/inject-cookies para injetar cookies do Chrome."
                )

            logger.warning(
                "Tentando login via browser (reCAPTCHA pode bloquear)..."
            )
            await self._try_browser_login()

    async def _try_browser_login(self) -> None:
        """
        Tenta login via automacao do browser.
        AVISO: reCAPTCHA v2 bloqueia em headless. Este eh um fallback.
        """
        try:
            await self._page.goto(
                settings.hoteldo_login_url,
                wait_until="domcontentloaded",
                timeout=settings.navigation_timeout_ms,
            )
            await self._page.wait_for_timeout(3000)

            # Remover flag de automacao
            await self._page.evaluate("""
                () => {
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                }
            """)

            # Preencher email
            logger.info("Preenchendo email...")
            email_sel = (
                'input[placeholder*="exemplo"], '
                'input[placeholder*="mail"], '
                'input[type="text"][placeholder]'
            )
            await self._page.wait_for_selector(
                email_sel, state="visible", timeout=15000
            )
            email_input = self._page.locator(email_sel).first
            await email_input.click()
            await self._page.wait_for_timeout(500)
            await email_input.fill(settings.hoteldo_email)
            await self._page.wait_for_timeout(1000)

            # Tentar reCAPTCHA (usar iframe anchor especificamente)
            logger.info("Verificando reCAPTCHA...")
            try:
                recaptcha_frame = self._page.frame_locator(
                    'iframe[src*="recaptcha/api2/anchor"]'
                )
                checkbox = recaptcha_frame.locator(
                    '#recaptcha-anchor, .recaptcha-checkbox-border'
                ).first
                if await checkbox.count() > 0:
                    logger.info("reCAPTCHA encontrado, tentando clicar...")
                    await checkbox.click()
                    await self._page.wait_for_timeout(5000)
            except Exception as e:
                logger.warning("reCAPTCHA handling falhou: %s — continuando", e)

            # Clicar "Seguinte"
            logger.info("Clicando Seguinte...")
            next_sel = (
                'a:has-text("Seguinte"), '
                'a:has-text("Siguiente"), '
                'a:has-text("Next"), '
                'button:has-text("Seguinte")'
            )
            await self._page.locator(next_sel).first.click()
            await self._page.wait_for_timeout(3000)

            logger.info("URL apos Seguinte: %s", self._page.url)

            # Preencher senha
            password_sel = 'input[type="password"]'
            try:
                await self._page.wait_for_selector(
                    password_sel, state="visible", timeout=15000
                )
                logger.info("Campo de senha encontrado, preenchendo...")
                await self._page.locator(password_sel).first.fill(
                    settings.hoteldo_password
                )
                await self._page.wait_for_timeout(500)

                submit_sel = (
                    'button[type="submit"], '
                    'a:has-text("Entrar"), '
                    'button:has-text("Entrar")'
                )
                await self._page.locator(submit_sel).first.click()
            except Exception:
                page_text = await self._page.evaluate(
                    "() => document.body?.innerText?.substring(0, 500) || ''"
                )
                logger.error(
                    "Nao encontrou campo de senha. URL: %s, Texto: %s",
                    self._page.url, page_text[:200]
                )
                raise HotelDoAuthError(
                    "Login travou na etapa de senha — reCAPTCHA bloqueou. "
                    "Use POST /admin/inject-cookies para injetar cookies do Chrome."
                )

            # Verificar sucesso
            await self._page.wait_for_timeout(5000)
            post_url = self._page.url
            if "/login" in post_url or "/auth" in post_url:
                raise HotelDoAuthError(
                    "Login falhou — reCAPTCHA bloqueou. "
                    "Use POST /admin/inject-cookies para injetar cookies do Chrome."
                )

            await self._save_state()
            self._authenticated = True
            logger.info("Login via browser OK (URL: %s)", post_url)

        except HotelDoAuthError:
            raise
        except Exception as exc:
            logger.error("Falha no login via browser: %s", exc)
            raise HotelDoAuthError(
                f"Login via browser falhou: {exc}. "
                "Use POST /admin/inject-cookies para injetar cookies do Chrome."
            ) from exc

    # -------------------------------------------------------------------------
    # Suggestions API (HTTP direto, sem browser, sem autenticacao)
    # -------------------------------------------------------------------------

    async def get_suggestions(self, query: str) -> list[dict[str, Any]]:
        """
        Busca sugestoes de cidades/hoteis via API de suggestions do HotelDo.

        Endpoint: GET /suggestions?locale=pt_BR&profile=sbox-hotels-facet&hint={query}

        Response real:
        {
            "items": [
                {
                    "group": "CITY",
                    "items": [
                        {"id": "CIT_6624", "label": "Santiago, ...", ...},
                        ...
                    ]
                },
                {
                    "group": "HOTEL",
                    "items": [...]
                }
            ]
        }

        Retorna lista plana com todos os itens, cada um com campo "group" adicionado.
        """
        logger.info("Buscando sugestoes para: %s", query)

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(
                    settings.hoteldo_suggestions_url,
                    params={
                        "locale": "pt_BR",
                        "profile": "sbox-hotels-facet",
                        "hint": query,
                    },
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "hoteldo-quoter/1.0",
                    },
                )
                response.raise_for_status()
                data = response.json()

                # Parsear estrutura aninhada:
                # {items: [{group: "CITY", items: [...]}, {group: "HOTEL", items: [...]}]}
                results: list[dict[str, Any]] = []
                if isinstance(data, dict) and "items" in data:
                    for group in data["items"]:
                        group_name = group.get("group", "UNKNOWN")
                        for item in group.get("items", []):
                            item["type"] = group_name  # CITY, HOTEL, etc.
                            results.append(item)
                elif isinstance(data, list):
                    # Fallback: se a API retornar array plano
                    results = data

                logger.info("Sugestoes encontradas: %d resultados", len(results))
                return results

            except httpx.HTTPError as exc:
                logger.error("Erro na API de sugestoes: %s", exc)
                return []

    # -------------------------------------------------------------------------
    # Helpers: construir URL de busca e distribution string
    # -------------------------------------------------------------------------

    @staticmethod
    def _build_distribution(
        adults: int = 2,
        children: int = 0,
        children_ages: list[int] | None = None,
        rooms: int = 1,
    ) -> str:
        """
        Constroi a string de distribuicao para a URL de busca.

        Formato HotelDo:
        - "2" → 2 adultos, sem criancas
        - "2-7" → 2 adultos + 1 crianca de 7 anos
        - "2-7-10" → 2 adultos + 2 criancas (7 e 10 anos)
        - "2!3" → quarto 1 com 2 adultos, quarto 2 com 3 adultos

        Para multiplos quartos, cada quarto eh separado por "!"
        """
        ages = children_ages or []

        room_parts = []
        for _ in range(rooms):
            parts = [str(adults)]
            for age in ages:
                parts.append(str(age))
            room_parts.append("-".join(parts))

        return "!".join(room_parts)

    def _build_search_url(
        self,
        destination_id: str,
        checkin: date,
        checkout: date,
        adults: int = 2,
        children: int = 0,
        children_ages: list[int] | None = None,
        rooms: int = 1,
    ) -> str:
        """
        Constroi a URL de busca de hoteis.

        Padrao descoberto:
        /accommodations/results/{CIT_ID}/{checkin_YYYY-MM-DD}/{checkout_YYYY-MM-DD}/{distribution}
            ?from=SB2&facet=city&reSearch=true&searchId={uuid}
        """
        distribution = self._build_distribution(adults, children, children_ages, rooms)
        search_id = str(uuid.uuid4())

        path = (
            f"/accommodations/results/"
            f"{destination_id}/"
            f"{checkin.isoformat()}/"
            f"{checkout.isoformat()}/"
            f"{distribution}"
        )
        params = (
            f"?from=SB2&facet=city&reSearch=true&searchId={search_id}"
        )
        return f"{settings.hoteldo_base_url}{path}{params}"

    # -------------------------------------------------------------------------
    # Search Hotels
    # -------------------------------------------------------------------------

    async def search_hotels(
        self,
        destination_id: str,
        checkin: date,
        checkout: date,
        adults: int = 2,
        children: int = 0,
        children_ages: list[int] | None = None,
        rooms: int = 1,
    ) -> dict[str, Any]:
        """
        Busca hoteis no HotelDo para o destino e datas informados.

        Estrategia:
        1. Construir URL de busca direta (sem preencher formulario)
        2. Registrar interceptador na API de disponibilidade
        3. Navegar para a URL
        4. Aguardar resposta da API de disponibilidade
        5. Parsear dados dos hoteis do response JSON

        Args:
            destination_id: ID do destino (ex: "CIT_6624" para Santiago)
            checkin: Data de check-in
            checkout: Data de check-out
            adults: Numero de adultos por quarto
            children: Numero de criancas por quarto
            children_ages: Idades das criancas
            rooms: Numero de quartos

        Returns:
            Dict com resultados da busca incluindo lista de hoteis.
        """
        await self.ensure_authenticated()
        logger.info(
            "Buscando hoteis: destino=%s, checkin=%s, checkout=%s, adultos=%d",
            destination_id, checkin, checkout, adults,
        )

        search_url = self._build_search_url(
            destination_id, checkin, checkout, adults, children, children_ages, rooms
        )
        logger.info("URL de busca: %s", search_url)

        # Coletor de responses da API de disponibilidade
        availability_data: list[dict[str, Any]] = []
        capture_done = asyncio.Event()

        async def _on_response(response: Response) -> None:
            """Intercepta responses da API de disponibilidade."""
            url = response.url
            if "/s-accommodations/api/accommodations/availability" not in url:
                return
            if response.status != 200:
                logger.warning("Availability API retornou %d", response.status)
                return
            try:
                body = await response.json()
                availability_data.append(body)
                logger.info(
                    "Availability response capturada: %d items",
                    len(body.get("availability", body.get("items", body.get("results", [])))),
                )
                capture_done.set()
            except Exception as exc:
                logger.warning("Erro ao parsear availability response: %s", exc)

        # Registrar interceptador
        self._page.on("response", _on_response)

        try:
            # Navegar para URL de busca
            await self._page.goto(
                search_url,
                wait_until="domcontentloaded",
                timeout=settings.navigation_timeout_ms,
            )

            # Aguardar response da API de disponibilidade (ate 45s)
            try:
                await asyncio.wait_for(
                    capture_done.wait(),
                    timeout=SEARCH_RESULTS_TIMEOUT_MS / 1000,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout aguardando resultados de disponibilidade (%ds)",
                    SEARCH_RESULTS_TIMEOUT_MS // 1000,
                )
                # Tentar extrair do DOM como fallback
                availability_data.append(
                    await self._extract_hotels_from_dom()
                )

            # Parsear resultados
            hotels = self._parse_availability_results(availability_data)

            return {
                "destination_id": destination_id,
                "checkin": checkin.isoformat(),
                "checkout": checkout.isoformat(),
                "adults": adults,
                "children": children,
                "children_ages": children_ages or [],
                "rooms": rooms,
                "hotels": hotels,
                "hotels_found": len(hotels),
                "search_url": search_url,
                "status": "ok" if hotels else "no_results",
            }

        except Exception as exc:
            logger.error("Erro na busca de hoteis: %s", exc)
            raise HotelDoSearchError(f"Busca falhou: {exc}") from exc
        finally:
            # Remover interceptador
            self._page.remove_listener("response", _on_response)

    def _parse_availability_results(
        self, responses: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Parseia as responses da API de disponibilidade em lista de hoteis.

        Estrutura real (s-accommodations/api/accommodations/availability):
        {
          "availability": [
            {
              "accommodation": {id, name, stars, location{address, city{name}}, reviews{rating, total}, pictures[]},
              "room_pack_id": "...",
              "prices": {currency, price_for_delta (total), unit_price_for_delta (noite), main, ...},
              "agency": {commission, net_price, commission_percentage},
              "tracking_info": {"item": {meal_plan, cancellation_policy, supplier}},
              "detail_url": "..."
            }, ...
          ],
          "paging": {page, limit, total}
        }
        Tambem aceita o formato legado items/results (fallback do DOM).
        """
        hotels: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for response_data in responses:
            items = (
                response_data.get("availability")
                or response_data.get("items")
                or response_data.get("results")
                or response_data.get("hotels")
                or []
            )

            for item in items:
                hotel = self._normalize_hotel(item)
                if not hotel:
                    continue
                if not hotel["id"] or hotel["id"] in seen_ids:
                    continue
                seen_ids.add(hotel["id"])
                hotels.append(hotel)
                if len(hotels) >= MAX_HOTELS_TO_EXTRACT:
                    break

        # Ordenar por preco (menor primeiro); sem preco vai pro fim
        hotels.sort(key=lambda h: h.get("total_price") or float("inf"))
        return hotels

    @staticmethod
    def _to_float(v: Any) -> float:
        """Converte '2.900' / '4.988,50' / 2900 em float."""
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        s = s.replace("R$", "").replace(" ", "")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        else:
            # "2.900" eh milhar em pt-BR
            parts = s.split(".")
            if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
                s = "".join(parts)
        try:
            return float(s)
        except ValueError:
            return 0.0

    @staticmethod
    def _normalize_hotel(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Normaliza um item de availability (ou item legado) para formato padrao."""
        acc = raw.get("accommodation") or raw
        name = acc.get("name") or raw.get("hotel_name") or ""
        if not name:
            return None

        hotel_id = str(acc.get("id") or raw.get("id") or raw.get("hotel_id") or "")

        stars = acc.get("stars") or raw.get("stars") or 0
        try:
            stars = int(float(stars))
        except (ValueError, TypeError):
            stars = 0

        prices = raw.get("prices") or {}
        total_price = HotelDoClient._to_float(
            prices.get("price_for_delta") or prices.get("main") or raw.get("total_price")
        )
        nightly_price = HotelDoClient._to_float(
            prices.get("unit_price_for_delta")
            or (prices.get("unit") or {}).get("value")
            or raw.get("nightly_price")
        )
        currency = prices.get("currency") or raw.get("currency") or "BRL"
        price_text = prices.get("main_text") or ""
        discount = ""
        promo = prices.get("promotion") or {}
        if promo.get("items"):
            discount = str(promo["items"][0].get("value") or "").strip()

        loc = acc.get("location") or {}
        address = loc.get("address") or raw.get("address") or ""
        zone = (loc.get("zone") or {}).get("name") or ""
        city = (loc.get("city") or {}).get("name") or ""

        reviews = acc.get("reviews") or {}
        review_score = reviews.get("rating") or raw.get("review_score") or 0

        tinfo = (raw.get("tracking_info") or {}).get("item") or {}
        meal_plan = tinfo.get("meal_plan") or ""
        meal_plan = {
            "H": "Somente hospedagem",
            "B": "Cafe da manha incluso",
            "F": "Pensao completa",
            "A": "All inclusive",
            "M": "Meia pensao",
        }.get(meal_plan, meal_plan)
        cancellation = tinfo.get("cancellation_policy") or ""
        free_cancel = any(
            (li.get("id") == "options_with_free_cancellation")
            for li in (raw.get("list_items") or [])
            if isinstance(li, dict)
        )

        agency = raw.get("agency") or {}

        pics = acc.get("pictures") or []
        image = pics[0] if pics and isinstance(pics[0], str) else ""

        return {
            "id": hotel_id,
            "name": name,
            "stars": stars,
            "total_price": total_price,
            "nightly_price": nightly_price,
            "currency": currency,
            "price_text": price_text,
            "discount": discount,
            "board": meal_plan,
            "cancellation_policy": cancellation,
            "free_cancellation_available": free_cancel,
            "image": image,
            "room_pack_id": str(raw.get("room_pack_id") or ""),
            "address": address,
            "zone": zone,
            "city": city,
            "review_score": review_score,
            "review_count": reviews.get("total") or 0,
            "detail_url": raw.get("detail_url") or "",
            "agency_commission": HotelDoClient._to_float(agency.get("commission")),
            "agency_net_price": HotelDoClient._to_float(agency.get("net_price")),
            "commission_percentage": agency.get("commission_percentage"),
        }

    async def _extract_hotels_from_dom(self) -> dict[str, Any]:
        """
        Fallback: extrai dados de hoteis do DOM quando a interceptacao de API falha.
        Parseia os cards de resultado renderizados pelo Angular.
        """
        logger.info("Tentando extrair hoteis do DOM (fallback)...")
        try:
            hotels_data = await self._page.evaluate("""
                () => {
                    const cards = document.querySelectorAll(
                        '[class*="accommodation-card"], ' +
                        '[class*="hotel-card"], ' +
                        '[class*="cluster-card"], ' +
                        '[class*="result-card"]'
                    );
                    const results = [];
                    cards.forEach((card, i) => {
                        if (i >= 10) return;
                        const nameEl = card.querySelector(
                            '[class*="hotel-name"], h2, h3, [class*="title"]'
                        );
                        const priceEl = card.querySelector(
                            '[class*="price"], [class*="amount"]'
                        );
                        const starsEl = card.querySelector(
                            '[class*="stars"], [class*="category"], [class*="rating"]'
                        );
                        if (nameEl) {
                            results.push({
                                name: nameEl.textContent.trim(),
                                total_price: priceEl
                                    ? parseFloat(
                                        priceEl.textContent
                                            .replace(/[^0-9.,]/g, '')
                                            .replace('.', '')
                                            .replace(',', '.')
                                      ) || 0
                                    : 0,
                                stars: starsEl
                                    ? (starsEl.querySelectorAll(
                                        '[class*="star"]'
                                      ).length || 0)
                                    : 0,
                                currency: "BRL",
                            });
                        }
                    });
                    return {items: results};
                }
            """)
            return hotels_data
        except Exception as exc:
            logger.warning("Fallback DOM tambem falhou: %s", exc)
            return {"items": []}

    # -------------------------------------------------------------------------
    # Hotel Detail — abrir pagina de detalhe de um hotel
    # -------------------------------------------------------------------------

    async def _open_hotel_detail(
        self,
        hotel_id: str,
        destination_id: str,
        checkin: date,
        checkout: date,
        distribution: str,
        search_id: str,
        room_pack_id: str = "",
    ) -> bool:
        """
        Navega para a pagina de detalhe de um hotel.

        URL: /accommodations/detail/{hotel_id}/{checkin}/{checkout}/{distribution}
             ?user_searched_gid={CIT_ID}&selected_room_pack={room_pack_id}
             &searchId={search_id}&throughResults=true
        """
        path = (
            f"/accommodations/detail/{hotel_id}"
            f"/{checkin.isoformat()}/{checkout.isoformat()}/{distribution}"
        )
        params = (
            f"?user_searched_gid={destination_id}"
            f"&throughResults=true&fromViewMode=list&isMapCommanding=false"
            f"&searchId={search_id}&currency=BRL"
        )
        if room_pack_id:
            params += f"&selected_room_pack={room_pack_id}"

        url = f"{settings.hoteldo_base_url}{path}{params}"
        logger.info("Abrindo detalhe do hotel: %s", url)

        try:
            await self._page.goto(url, wait_until="domcontentloaded")
            await self._page.wait_for_timeout(3000)
            return True
        except Exception as exc:
            logger.error("Erro ao abrir detalhe do hotel: %s", exc)
            return False

    # -------------------------------------------------------------------------
    # Health check
    # -------------------------------------------------------------------------

    async def check_health(self) -> dict[str, Any]:
        """Verifica se o browser esta rodando e a sessao esta ativa."""
        browser_ok = self._browser is not None and self._browser.is_connected()
        session_ok = self._authenticated

        # Testar conectividade com a API de suggestions
        suggestions_ok = False
        try:
            results = await self.get_suggestions("Santiago")
            suggestions_ok = len(results) > 0
        except Exception:
            pass

        return {
            "browser_running": browser_ok,
            "session_authenticated": session_ok,
            "suggestions_api": suggestions_ok,
        }
