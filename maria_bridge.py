"""
Ponte de integracao entre o bot MARIA e o hoteldo-quoter.

A MARIA qualifica leads no WhatsApp via Zenvia Conversion e coleta:
- Destino desejado (ex: "Santiago", "Buenos Aires")
- Datas de viagem (checkin/checkout)
- Numero de passageiros (adultos + criancas + idades)
- Aeroporto de origem (para voos, nao usado aqui)

Este modulo recebe esses dados, mapeia para IDs do HotelDo,
executa a busca/cotacao, e retorna mensagem formatada para a MARIA
enviar ao cliente no WhatsApp.

Fluxo completo (pos-Etapa 1):
1. MARIA coleta dados do cliente
2. MariaBridge.resolve_destination() -> CIT_ID
3. HotelDoClient.search_hotels() -> lista de hoteis (API interceptada)
4. MariaBridge seleciona top 3 hoteis
5. HotelDoClient.create_quote() -> tripId + URL compartilhavel
6. MariaBridge formata msg WhatsApp com link da cotacao
7. MARIA envia ao cliente -> cliente clica no link -> ve pagina bonita
8. Cliente quer fechar -> MARIA transfere para "setor de fechamentos"
"""

import logging
from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from hoteldo_client import HotelDoClient

logger = logging.getLogger("hoteldo_quoter.maria_bridge")


# ---------------------------------------------------------------------------
# Mapeamento de destinos para IDs do HotelDo
# ---------------------------------------------------------------------------

# IDs confirmados via Suggestions API e navegacao manual:
# GET /suggestions?locale=pt_BR&profile=sbox-hotels-facet&hint={nome}
DESTINATION_MAP: dict[str, str] = {
    # --- Chile ---
    "santiago": "CIT_6624",
    "santiago do chile": "CIT_6624",
    "santiago de chile": "CIT_6624",
    "chile": "CIT_6624",
    "san pedro de atacama": "CIT_394",
    "atacama": "CIT_394",

    # --- Argentina ---
    "buenos aires": "CIT_982",
    "bariloche": "CIT_901",
    "san carlos de bariloche": "CIT_901",
    "mendoza": "CIT_4451",
    "argentina": "CIT_982",

    # --- Peru ---
    "lima": "CIT_4088",
    "cusco": "CIT_1579",
    "cuzco": "CIT_1579",
    "peru": "CIT_4088",

    # --- Colombia ---
    "bogota": "CIT_854",
    "bogota": "CIT_854",
    "cartagena": "CIT_1535",
    "cartagena de indias": "CIT_1535",
    "san andres": "CIT_6350",
    "san andres": "CIT_6350",
    "colombia": "CIT_854",
    "colombia": "CIT_854",

    # --- Mexico ---
    "cancun": "CIT_1569",
    "cancun": "CIT_1569",
    "cidade do mexico": "CIT_4475",
    "mexico": "CIT_1569",
    "mexico": "CIT_1569",

    # --- Caribe ---
    "punta cana": "CIT_5963",
    "republica dominicana": "CIT_5963",
    "republica dominicana": "CIT_5963",

    # --- EUA ---
    "orlando": "CIT_5419",
    "miami": "CIT_4625",

    # --- Uruguai ---
    "montevideo": "CIT_4685",
    "montevideu": "CIT_4685",
    "montevideu": "CIT_4685",
}

# Nomes amigaveis para destinos (para mensagens WhatsApp)
DESTINATION_NAMES: dict[str, str] = {
    "CIT_6624": "Santiago",
    "CIT_394": "Atacama",
    "CIT_982": "Buenos Aires",
    "CIT_901": "Bariloche",
    "CIT_4451": "Mendoza",
    "CIT_4088": "Lima",
    "CIT_1579": "Cusco",
    "CIT_854": "Bogota",
    "CIT_1535": "Cartagena",
    "CIT_6350": "San Andres",
    "CIT_1569": "Cancun",
    "CIT_4475": "Cidade do Mexico",
    "CIT_5963": "Punta Cana",
    "CIT_5419": "Orlando",
    "CIT_4625": "Miami",
    "CIT_4685": "Montevideu",
}


# ---------------------------------------------------------------------------
# Modelos de dados
# ---------------------------------------------------------------------------

class MariaQuoteRequest(BaseModel):
    """Dados que a MARIA envia para solicitar uma cotacao de hotel."""

    destino: str = Field(..., description="Nome do destino (ex: Santiago, Buenos Aires)")
    checkin: str = Field(..., description="Data de checkin no formato YYYY-MM-DD ou DD/MM/YYYY")
    checkout: str = Field(..., description="Data de checkout no formato YYYY-MM-DD ou DD/MM/YYYY")
    adults: int = Field(default=2, ge=1, le=10, description="Numero de adultos")
    children: int = Field(default=0, ge=0, le=6, description="Numero de criancas")
    children_ages: list[int] = Field(default_factory=list, description="Idades das criancas")
    rooms: int = Field(default=1, ge=1, le=5, description="Numero de quartos")

    # Dados do cliente (para cotacao no Socrates)
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None

    # Metadata da MARIA
    lead_id: Optional[str] = None
    conversation_id: Optional[str] = None

    @field_validator("checkin", "checkout")
    @classmethod
    def parse_date_string(cls, v: str) -> str:
        """Aceita datas em DD/MM/YYYY ou YYYY-MM-DD e normaliza para YYYY-MM-DD."""
        if "/" in v:
            try:
                parsed = datetime.strptime(v, "%d/%m/%Y")
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            raise ValueError(f"Data invalida: {v}. Use YYYY-MM-DD ou DD/MM/YYYY")


class MariaQuoteResponse(BaseModel):
    """Resposta formatada para a MARIA enviar ao cliente."""

    success: bool
    message: str  # Mensagem formatada para WhatsApp
    quote_url: Optional[str] = None
    hotels_found: int = 0
    destination_id: Optional[str] = None
    destination_name: Optional[str] = None
    error: Optional[str] = None
    raw_results: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class MariaBridge:
    """
    Camada de integracao MARIA -> HotelDo.

    Responsabilidades:
    1. Receber dados de qualificacao da MARIA
    2. Mapear destinos para IDs do HotelDo
    3. Executar busca de hoteis via HotelDoClient
    4. Criar cotacao compartilhavel com link publico
    5. Formatar resposta para envio no WhatsApp
    """

    def __init__(self, client: HotelDoClient) -> None:
        self._client = client

    async def resolve_destination(self, destino: str) -> Optional[str]:
        """
        Resolve o nome de um destino para seu ID no HotelDo.

        Estrategia:
        1. Mapeamento local (DESTINATION_MAP)
        2. Suggestions API do HotelDo (fallback)
        3. Cacheia resultados da API no mapa
        """
        normalized = destino.strip().lower()

        # 1. Mapeamento local
        destination_id = DESTINATION_MAP.get(normalized)
        if destination_id:
            logger.info("Destino '%s' resolvido via mapa: %s", destino, destination_id)
            return destination_id

        # 2. Suggestions API
        logger.info("Destino '%s' nao encontrado no mapa, consultando API...", destino)
        suggestions = await self._client.get_suggestions(destino)

        for suggestion in suggestions:
            if isinstance(suggestion, dict) and suggestion.get("type") == "CITY":
                city_id = suggestion.get("id", "")
                if city_id:
                    DESTINATION_MAP[normalized] = city_id
                    logger.info(
                        "Destino '%s' resolvido via API: %s (cacheado)",
                        destino, city_id,
                    )
                    return city_id

        logger.warning("Destino '%s' nao encontrado em nenhuma fonte", destino)
        return None

    async def generate_quote(self, request: MariaQuoteRequest) -> MariaQuoteResponse:
        """
        Gera uma cotacao de hotel a partir dos dados da MARIA.

        Fluxo completo:
        1. Resolver destino -> CIT_ID
        2. Buscar hoteis (Playwright intercepta API de disponibilidade)
        3. Selecionar melhor hotel
        4. Criar cotacao no HotelDo (carrinho -> Socrates)
        5. Obter URL compartilhavel (/quotations/{tripId})
        6. Formatar mensagem WhatsApp com link
        """
        logger.info(
            "Gerando cotacao: destino=%s, checkin=%s, checkout=%s, adultos=%d",
            request.destino, request.checkin, request.checkout, request.adults,
        )

        # 1. Resolver destino
        destination_id = await self.resolve_destination(request.destino)
        if not destination_id:
            return MariaQuoteResponse(
                success=False,
                message=self._format_destination_not_found(request.destino),
                error=f"Destino nao encontrado: {request.destino}",
            )

        dest_name = DESTINATION_NAMES.get(destination_id, request.destino)

        # 2. Buscar hoteis
        try:
            checkin = date.fromisoformat(request.checkin)
            checkout = date.fromisoformat(request.checkout)

            results = await self._client.search_hotels(
                destination_id=destination_id,
                checkin=checkin,
                checkout=checkout,
                adults=request.adults,
                children=request.children,
                children_ages=request.children_ages,
                rooms=request.rooms,
            )
        except Exception as exc:
            logger.error("Erro na busca de hoteis: %s", exc)
            return MariaQuoteResponse(
                success=False,
                message=self._format_search_error(),
                error=str(exc),
                destination_id=destination_id,
                destination_name=dest_name,
            )

        hotels = results.get("hotels", [])
        if not hotels:
            return MariaQuoteResponse(
                success=True,
                message=self._format_no_results(request, dest_name),
                hotels_found=0,
                destination_id=destination_id,
                destination_name=dest_name,
                raw_results=results,
            )

        # 3. Tentar criar cotacao com link compartilhavel
        quote_url = None
        best_hotel = hotels[0]  # Hotel mais barato (ja ordenado por preco)

        try:
            quote_result = await self._client.create_quote(
                hotel_selection=best_hotel,
                client_data={
                    "name": request.client_name or "",
                    "email": request.client_email or "",
                    "phone": request.client_phone or "",
                },
                destination_id=destination_id,
                checkin=checkin,
                checkout=checkout,
                adults=request.adults,
                children=request.children,
                children_ages=request.children_ages,
                rooms=request.rooms,
            )
            quote_url = quote_result.get("quote_url")
        except Exception as exc:
            logger.warning(
                "Cotacao nao criada (enviando resultados sem link): %s", exc
            )

        # 4. Formatar resposta
        return MariaQuoteResponse(
            success=True,
            message=self._format_results_message(
                request, hotels, dest_name, quote_url
            ),
            quote_url=quote_url,
            hotels_found=len(hotels),
            destination_id=destination_id,
            destination_name=dest_name,
            raw_results=results,
        )

    # -------------------------------------------------------------------------
    # Formatacao de mensagens para WhatsApp
    # -------------------------------------------------------------------------

    def _format_destination_not_found(self, destino: str) -> str:
        return (
            f"Nao encontrei o destino *{destino}* no nosso sistema de hoteis. "
            "Vou verificar com nossa equipe e retorno em breve com opcoes!"
        )

    def _format_search_error(self) -> str:
        return (
            "Tive um problema tecnico ao buscar hoteis agora. "
            "Nossa equipe ja foi notificada e vai retornar com opcoes em breve!"
        )

    def _format_no_results(self, request: MariaQuoteRequest, dest_name: str) -> str:
        checkin = self._format_date_br(request.checkin)
        checkout = self._format_date_br(request.checkout)
        nights = self._calc_nights(request.checkin, request.checkout)
        return (
            f"Busquei hoteis em *{dest_name}* "
            f"de {checkin} a {checkout} ({nights} noites) "
            f"mas nao encontrei disponibilidade para essas datas. "
            f"Quer que eu busque em datas alternativas?"
        )

    def _format_results_message(
        self,
        request: MariaQuoteRequest,
        hotels: list[dict[str, Any]],
        dest_name: str,
        quote_url: Optional[str] = None,
    ) -> str:
        """Formata mensagem WhatsApp com hoteis e link de cotacao."""
        checkin = self._format_date_br(request.checkin)
        checkout = self._format_date_br(request.checkout)
        nights = self._calc_nights(request.checkin, request.checkout)

        lines = [
            f"Encontrei otimas opcoes de hotel em *{dest_name}*!",
            f"Datas: {checkin} a {checkout} ({nights} noites)",
            f"Para {request.adults} adulto(s)",
            "",
        ]

        # Top 3 hoteis
        for i, hotel in enumerate(hotels[:3], 1):
            name = hotel.get("name", "Hotel")
            stars = hotel.get("stars", 0)
            price = hotel.get("total_price", 0)
            currency = hotel.get("currency", "BRL")
            board = hotel.get("board", "")

            star_str = self._star_emoji(stars) if stars else ""
            lines.append(f"{i}. *{name}* {star_str}")
            if price:
                sym = "R$" if currency == "BRL" else currency
                total_fmt = f"{price:,.0f}".replace(",", ".")
                nightly = hotel.get("nightly_price") or 0
                per_night = (
                    f" ({sym} {nightly:,.0f}/noite)".replace(",", ".") if nightly else ""
                )
                lines.append(f"   {sym} {total_fmt} total{per_night}")
            if board:
                lines.append(f"   {board}")
            if hotel.get("cancellation_policy") == "fully_refundable":
                lines.append("   Cancelamento gratis")
            lines.append("")

        if len(hotels) > 3:
            lines.append(f"... e mais {len(hotels) - 3} opcoes disponiveis!")
            lines.append("")

        # Link de cotacao (se criado com sucesso)
        if quote_url:
            lines.append("Veja seu orcamento completo aqui:")
            lines.append(quote_url)
            lines.append("")
            lines.append(
                "Gostou de alguma opcao? Posso te conectar com nosso "
                "setor de fechamentos de viagens pra finalizar tudo!"
            )
        else:
            lines.append(
                "Quer que eu prepare o orcamento completo pra voce? "
                "Nosso setor de fechamentos finaliza tudo rapidinho!"
            )

        return "\n".join(lines)

    @staticmethod
    def _star_emoji(stars: int) -> str:
        """Retorna emoji de estrelas para o hotel."""
        if stars <= 0:
            return ""
        return "★" * stars + "☆" * max(0, 5 - stars)

    @staticmethod
    def _format_date_br(date_str: str) -> str:
        """Converte YYYY-MM-DD para DD/MM/YYYY."""
        try:
            d = date.fromisoformat(date_str)
            return d.strftime("%d/%m/%Y")
        except ValueError:
            return date_str

    @staticmethod
    def _calc_nights(checkin_str: str, checkout_str: str) -> int:
        """Calcula numero de noites entre as datas."""
        try:
            ci = date.fromisoformat(checkin_str)
            co = date.fromisoformat(checkout_str)
            return (co - ci).days
        except ValueError:
            return 0
