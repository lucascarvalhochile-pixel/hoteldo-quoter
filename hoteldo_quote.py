"""
Geracao de cotacao compartilhavel no HotelDo (mixin do HotelDoClient).

Fluxo real descoberto em 04/11/2026 (Chrome, conta Camila):
  1. Pagina de detalhe do hotel -> botao "Reservar"/"Adicionar"
     -> redireciona pra /trips/xs/{PR...} e depois /checkout/{PR...}/form
     O {PR...} eh o trip id E o id da cotacao.
  2. Em /checkout/{PR}/form existe o botao "Gerar cotacao" (modo agencia).
  3. Modal "Gerar cotacao": nome do cliente, telefone (so digitos), e-mails.
     Botao "Gerar" -> POST /checkout/{PR}/redirect-quotation (payload montado
     pelo front) -> {"topicName":"channels-checkout-quote","correctness":"ok"}
     e abre /quotations/{PR} numa aba nova.
  4. /quotations/{PR} eh PUBLICO (sem login) so depois do passo 3.

O mesmo fluxo vale pra voo, hotel ou pacote (o checkout cota o que estiver
no trip). O carrinho (/cart) eh compartilhado com o usuario humano da conta,
por isso usamos o fluxo "Reservar" direto do detalhe, que cria um trip
isolado em vez de mexer no carrinho.
"""

import asyncio
import logging
import re
import uuid
from datetime import date
from typing import Any, Optional

from config import settings

logger = logging.getLogger("hoteldo_quoter.quote")

TRIP_ID_RE = re.compile(r"(PR[a-f0-9]{30,})")


class HotelDoQuoteError(Exception):
    """Falha na criacao de cotacao."""


class QuoteMixin:
    """Espera self._page, self._build_distribution, self._open_hotel_detail, ensure_authenticated."""

    # -------------------------------------------------------------------------
    # Passo 1: detalhe -> Reservar -> trip id
    # -------------------------------------------------------------------------

    async def _start_checkout_from_detail(self) -> str:
        """Clica em Reservar/Adicionar na pagina de detalhe e devolve o trip id (PR...)."""
        add_sel = (
            'button:has-text("Reservar"), '
            'a:has-text("Reservar"), '
            'button:has-text("Adicionar"), '
            'a:has-text("Adicionar"), '
            'button:has-text("Selecionar"), '
            '[class*="add-to-cart"], [class*="book-button"]'
        )
        await self._page.wait_for_selector(add_sel, state="visible", timeout=20000)
        await self._page.locator(add_sel).first.click()

        # Esperar a URL conter um PR... (trips/xs/PR ou checkout/PR)
        for _ in range(40):
            await self._page.wait_for_timeout(1000)
            m = TRIP_ID_RE.search(self._page.url)
            if m:
                logger.info("Trip criado: %s (URL: %s)", m.group(1), self._page.url[:120])
                return m.group(1)
        raise HotelDoQuoteError(
            f"Nao achei trip id apos Reservar (URL: {self._page.url[:200]})"
        )

    # -------------------------------------------------------------------------
    # Passo 2/3: checkout -> Gerar cotacao -> modal -> Gerar
    # -------------------------------------------------------------------------

    async def _generate_quotation(
        self,
        trip_id: str,
        client_name: str,
        client_phone: str,
        client_email: str,
    ) -> dict[str, Any]:
        """Abre /checkout/{PR}/form, usa o modal 'Gerar cotacao' e devolve a URL publica."""
        checkout_url = f"{settings.hoteldo_base_url}/checkout/{trip_id}/form"
        quote_url = f"{settings.hoteldo_base_url}/quotations/{trip_id}"

        if not TRIP_ID_RE.fullmatch(trip_id):
            raise HotelDoQuoteError(f"trip id invalido: {trip_id}")

        phone_digits = re.sub(r"\D", "", client_phone or "") or settings.quote_default_phone
        email = (client_email or "").strip() or settings.quote_default_email
        name = (client_name or "").strip() or "Cliente LC Viagens"

        logger.info("Abrindo checkout %s", checkout_url)
        if trip_id not in self._page.url or "/form" not in self._page.url:
            await self._page.goto(checkout_url, wait_until="domcontentloaded")

        # O checkout demora (loader "Carregando..." ~20-40s)
        gerar_sel = 'button:has-text("Gerar cotação"), button:has-text("Gerar cotacao")'
        await self._page.wait_for_selector(gerar_sel, state="visible", timeout=90000)
        await self._page.locator(gerar_sel).first.click()

        # Modal
        name_sel = 'input[placeholder*="nome e sobrenome"]'
        phone_sel = 'input[placeholder*="telefone"]'
        email_sel = 'input[placeholder*="e-mail"]'
        await self._page.wait_for_selector(name_sel, state="visible", timeout=20000)
        await self._page.locator(name_sel).first.fill(name)
        await self._page.locator(phone_sel).first.fill(phone_digits)
        await self._page.locator(email_sel).first.fill(email)

        # Capturar resposta do redirect-quotation
        result: dict[str, Any] = {}
        done = asyncio.Event()

        async def _on_resp(resp):
            if "redirect-quotation" not in resp.url:
                return
            try:
                body = await resp.json()
            except Exception:
                body = {"raw": (await resp.text())[:300]}
            result["status"] = resp.status
            result["body"] = body
            done.set()

        self._page.on("response", _on_resp)
        # O "Gerar" abre a cotacao numa aba nova; fechamos pra nao acumular
        popup_task = asyncio.ensure_future(self._page.context.wait_for_event("page", timeout=30000))
        try:
            gerar_btn = self._page.locator(
                '[class*="modal"] button:has-text("Gerar"), '
                'button:has-text("Gerar"):not(:has-text("cotação")):not(:has-text("cotacao"))'
            ).last
            await gerar_btn.click()
            try:
                await asyncio.wait_for(done.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("Timeout esperando redirect-quotation")
        finally:
            self._page.remove_listener("response", _on_resp)
            try:
                popup = await asyncio.wait_for(popup_task, timeout=5)
                await popup.close()
            except Exception:
                popup_task.cancel()

        ok = result.get("status") == 200 and str(
            (result.get("body") or {}).get("correctness", "")
        ).lower() == "ok"
        logger.info("redirect-quotation: %s", result)
        if not ok:
            raise HotelDoQuoteError(f"Gerar cotacao falhou: {result}")

        return {"quote_id": trip_id, "quote_url": quote_url, "api": result}

    # -------------------------------------------------------------------------
    # create_quote (hotel)
    # -------------------------------------------------------------------------

    async def create_quote(
        self,
        hotel_selection: dict[str, Any],
        client_data: dict[str, Any],
        destination_id: str = "",
        checkin: date | None = None,
        checkout: date | None = None,
        adults: int = 2,
        children: int = 0,
        children_ages: list[int] | None = None,
        rooms: int = 1,
    ) -> dict[str, Any]:
        """Cria cotacao compartilhavel de HOTEL e devolve quote_url publica."""
        await self.ensure_authenticated()
        hotel_name = hotel_selection.get("name", "?")
        hotel_id = hotel_selection.get("id", "")
        room_pack_id = hotel_selection.get("room_pack_id", "")
        if not hotel_id:
            raise HotelDoQuoteError("hotel_selection deve conter 'id'")
        if not checkin or not checkout:
            raise HotelDoQuoteError("checkin e checkout sao obrigatorios")

        logger.info("Criando cotacao de hotel: %s (%s)", hotel_name, hotel_id)
        distribution = self._build_distribution(adults, children, children_ages, rooms)
        search_id = str(uuid.uuid4())

        ok = await self._open_hotel_detail(
            hotel_id=hotel_id,
            destination_id=destination_id,
            checkin=checkin,
            checkout=checkout,
            distribution=distribution,
            search_id=search_id,
            room_pack_id=room_pack_id,
        )
        if not ok:
            raise HotelDoQuoteError(f"Nao consegui abrir detalhe do hotel {hotel_id}")

        trip_id = await self._start_checkout_from_detail()
        q = await self._generate_quotation(
            trip_id,
            client_name=client_data.get("name", ""),
            client_phone=client_data.get("phone", ""),
            client_email=client_data.get("email", ""),
        )

        return {
            "quote_id": trip_id,
            "quote_url": q["quote_url"],
            "hotel_name": hotel_name,
            "hotel_id": hotel_id,
            "total_price": hotel_selection.get("total_price", 0),
            "currency": hotel_selection.get("currency", "BRL"),
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "status": "created",
        }

    # -------------------------------------------------------------------------
    # Helpers comuns: esperar PR na URL, clicar Comprar, modal de bagagem
    # -------------------------------------------------------------------------

    async def _wait_trip_id_in_url(self, timeout_s: int = 60) -> str:
        for _ in range(timeout_s):
            m = TRIP_ID_RE.search(self._page.url)
            if m:
                return m.group(1)
            await self._page.wait_for_timeout(1000)
        raise HotelDoQuoteError(f"Sem trip id na URL apos {timeout_s}s: {self._page.url[:200]}")

    async def _click_comprar_and_reach_checkout(self, sort_cheapest: bool = False) -> str:
        """Clica no primeiro 'Comprar', trata modal de bagagem, espera /checkout/{PR}/form."""
        comprar_sel = 'a:has-text("Comprar"), button:has-text("Comprar")'
        await self._page.wait_for_selector(comprar_sel, state="visible", timeout=90000)

        if sort_cheapest:
            try:
                tab = self._page.locator('text=/Mais baratos/i').first
                if await tab.count():
                    await tab.click(timeout=5000)
                    await self._page.wait_for_timeout(3000)
            except Exception as exc:
                logger.info("Nao consegui ordenar por mais baratos: %s", exc)

        await self._page.locator(comprar_sel).first.click()

        # Modal de bagagem (voos): "Continuar" mantem a tarifa Basic
        for _ in range(20):
            await self._page.wait_for_timeout(1000)
            if "/checkout/" in self._page.url:
                break
            cont = self._page.locator('button:has-text("Continuar")')
            try:
                if await cont.count() and await cont.first.is_visible():
                    await cont.first.click()
                    logger.info("Modal de bagagem: Continuar")
            except Exception:
                pass

        trip_id = await self._wait_trip_id_in_url(90)
        # Garantir que estamos no /form
        for _ in range(60):
            if "/form" in self._page.url:
                break
            await self._page.wait_for_timeout(1000)
        logger.info("Checkout alcancado: %s", self._page.url[:140])
        return trip_id

    # -------------------------------------------------------------------------
    # VOO
    # -------------------------------------------------------------------------

    def _build_flight_url(
        self, origin: str, destination: str, departure: date, return_date: Optional[date],
        adults: int = 2, children: int = 0, infants: int = 0,
    ) -> str:
        kind = "roundtrip" if return_date else "oneway"
        path = f"/shop/flights/results/{kind}/{origin.upper()}/{destination.upper()}/{departure.isoformat()}"
        if return_date:
            path += f"/{return_date.isoformat()}"
        path += f"/{adults}/{children}/{infants}"
        return f"{settings.hoteldo_base_url}{path}"

    async def create_flight_quote(
        self,
        origin: str,
        destination: str,
        departure: date,
        return_date: Optional[date],
        client_data: dict[str, Any],
        adults: int = 2,
        children: int = 0,
        infants: int = 0,
    ) -> dict[str, Any]:
        """Busca voos, escolhe o mais barato e gera cotacao compartilhavel."""
        await self.ensure_authenticated()
        url = self._build_flight_url(origin, destination, departure, return_date, adults, children, infants)
        logger.info("Busca de voos: %s", url)

        search_data: list[dict[str, Any]] = []

        async def _on_resp(resp):
            if "flights-busquets/api/v1/web/search" in resp.url and resp.status == 200:
                try:
                    search_data.append(await resp.json())
                except Exception:
                    pass

        self._page.on("response", _on_resp)
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
            trip_id = await self._click_comprar_and_reach_checkout(sort_cheapest=True)
        finally:
            self._page.remove_listener("response", _on_resp)

        q = await self._generate_quotation(
            trip_id,
            client_name=client_data.get("name", ""),
            client_phone=client_data.get("phone", ""),
            client_email=client_data.get("email", ""),
        )
        return {
            "product": "flight",
            "quote_id": trip_id,
            "quote_url": q["quote_url"],
            "search_url": url,
            "flights": self._parse_flight_search(search_data),
            "status": "created",
        }

    @staticmethod
    def _parse_flight_search(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Resumo dos itinerarios (melhor esforco; estrutura Despegar 'clusters')."""
        out: list[dict[str, Any]] = []
        for data in responses:
            clusters = (
                data.get("clusters")
                or (data.get("items") or {}).get("clusters")
                or (data.get("data") or {}).get("clusters")
                or []
            )
            for c in clusters[:5]:
                price = c.get("price") or {}
                segs = c.get("segments") or []
                airlines = set()
                for s in segs:
                    for ch in (s.get("choices") or [])[:1]:
                        for leg in ch.get("legs") or ch.get("segments") or []:
                            a = (leg.get("airline") or {}).get("name") if isinstance(leg.get("airline"), dict) else leg.get("airline")
                            if a:
                                airlines.add(str(a))
                out.append({
                    "total": price.get("total") or price.get("amount") or price.get("final_price"),
                    "per_adult": price.get("adult") or price.get("per_adult"),
                    "currency": price.get("currency") or "BRL",
                    "airlines": sorted(airlines),
                    "raw_keys": list(c.keys())[:15],
                })
        return out

    # -------------------------------------------------------------------------
    # PACOTE (voo + hotel) — trip planner FH
    # -------------------------------------------------------------------------

    def _build_package_url(
        self, origin_cit: str, dest_cit: str, departure: date, return_date: date,
        checkin: date, checkout: date, distribution: str,
    ) -> str:
        path = (
            f"/trip/start/FH/{origin_cit}/{dest_cit}/{departure.isoformat()}/{return_date.isoformat()}"
            f"/{dest_cit}/{checkin.isoformat()}/{checkout.isoformat()}/{distribution}"
        )
        return f"{settings.hoteldo_base_url}{path}?from=PSB&nw=true&reSearch=true"

    async def create_package_quote(
        self,
        origin_cit: str,
        dest_cit: str,
        departure: date,
        return_date: date,
        client_data: dict[str, Any],
        checkin: Optional[date] = None,
        checkout: Optional[date] = None,
        adults: int = 2,
        children: int = 0,
        children_ages: list[int] | None = None,
        rooms: int = 1,
    ) -> dict[str, Any]:
        """Pacote voo+hotel: trip/start/FH -> resultados (hotel recomendado) -> Comprar -> cotacao."""
        await self.ensure_authenticated()
        checkin = checkin or departure
        checkout = checkout or return_date
        distribution = self._build_distribution(adults, children, children_ages, rooms)
        url = self._build_package_url(origin_cit, dest_cit, departure, return_date, checkin, checkout, distribution)
        logger.info("Busca de pacote: %s", url)

        avail: list[dict[str, Any]] = []

        async def _on_resp(resp):
            if "/availability" in resp.url and resp.status == 200:
                try:
                    avail.append(await resp.json())
                except Exception:
                    pass

        self._page.on("response", _on_resp)
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
            await self._wait_trip_id_in_url(90)
            trip_id = await self._click_comprar_and_reach_checkout()
        finally:
            self._page.remove_listener("response", _on_resp)

        q = await self._generate_quotation(
            trip_id,
            client_name=client_data.get("name", ""),
            client_phone=client_data.get("phone", ""),
            client_email=client_data.get("email", ""),
        )
        hotels = self._parse_availability_results(avail) if avail else []
        return {
            "product": "package",
            "quote_id": trip_id,
            "quote_url": q["quote_url"],
            "search_url": url,
            "hotels": hotels,
            "status": "created",
        }
