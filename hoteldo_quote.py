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
        details = await self._scrape_quotation_details(q["quote_url"])
        return {
            "product": "flight",
            "quote_id": trip_id,
            "quote_url": q["quote_url"],
            "search_url": url,
            "details": details,
            "flights": self._safe_parse_flights(search_data),
            "status": "created",
        }

    @classmethod
    def _safe_parse_flights(cls, search_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return cls._parse_flight_search(search_data)
        except Exception as exc:
            logger.warning("Resumo de voos falhou (link segue valido): %s", exc)
            return []

    @staticmethod
    def _parse_flight_search(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Resumo dos itinerarios. Estrutura real (flights-busquets/api/v1/web/search):
        {
          "items": [{"item": {
              "priceDetail": {"totalFare": {"amount": 4826}, "mainFare": {"amount": 1868}, "adultTotal": 2414},
              "airlines": ["L0"], "validatingCarrier": "JJ",
              "agencyDetail": {"commission": {"amount": 254}},
              "routeChoices": [{"routes": [{"stopsCount", "totalDuration", "departure", "arrival",
                                            "segments": [{"departure":{"airportCode","date"}, "arrival":{...},
                                                          "airlineCode", "flightId", "duration"}]}]}],
              "baggageV2": {"backpack", "carryOn", "checkedBagQuantity"}
          }}],
          "referenceData": {"airlines": {"L0": "LATAM", "G3": "Gol"}},
          "highlights": {"cheapestItinerary": {"priceTotal": {"amount"}}}
        }
        """
        out: list[dict[str, Any]] = []
        for data in responses:
            if not isinstance(data, dict):
                continue
            names = ((data.get("referenceData") or {}).get("airlines") or {})
            items = data.get("items") or []
            for wrap in items[:5]:
                try:
                    it = wrap.get("item") if isinstance(wrap, dict) else None
                    if not it:
                        continue
                    pd = it.get("priceDetail") or {}
                    total = (pd.get("totalFare") or {}).get("amount")
                    per_adult = pd.get("adultTotal") or (pd.get("mainFare") or {}).get("amount")
                    codes = it.get("airlines") or ([it.get("validatingCarrier")] if it.get("validatingCarrier") else [])
                    airlines = [str(names.get(c, c)) for c in codes if c]
                    legs = []
                    for rc in it.get("routeChoices") or []:
                        routes = rc.get("routes") or []
                        r = routes[rc.get("selectedRouteIndex") or 0] if routes else None
                        if not r:
                            continue
                        segs = r.get("segments") or []
                        legs.append({
                            "from": (segs[0].get("departure") or {}).get("airportCode") if segs else None,
                            "to": (segs[-1].get("arrival") or {}).get("airportCode") if segs else None,
                            "departure": (segs[0].get("departure") or {}).get("date") if segs else None,
                            "arrival": (segs[-1].get("arrival") or {}).get("date") if segs else None,
                            "stops": r.get("stopsCount"),
                            "duration": r.get("totalDuration"),
                            "flights": [sg.get("flightId") for sg in segs],
                        })
                    bag = it.get("baggageV2") or {}
                    out.append({
                        "total": total,
                        "per_adult": per_adult,
                        "currency": pd.get("currencyCode") or "BRL",
                        "airlines": airlines,
                        "legs": legs,
                        "checked_bags": bag.get("checkedBagQuantity"),
                        "carry_on": bag.get("carryOn"),
                        "agency_commission": ((it.get("agencyDetail") or {}).get("commission") or {}).get("amount"),
                    })
                except Exception as exc:  # resumo eh best-effort, nunca derruba a cotacao
                    logger.debug("parse flight item falhou: %s", exc)
            hl = (data.get("highlights") or {}).get("cheapestItinerary") or {}
            if hl:
                out.sort(key=lambda f: f.get("total") or float("inf"))
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

        _page0 = self._page
        _page0.on("response", _on_resp)
        summary: dict[str, Any] = {}
        hotels: list[dict[str, Any]] = []
        region_pick: Optional[dict[str, Any]] = None
        region_applied = False
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
            await self._wait_trip_id_in_url(90)
            summary = await self._scrape_package_summary()
            hotels = self._parse_availability_results(avail) if avail else []

            # REGRA DO LUCAS (04/11): em destinos com regiao preferida (Santiago:
            # Providencia / Las Condes / Centro), o hotel do pacote tem que cair
            # dentro do poligono. Se o recomendado pelo planner ja esta na regiao,
            # mantem; senao troca pelo melhor da regiao (clicando "Ver detalhe"
            # no card, o que muda a hospedagem do pacote).
            from regions import has_preferred_regions, is_excluded, pick_preferred_hotel, region_of
            if has_preferred_regions(dest_cit) and hotels:
                current_name = (summary.get("hotel_name") or "").strip().lower()
                current = next((h for h in hotels if h.get("name", "").strip().lower() == current_name), None)
                if (current and not is_excluded(dest_cit, current)
                        and region_of(dest_cit, current.get("latitude"), current.get("longitude"))):
                    current["region"] = region_of(dest_cit, current.get("latitude"), current.get("longitude"))
                    logger.info("Regiao: hotel recomendado '%s' ja esta em %s", current.get("name"), current["region"])
                    region_applied = True
                else:
                    region_pick = pick_preferred_hotel(dest_cit, hotels)
                    if region_pick:
                        logger.info("Regiao: trocando '%s' por '%s' (%s, R$ %s)",
                                    summary.get("hotel_name"), region_pick.get("name"),
                                    region_pick.get("region"), region_pick.get("total_price"))
                        region_applied = await self._select_hotel_card(region_pick["name"])
                        if region_applied:
                            summary = {**summary, **(await self._scrape_package_summary(detail_page=True))}
                            summary["hotel_name"] = region_pick["name"]
                    else:
                        logger.info("Regiao: nenhum hotel da lista cai na regiao preferida, mantendo recomendado")

            trip_id = await self._click_comprar_and_reach_checkout()
        finally:
            # a pagina pode ter sido trocada (popup do card): remove do page original
            try:
                _page0.remove_listener("response", _on_resp)
            except Exception:
                pass

        q = await self._generate_quotation(
            trip_id,
            client_name=client_data.get("name", ""),
            client_phone=client_data.get("phone", ""),
            client_email=client_data.get("email", ""),
        )
        # O hotel que esta NO pacote eh o "Hospedagem selecionada" do planner,
        # nao o mais barato da lista
        selected = None
        if summary.get("hotel_name"):
            for h in hotels:
                if h.get("name", "").strip().lower() == summary["hotel_name"].strip().lower():
                    selected = h
                    break
            if not selected:
                selected = {"name": summary["hotel_name"], "stars": summary.get("stars") or 0}
        details = await self._scrape_quotation_details(q["quote_url"])
        return {
            "product": "package",
            "quote_id": trip_id,
            "quote_url": q["quote_url"],
            "search_url": url,
            "hotels": hotels,
            "selected_hotel": selected,
            "summary": summary,
            "details": details,
            "region_applied": region_applied,
            "status": "created",
        }

    async def _select_hotel_card(self, hotel_name: str) -> bool:
        """
        Na lista de hospedagens do pacote, clica "Ver detalhe" do card do hotel
        (isso troca a hospedagem selecionada do pacote) e espera a pagina de
        detalhe dentro do fluxo (/trip/accommodations/detail/).
        """
        target = hotel_name.strip().lower()
        try:
            await self._page.wait_for_selector('button:has-text("Ver detalhe")', timeout=30000)
            # Lista pode ter lazy-load: rola ate o fim algumas vezes
            for _ in range(6):
                found = await self._page.evaluate(
                    """(name) => {
                        const btns = [...document.querySelectorAll('button')].filter(b => /Ver detalhe/i.test(b.innerText));
                        for (const b of btns) {
                            let c = b;
                            for (let k = 0; k < 8 && c; k++) { c = c.parentElement; if (c && c.innerText.length > 150) break; }
                            if (c && c.innerText.toLowerCase().includes(name)) { b.scrollIntoView({block:'center'}); return true; }
                        }
                        return false;
                    }""",
                    target,
                )
                if found:
                    break
                await self._page.evaluate("window.scrollBy(0, 2500)")
                await self._page.wait_for_timeout(1500)
            if not found:
                logger.info("Regiao: card de '%s' nao encontrado na lista", hotel_name)
                return False
            # O card pode abrir o detalhe em NOVA ABA (window.open). Captura popup;
            # se nao vier, assume navegacao na mesma pagina.
            click_js = """(name) => {
                const btns = [...document.querySelectorAll('button')].filter(b => /Ver detalhe/i.test(b.innerText));
                for (const b of btns) {
                    let c = b;
                    for (let k = 0; k < 8 && c; k++) { c = c.parentElement; if (c && c.innerText.length > 150) break; }
                    if (c && c.innerText.toLowerCase().includes(name)) { b.click(); return true; }
                }
                return false;
            }"""
            # Clique REAL (mouse) no botao do card: acha o elemento com o nome do hotel e
            # sobe ate o ancestral mais proximo que tem um "Ver detalhe".
            safe_name = hotel_name.strip().replace('"', "")
            xpath = (
                f'xpath=(//*[contains(normalize-space(.), "{safe_name}") and not(self::body) and not(self::html)]'
                f'/ancestor-or-self::*[.//button[contains(., "Ver detalhe")]][1]//button[contains(., "Ver detalhe")])[1]'
            )
            new_page = None
            clicked = False
            try:
                loc = self._page.locator(xpath)
                if await loc.count():
                    await loc.first.scroll_into_view_if_needed(timeout=5000)
                    try:
                        async with self._page.expect_popup(timeout=8000) as pop:
                            await loc.first.click(timeout=5000)
                        new_page = await pop.value
                    except Exception:
                        new_page = None
                    clicked = True
            except Exception as exc:
                logger.info("Regiao: clique real falhou (%s), tentando via JS", exc)
            if not clicked:
                try:
                    async with self._page.expect_popup(timeout=8000) as pop:
                        await self._page.evaluate(click_js, target)
                    new_page = await pop.value
                except Exception:
                    new_page = None
            if new_page is not None:
                old_page = self._page
                self._page = new_page
                try:
                    await old_page.close()
                except Exception:
                    pass
                logger.info("Regiao: detalhe abriu em nova aba")
            for _ in range(40):
                await self._page.wait_for_timeout(1000)
                if "/trip/accommodations/detail/" in self._page.url:
                    break
            if "/trip/accommodations/detail/" not in self._page.url:
                logger.info("Regiao: nao chegou na pagina de detalhe (url=%s)", self._page.url[:120])
                return False
            await self._page.wait_for_selector('a:has-text("Comprar"), button:has-text("Comprar")', timeout=60000)
            await self._page.wait_for_timeout(1500)
            ok = await self._page.evaluate(
                "(name) => (document.body.innerText || '').toLowerCase().includes(name)", target
            )
            logger.info("Regiao: detalhe de '%s' aberto (%s)", hotel_name, self._page.url[:100])
            return bool(ok)
        except Exception as exc:
            logger.warning("Regiao: falha ao selecionar '%s': %s", hotel_name, exc)
            return False

    async def _scrape_quotation_details(self, quote_url: str) -> dict[str, Any]:
        """
        Abre a pagina PUBLICA da cotacao, expande os "Ver detalhe" e le cia aerea,
        numero do voo, aeroportos (codigo + cidade + nome) e bagagem de cada trecho.
        Best-effort: qualquer falha devolve {}.
        """
        try:
            await self._page.goto(quote_url, wait_until="domcontentloaded")
            await self._page.wait_for_selector('button:has-text("Ver detalhe")', timeout=30000)
            for _ in range(6):
                btns = self._page.locator('button:has-text("Ver detalhe")')
                n = await btns.count()
                if not n:
                    break
                try:
                    await btns.first.click(timeout=5000)
                except Exception:
                    break
                await self._page.wait_for_timeout(600)
            txt = await self._page.evaluate("() => document.body.innerText || ''")
            return self._parse_quotation_text(txt)
        except Exception as exc:
            logger.warning("Nao consegui ler detalhes da cotacao publica: %s", exc)
            return {}

    @staticmethod
    def _parse_quotation_text(txt: str) -> dict[str, Any]:
        import re as _re

        out: dict[str, Any] = {"flights": []}
        m = _re.search(r"R\$\s*\n?\s*([\d\.]+)", txt)
        if m:
            out["total_text"] = m.group(1)

        def _section(start_label: str, end_labels: list[str]) -> str:
            i = txt.find(start_label)
            if i < 0:
                return ""
            end = len(txt)
            for e in end_labels:
                j = txt.find(e, i + len(start_label))
                if 0 <= j < end:
                    end = j
            return txt[i:end]

        for label, key in (("\nIda\n", "out"), ("\nVolta\n", "back")):
            sec = _section(label, ["\nVolta\n", "\nAcomoda", "Valores referentes"]) if key == "out" \
                else _section(label, ["\nAcomoda", "Valores referentes"])
            if not sec:
                continue
            f: dict[str, Any] = {"leg": key}
            mm = _re.search(r"([A-Z]{3})\n(\d\d:\d\d)\n([A-Z]{3})\n(\d\d:\d\d)\n([^\n]+)\n(\d+ h \d+ m)", sec)
            if mm:
                f.update(from_code=mm.group(1), dep=mm.group(2), to_code=mm.group(3), arr=mm.group(4),
                         stops=mm.group(5).strip(), duration=mm.group(6))
            mm = _re.search(r"Ocultar detalhe\n([^\n]+)\nVoo N[°º]\s*([A-Z0-9]+)", sec)
            if mm:
                f["airline"] = mm.group(1).strip()
                f["flight_number"] = mm.group(2).strip()
            mm = _re.search(r"Classe:\s*([^\n]+)", sec)
            if mm:
                f["cabin"] = mm.group(1).strip()
            # GRU \n Sao Paulo \n Duracao \n 4 h 29 m \n SCL \n Santiago \n Aeroporto X \n Aeroporto Y
            mm = _re.search(
                r"([A-Z]{3})\n([^\n]+)\nDura[cç][aã]o\n[^\n]+\n([A-Z]{3})\n([^\n]+)\n([^\n]*Aeroporto[^\n]*)\n([^\n]*Aeroporto[^\n]*)",
                sec,
            )
            if mm:
                f.update(from_city=mm.group(2).strip(), to_city=mm.group(4).strip(),
                         from_airport=mm.group(5).strip(), to_airport=mm.group(6).strip())
            else:
                mm = _re.search(r"([A-Z]{3})\n([^\n]+)\nDura[cç][aã]o\n[^\n]+\n([A-Z]{3})\n([^\n]+)", sec)
                if mm:
                    f.update(from_city=mm.group(2).strip(), to_city=mm.group(4).strip())
            bi = sec.find("Bagagem")
            if bi >= 0:
                bsec = sec[bi:bi + 600]
                lines = [l.strip() for l in bsec.split("\n") if l.strip()]
                f["baggage_lines"] = lines[1:8]
                low = bsec.lower()
                f["baggage"] = {
                    "backpack": "inclui uma mochila" in low or "inclui mochila" in low,
                    "hand": ("inclui bagagem de m" in low) and ("não inclui bagagem de m" not in low and "nao inclui bagagem de m" not in low),
                    "checked": ("inclui bagagem para despachar" in low or "inclui bagagem despach" in low)
                               and ("não inclui bagagem para despachar" not in low and "nao inclui bagagem para despachar" not in low),
                }
            out["flights"].append(f)
        hi = txt.find("Acomoda")
        if hi >= 0:
            hsec = txt[hi:hi + 400].split("\n")
            hsec = [l.strip() for l in hsec if l.strip()]
            if len(hsec) >= 3:
                out["hotel_name"] = hsec[1]
                out["hotel_address"] = hsec[2]
        return out

    async def _scrape_package_summary(self, detail_page: bool = False) -> dict[str, Any]:
        """Le do planner o hotel selecionado e o preco final do pacote (best-effort)."""
        try:
            await self._page.wait_for_selector('text=/Comprar/', timeout=60000)
            await self._page.wait_for_timeout(1500)
            data = await self._page.evaluate(r"""
                () => {
                    const txt = document.body.innerText || '';
                    const out = {};
                    let m = txt.match(/Hospedagem selecionada\s*\n\s*([^\n]+)/);
                    if (!m) m = txt.match(/Hospedagem\s*\n\s*Alterar hospedagem\s*\n\s*([^\n]+)/);
                    if (m) out.hotel_name = m[1].trim();
                    m = txt.match(/Pre[cç]o final por pessoa\s*\n?\s*(?:\(i\)\s*)?R\$\s*([\d\.]+)/);
                    if (m) out.price_per_person = m[1];
                    m = txt.match(/Final\s+(\d+)\s+pessoas?\s*R\$\s*([\d\.]+)/);
                    if (m) { out.pax = parseInt(m[1]); out.price_total = m[2]; }
                    m = txt.match(/Seu ganho\s*R\$\s*([\d\.]+)/);
                    if (m) out.agency_revenue = m[1];
                    m = txt.match(/Alterar voo\s*\n([A-Z]{3})\n(\d\d:\d\d)\n([^\n]+)\n([A-Z]{3})\n(\d\d:\d\d)\n([A-Z]{3})\n(\d\d:\d\d)\n([^\n]+)\n([A-Z]{3})\n(\d\d:\d\d)/);
                    if (m) {
                        out.flight_out = {from: m[1], dep: m[2], stops: m[3], to: m[4], arr: m[5]};
                        out.flight_back = {from: m[6], dep: m[7], stops: m[8], to: m[9], arr: m[10]};
                    }
                    out.snippet = txt.slice(0, 600);
                    return out;
                }
            """)
            for k in ("price_per_person", "price_total", "agency_revenue"):
                if data.get(k):
                    data[k] = float(str(data[k]).replace(".", ""))
            logger.info("Resumo do pacote: %s", {k: v for k, v in data.items() if k != "snippet"})
            return data
        except Exception as exc:
            logger.warning("Nao consegui ler resumo do pacote: %s", exc)
            return {}
