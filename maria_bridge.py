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


# Aeroporto principal de cada destino (pra busca de voo)
DESTINATION_IATA: dict[str, str] = {
    "CIT_6624": "SCL", "CIT_394": "CJC", "CIT_982": "BUE", "CIT_901": "BRC",
    "CIT_4451": "MDZ", "CIT_4088": "LIM", "CIT_1579": "CUZ", "CIT_854": "BOG",
    "CIT_1535": "CTG", "CIT_6350": "ADZ", "CIT_1569": "CUN", "CIT_4475": "MEX",
    "CIT_5963": "PUJ", "CIT_5419": "MCO", "CIT_4625": "MIA", "CIT_4685": "MVD",
}

# Origem (cidade/aeroporto de saida do cliente) -> IATA
ORIGIN_IATA: dict[str, str] = {
    "joinville": "JOI", "navegantes": "NVT", "campo grande": "CGR", "vitoria": "VIX", "belem": "BEL",
    "natal": "NAT", "salvador": "SSA", "londrina": "LDB", "maringa": "MGF", "ribeirao preto": "RAO",
    "uberlandia": "UDI", "foz do iguacu": "IGU", "porto seguro": "BPS", "cuiaba": "CGB", "goiania": "GYN",
    "sao luis": "SLZ", "teresina": "THE", "manaus": "MAO", "aracaju": "AJU", "joao pessoa": "JPA", "maceio": "MCZ",
    "sao paulo": "GRU", "são paulo": "GRU", "sp": "GRU", "guarulhos": "GRU", "gru": "GRU",
    "congonhas": "CGH", "campinas": "VCP", "viracopos": "VCP",
    "rio de janeiro": "GIG", "rio": "GIG", "galeao": "GIG", "galeão": "GIG", "gig": "GIG",
    "santos dumont": "SDU",
    "belo horizonte": "CNF", "bh": "CNF", "confins": "CNF", "cnf": "CNF",
    "brasilia": "BSB", "brasília": "BSB", "bsb": "BSB",
    "curitiba": "CWB", "porto alegre": "POA", "florianopolis": "FLN", "florianópolis": "FLN",
    "salvador": "SSA", "recife": "REC", "fortaleza": "FOR", "natal": "NAT", "maceio": "MCZ",
    "maceió": "MCZ", "belem": "BEL", "belém": "BEL", "manaus": "MAO", "goiania": "GYN",
    "goiânia": "GYN", "cuiaba": "CGB", "cuiabá": "CGB", "campo grande": "CGR",
    "vitoria": "VIX", "vitória": "VIX", "joao pessoa": "JPA", "joão pessoa": "JPA",
    "aracaju": "AJU", "teresina": "THE", "sao luis": "SLZ", "são luís": "SLZ",
    "londrina": "LDB", "maringa": "MGF", "maringá": "MGF", "ribeirao preto": "RAO",
    "ribeirão preto": "RAO", "uberlandia": "UDI", "uberlândia": "UDI", "navegantes": "NVT",
    "foz do iguacu": "IGU", "foz do iguaçu": "IGU", "porto seguro": "BPS", "ilheus": "IOS",
}


def _norm(txt: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return t.strip().lower()


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

    # O que o cliente pediu: hotel | flight | package (voo + hotel, link unico)
    product: str = Field(default="hotel", description="hotel | flight | package")
    # Cidade/aeroporto de saida (necessario pra flight e package)
    origem: Optional[str] = Field(default=None, description="Cidade ou IATA de origem")
    infants: int = Field(default=0, ge=0, le=4)

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
    product: str = "hotel"
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

        product = (request.product or "hotel").strip().lower()
        if product in ("voo", "passagem", "flight", "flights"):
            return await self._generate_flight_quote(request, destination_id, dest_name)
        if product in ("pacote", "package", "both", "voo+hotel"):
            return await self._generate_package_quote(request, destination_id, dest_name)

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
            product="hotel",
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
    # Origem (IATA / CIT) e cotacoes de voo e pacote
    # -------------------------------------------------------------------------

    def _origin_iata(self, origem: Optional[str]) -> Optional[str]:
        if not origem:
            return None
        o = _norm(origem)
        if len(o) == 3 and o.isalpha():
            return o.upper()
        if o in ORIGIN_IATA:
            return ORIGIN_IATA[o]
        for k, v in ORIGIN_IATA.items():
            if k in o or o in k:
                return v
        return None

    async def _origin_cit(self, origem: Optional[str]) -> Optional[str]:
        """Resolve a cidade de origem pra CIT_ (pacotes usam cidade, nao aeroporto)."""
        if not origem:
            return None
        key = _norm(origem)
        iata = self._origin_iata(origem)
        # Nomes amigaveis pros IATA mais comuns (o suggestions entende melhor o nome)
        iata_to_city = {
            "SAO": "Sao Paulo", "GRU": "Sao Paulo", "CGH": "Sao Paulo", "VCP": "Campinas",
            "RIO": "Rio de Janeiro", "GIG": "Rio de Janeiro", "SDU": "Rio de Janeiro",
            "BHZ": "Belo Horizonte", "CNF": "Belo Horizonte", "BSB": "Brasilia",
        }
        query = iata_to_city.get(iata or "", origem)
        cached = DESTINATION_MAP.get("origem:" + key)
        if cached:
            return cached
        suggestions = await self._client.get_suggestions(query)
        for sgg in suggestions:
            if isinstance(sgg, dict) and sgg.get("type") == "CITY" and str(sgg.get("id", "")).startswith("CIT_"):
                DESTINATION_MAP["origem:" + key] = sgg["id"]
                return sgg["id"]
        return None

    def _client_data(self, request: MariaQuoteRequest) -> dict[str, Any]:
        return {
            "name": request.client_name or "",
            "email": request.client_email or "",
            "phone": request.client_phone or "",
        }

    async def _generate_flight_quote(
        self, request: MariaQuoteRequest, destination_id: str, dest_name: str
    ) -> MariaQuoteResponse:
        origin = self._origin_iata(request.origem)
        dest_iata = DESTINATION_IATA.get(destination_id)
        if not origin or not dest_iata:
            return MariaQuoteResponse(
                success=False, product="flight",
                message=self._format_missing_origin(request.origem, dest_name),
                error=f"origem/destino sem IATA: origem={request.origem} dest={destination_id}",
                destination_id=destination_id, destination_name=dest_name,
            )
        try:
            dep = date.fromisoformat(request.checkin)
            ret = date.fromisoformat(request.checkout)
            result = await self._client.create_flight_quote(
                origin=origin, destination=dest_iata, departure=dep, return_date=ret,
                client_data=self._client_data(request),
                adults=request.adults, children=request.children, infants=request.infants,
            )
        except Exception as exc:
            logger.error("Erro na cotacao de voo: %s", exc, exc_info=True)
            return MariaQuoteResponse(
                success=False, product="flight", message=self._format_search_error(),
                error=str(exc), destination_id=destination_id, destination_name=dest_name,
            )
        return MariaQuoteResponse(
            success=True, product="flight",
            message=self._format_flight_message(request, dest_name, origin, result),
            quote_url=result.get("quote_url"), destination_id=destination_id,
            destination_name=dest_name, raw_results=result,
        )

    async def _generate_package_quote(
        self, request: MariaQuoteRequest, destination_id: str, dest_name: str
    ) -> MariaQuoteResponse:
        origin_cit = await self._origin_cit(request.origem)
        if not origin_cit:
            return MariaQuoteResponse(
                success=False, product="package",
                message=self._format_missing_origin(request.origem, dest_name),
                error=f"origem sem CIT: {request.origem}",
                destination_id=destination_id, destination_name=dest_name,
            )
        try:
            dep = date.fromisoformat(request.checkin)
            ret = date.fromisoformat(request.checkout)
            result = await self._client.create_package_quote(
                origin_cit=origin_cit, dest_cit=destination_id, departure=dep, return_date=ret,
                client_data=self._client_data(request),
                adults=request.adults, children=request.children,
                children_ages=request.children_ages, rooms=request.rooms,
            )
        except Exception as exc:
            logger.error("Erro na cotacao de pacote: %s", exc, exc_info=True)
            return MariaQuoteResponse(
                success=False, product="package", message=self._format_search_error(),
                error=str(exc), destination_id=destination_id, destination_name=dest_name,
            )
        return MariaQuoteResponse(
            success=True, product="package",
            message=self._format_package_message(request, dest_name, result),
            quote_url=result.get("quote_url"), hotels_found=len(result.get("hotels") or []),
            destination_id=destination_id, destination_name=dest_name, raw_results=result,
        )

    # -------------------------------------------------------------------------
    # Formatacao de mensagens para WhatsApp
    # -------------------------------------------------------------------------

    def _format_missing_origin(self, origem: Optional[str], dest_name: str) -> str:
        if origem:
            return (
                f"Nao consegui identificar o aeroporto de saida *{origem}* pra montar "
                f"a passagem pra *{dest_name}*. Me diz de qual cidade voce sai?"
            )
        return f"Pra montar a passagem pra *{dest_name}*, de qual cidade voce sai?"

    # Nome da cidade por codigo IATA (fallback quando a pagina da cotacao nao traz)
    _IATA_CITY = {
        "GRU": "Sao Paulo (Guarulhos)", "CGH": "Sao Paulo (Congonhas)", "VCP": "Campinas (Viracopos)",
        "GIG": "Rio de Janeiro (Galeao)", "SDU": "Rio de Janeiro (Santos Dumont)", "CNF": "Belo Horizonte (Confins)",
        "BSB": "Brasilia", "CWB": "Curitiba", "POA": "Porto Alegre", "FLN": "Florianopolis", "NVT": "Navegantes",
        "SSA": "Salvador", "REC": "Recife", "FOR": "Fortaleza", "NAT": "Natal", "MCZ": "Maceio", "JPA": "Joao Pessoa",
        "AJU": "Aracaju", "BEL": "Belem", "MAO": "Manaus", "SLZ": "Sao Luis", "THE": "Teresina", "GYN": "Goiania",
        "CGB": "Cuiaba", "CGR": "Campo Grande", "VIX": "Vitoria", "LDB": "Londrina", "MGF": "Maringa",
        "RAO": "Ribeirao Preto", "UDI": "Uberlandia", "IGU": "Foz do Iguacu", "BPS": "Porto Seguro",
        "SCL": "Santiago", "BOG": "Bogota", "CTG": "Cartagena", "MDE": "Medellin", "ADZ": "San Andres",
        "LIM": "Lima", "CUZ": "Cusco", "EZE": "Buenos Aires (Ezeiza)", "AEP": "Buenos Aires (Aeroparque)",
        "BRC": "Bariloche", "MDZ": "Mendoza", "USH": "Ushuaia", "PUJ": "Punta Cana", "SDQ": "Santo Domingo",
        "CUN": "Cancun", "MEX": "Cidade do Mexico", "CUR": "Curacao", "MIA": "Miami", "MCO": "Orlando",
        "JFK": "Nova York (JFK)", "EWR": "Nova York (Newark)", "LAX": "Los Angeles", "LAS": "Las Vegas",
    }

    def _airport_label(self, code: str, city: str = "") -> str:
        code = (code or "").upper()
        city = (city or "").strip()
        if not city:
            city = self._IATA_CITY.get(code, "")
        return f"{city} ({code})" if city and code else (city or code)

    @staticmethod
    def _stops_label(stops: str) -> str:
        s = (stops or "").strip().lower()
        if not s:
            return ""
        if s.startswith("direto"):
            return "voo direto"
        return s

    def _flight_lines(self, leg: dict[str, Any], label: str) -> list[str]:
        """Linhas de um trecho (ida/volta) com cidade + codigo, horarios, cia e bagagem."""
        if not leg:
            return []
        out = [f"*{label}:* {self._airport_label(leg.get('from_code'), leg.get('from_city'))} "
               f"{leg.get('dep', '')} -> {self._airport_label(leg.get('to_code'), leg.get('to_city'))} {leg.get('arr', '')}"]
        extra = []
        if leg.get("airline"):
            extra.append(leg["airline"])
        st = self._stops_label(leg.get("stops", ""))
        if st:
            extra.append(st + (f" via {leg['via']}" if leg.get("via") and st != "voo direto" else ""))
        if leg.get("duration"):
            extra.append(leg["duration"].replace(" h ", "h").replace(" m", "min"))
        if extra:
            out.append("   " + " | ".join(extra))
        return out

    @staticmethod
    def _baggage_line(flights: list[dict[str, Any]]) -> str:
        """Resumo de bagagem (pior caso entre os trechos: se um nao inclui, avisa)."""
        bags = [f.get("baggage") for f in flights if f.get("baggage")]
        if not bags:
            return ""
        backpack = all(b.get("backpack") for b in bags)
        hand = all(b.get("hand") for b in bags)
        checked = all(b.get("checked") for b in bags)
        if checked:
            return "🧳 Bagagem: mochila + mala de mao + mala despachada inclusas"
        if hand:
            return "🧳 Bagagem: mochila + mala de mao inclusas (mala despachada NAO inclusa, da pra adicionar)"
        if backpack:
            return "🧳 Bagagem: so mochila/bolsa de mao inclusa (mala de mao e despachada NAO inclusas, da pra adicionar)"
        return "🧳 Bagagem: conferir no link (tarifa basica)"

    @staticmethod
    def _board_line(hotel: dict[str, Any]) -> str:
        board = (hotel.get("board") or "").lower()
        if "cafe" in board:
            return "☕ Cafe da manha incluso"
        if "all" in board:
            return "🍽️ All inclusive"
        if "pensao" in board or "meia" in board:
            return f"🍽️ {hotel.get('board')}"
        if board:
            return "☕ Cafe da manha NAO incluso"
        return ""

    def _format_flight_message(
        self, request: MariaQuoteRequest, dest_name: str, origin: str, result: dict[str, Any]
    ) -> str:
        ida = self._format_date_br(request.checkin)
        volta = self._format_date_br(request.checkout)
        pax = request.adults + (request.children or 0)
        details = result.get("details") or {}
        flights = details.get("flights") or []
        f_out = next((f for f in flights if f.get("leg") == "out"), None)
        f_back = next((f for f in flights if f.get("leg") == "back"), None)
        lines = [
            f"Montei sua passagem pra *{dest_name}*! ✈️",
            "",
            f"📅 *Datas:* ida {ida} e volta {volta}",
            f"👥 *Viajantes:* {pax} pessoa(s)",
            "",
        ]
        if f_out or f_back:
            lines.append("✈️ *VOO*")
            lines += self._flight_lines(f_out, "Ida")
            lines += self._flight_lines(f_back, "Volta")
            bl = self._baggage_line(flights)
            if bl:
                lines.append(bl)
            lines.append("")
        parsed = result.get("flights") or []
        total_txt = details.get("total_text")
        if total_txt:
            lines.append(f"💰 *Total:* R$ {total_txt} para {pax} pessoa(s)")
            lines.append("Taxas e impostos inclusos")
            lines.append("")
        elif parsed and parsed[0].get("total"):
            f0 = parsed[0]
            total = f"{float(f0['total']):,.0f}".replace(",", ".")
            lines.append(f"💰 *Total:* R$ {total}")
            lines.append("")
        lines += [
            "🔗 *Veja a passagem completa e todos os detalhes aqui:*",
            result.get("quote_url") or "",
            "",
            "Gostou? Se quiser ajustar algo (datas, hotel, cidade de saída) ou ver outro destino, me diz que eu monto outra opção. E quando quiser garantir a reserva, é só me avisar! 😊",
        ]
        return "\n".join(lines)

    def _format_package_message(
        self, request: MariaQuoteRequest, dest_name: str, result: dict[str, Any]
    ) -> str:
        ida = self._format_date_br(request.checkin)
        volta = self._format_date_br(request.checkout)
        nights = self._calc_nights(request.checkin, request.checkout)
        rooms = max(1, int(request.rooms or 1))
        pax = request.adults * rooms + (request.children or 0)
        h = result.get("selected_hotel") or ((result.get("hotels") or [None])[0]) or {}
        summary = result.get("summary") or {}
        details = result.get("details") or {}
        _badges = ("all inclusive", "cafe da manha", "café da manhã", "oferta imbatível", "reserva flexível", "pague em parcelas")
        if (not h or not h.get("name") or (h.get("name") or "").strip().lower() in _badges) and details.get("hotel_name"):
            dn = details["hotel_name"].strip()
            match = next((x for x in (result.get("hotels") or []) if (x.get("name") or "").strip().lower() == dn.lower()), None)
            h = match or {**(h or {}), "name": dn, "address": details.get("hotel_address", "")}
        flights = details.get("flights") or []
        f_out = next((f for f in flights if f.get("leg") == "out"), None)
        f_back = next((f for f in flights if f.get("leg") == "back"), None)
        if not f_out and summary.get("flight_out"):
            fo = summary["flight_out"]
            f_out = {"from_code": fo["from"], "dep": fo["dep"], "to_code": fo["to"], "arr": fo["arr"], "stops": fo["stops"]}
        if not f_back and summary.get("flight_back"):
            fb = summary["flight_back"]
            f_back = {"from_code": fb["from"], "dep": fb["dep"], "to_code": fb["to"], "arr": fb["arr"], "stops": fb["stops"]}

        lines = [
            f"Montei um pacote completo pra *{dest_name}* com voo + hotel! ✈️🏨",
            "",
            f"📅 *Datas:* {ida} a {volta} ({nights} noites)",
            f"👥 *Viajantes:* {pax} pessoa(s)",
            "",
        ]
        if f_out or f_back:
            lines.append("✈️ *VOO*")
            lines += self._flight_lines(f_out, "Ida")
            lines += self._flight_lines(f_back, "Volta")
            bl = self._baggage_line(flights)
            if bl:
                lines.append(bl)
            lines.append("")
        if h and h.get("name"):
            stars = self._star_emoji(h.get("stars", 0) or 0)
            lines.append("🏨 *HOTEL*")
            lines.append(f"*{h.get('name')}*" + (f" {stars}" if stars else ""))
            if rooms > 1:
                lines.append(f"🛏️ {rooms} quartos")
            loc = h.get("zone") or h.get("region") or ""
            addr = h.get("address") or details.get("hotel_address") or ""
            if loc and loc.lower() not in addr.lower():
                lines.append(f"📍 {loc}" + (f" - {addr}" if addr else ""))
            elif addr:
                lines.append(f"📍 {addr}")
            bl = self._board_line(h)
            if bl:
                lines.append(bl)
            if h.get("review_score"):
                lines.append(f"⭐ Nota {h['review_score']} dos hospedes")
            if h.get("cancellation_policy") == "fully_refundable" or h.get("free_cancellation_available"):
                lines.append("✅ Cancelamento gratis")
            lines.append("")
        if summary.get("price_total"):
            total = f"{summary['price_total']:,.0f}".replace(",", ".")
            spax = summary.get("pax") or pax
            line = f"💰 *Total voo + hotel:* R$ {total} para {spax} pessoas"
            if summary.get("price_per_person"):
                pp = f"{summary['price_per_person']:,.0f}".replace(",", ".")
                line += f" (R$ {pp} por pessoa)"
            lines.append(line)
            lines.append("Taxas e impostos inclusos")
            lines.append("")
        lines += [
            "🔗 *Veja o pacote completo e todos os detalhes aqui:*",
            result.get("quote_url") or "",
            "",
            "Gostou? Se quiser ajustar algo (datas, hotel, cidade de saída) ou ver outro destino, me diz que eu monto outra opção. E quando quiser garantir a reserva, é só me avisar! 😊",
        ]
        return "\n".join(lines)

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
            lines.append("🔗 *Veja seu orçamento completo aqui:*")
            lines.append(quote_url)
            lines.append("")
            lines.append(
                "Gostou de alguma opção? Se quiser ajustar datas ou ver outros hotéis, "
                "me diz que eu monto outra opção. E quando quiser garantir a reserva, é só me avisar! 😊"
            )
        else:
            lines.append(
                "Quer que eu prepare o orçamento completo pra você? É só me dizer!"
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
