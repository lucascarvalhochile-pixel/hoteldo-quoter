"""
Autenticacao HotelDo via tokens reCAPTCHA (mixin do HotelDoClient).

O reCAPTCHA v2 bloqueia login headless. Solucao: um Chrome real gera os
tokens (checkbox + invisivel), POSTa em /admin/token-login, e aqui fazemos
o fluxo de login por HTTP puro, recebendo os cookies httpOnly por Set-Cookie
e injetando no contexto Playwright.

Endpoints descobertos no login-bundle.js 7.72.0 (ForgotPasswordService-bundle):
  POST /login/evo/login/user/status   {email, recaptcha, action_type: "signin"}
  POST /login/evo/login?from=landing&url_ref=...&invisible=true
       {type: "DESPEGAR", username, user_id, password, recaptcha, flow_context}
"""

import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger("hoteldo_quoter.auth")


class TokenAuthMixin:
    """Metodos de login via tokens reCAPTCHA. Espera self._context/_page/inject_cookies."""

    # -------------------------------------------------------------------------
    # Token Login (HTTP direto, sem browser) — bypass reCAPTCHA com tokens
    # gerados num Chrome real. Os cookies httpOnly chegam via Set-Cookie e
    # sao injetados no Playwright.
    # -------------------------------------------------------------------------

    LOGIN_ORIGIN = "https://pt-hoteldo.e-agencias.com"
    LOGIN_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )

    async def login_with_tokens(
        self,
        status_token: str,
        login_token: str,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Faz o login do HotelDo via HTTP puro usando tokens reCAPTCHA ja
        resolvidos (gerados num navegador real).

        Fluxo (descoberto no login-bundle.js 7.72.0):
          1. GET  /login/signin                         -> cookies iniciais
          2. POST /login/evo/login/user/status          {email, recaptcha, action_type}
          3. POST /login/evo/login?from=landing&...     {type, username, user_id, password, recaptcha}
          4. Cookies do jar (incluindo httpOnly) -> inject_cookies()

        Returns dict com status de cada etapa e se a sessao autenticou.
        """
        email = email or settings.hoteldo_email
        password = password or settings.hoteldo_password
        result: dict[str, Any] = {"steps": {}}

        headers = {
            "User-Agent": self.LOGIN_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Content-Type": "application/json",
            "Origin": self.LOGIN_ORIGIN,
            "Referer": f"{self.LOGIN_ORIGIN}/login/signin",
            "X-Requested-With": "XMLHttpRequest",
        }

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=headers
        ) as client:
            # 1. Seed cookies
            r0 = await client.get(
                f"{self.LOGIN_ORIGIN}/login/signin",
                headers={"Accept": "text/html,*/*"},
            )
            result["steps"]["signin_page"] = r0.status_code

            # 2. user/status (valida email + captcha checkbox)
            r1 = await client.post(
                f"{self.LOGIN_ORIGIN}/login/evo/login/user/status",
                json={"email": email, "recaptcha": status_token, "action_type": "signin"},
            )
            try:
                d1 = r1.json()
            except Exception:
                d1 = {"raw": r1.text[:300]}
            result["steps"]["user_status"] = {"http": r1.status_code, "body": d1}
            if r1.status_code != 200:
                result["authenticated"] = False
                result["error"] = f"user/status falhou: {d1}"
                return result

            user_id = d1.get("user_id") if isinstance(d1, dict) else None

            # 3. login (captcha invisivel)
            params = {
                "from": "landing",
                "url_ref": f"{self.LOGIN_ORIGIN}/",
                "invisible": "true",
            }
            r2 = await client.post(
                f"{self.LOGIN_ORIGIN}/login/evo/login",
                params=params,
                json={
                    "type": "DESPEGAR",
                    "username": email,
                    "user_id": user_id,
                    "password": password,
                    "recaptcha": login_token,
                    "flow_context": None,
                },
            )
            try:
                d2 = r2.json()
            except Exception:
                d2 = {"raw": r2.text[:300]}
            # nao logar dados do usuario inteiros
            safe_d2 = d2 if r2.status_code != 200 else {
                "user": (d2.get("user") or {}).get("email") if isinstance(d2, dict) else None
            }
            result["steps"]["login"] = {"http": r2.status_code, "body": safe_d2}
            if r2.status_code != 200:
                result["authenticated"] = False
                result["error"] = f"login falhou: {d2}"
                return result

            # 4. Extrair cookies do jar (com flags httpOnly/secure)
            cookies: list[dict[str, Any]] = []
            for c in client.cookies.jar:
                rest = getattr(c, "_rest", {}) or {}
                http_only = any(k.lower() == "httponly" for k in rest.keys())
                domain = c.domain or ".e-agencias.com"
                cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": domain,
                    "path": c.path or "/",
                    "secure": bool(c.secure),
                    "httpOnly": http_only,
                    "sameSite": "Lax",
                    **({"expires": c.expires} if c.expires else {}),
                })

        result["cookies_count"] = len(cookies)
        result["cookie_names"] = [c["name"] for c in cookies]
        logger.info(
            "Token-login OK via HTTP. %d cookies recebidos: %s",
            len(cookies), result["cookie_names"],
        )

        result["cookie_domains"] = {c["name"]: c["domain"] for c in cookies}

        # Salvar e injetar no Playwright
        cookies_path = str(Path(settings.browser_state_path) / "cookies.json")
        ok = await self.inject_cookies(cookies)
        result["inject_attempt_1"] = ok
        if not ok:
            # Retry: ampliar dominio pra .e-agencias.com (host-only nao chega
            # no subdominio da agencia)
            broad = [dict(c, domain=".e-agencias.com") for c in cookies]
            ok = await self.inject_cookies(broad)
            result["inject_attempt_2_broad_domain"] = ok
            if ok:
                cookies = broad
        if ok:
            self.save_cookies_to_file(cookies, cookies_path)
        result["debug"] = await self.debug_session()
        result["authenticated"] = ok
        return result

    async def debug_session(self) -> dict[str, Any]:
        """Diagnostico: cookies no contexto, URL atual e status da API de sessao."""
        info: dict[str, Any] = {}
        try:
            ctx_cookies = await self._context.cookies()
            info["context_cookies"] = [
                {"name": c["name"], "domain": c["domain"], "httpOnly": c.get("httpOnly")}
                for c in ctx_cookies
            ]
            info["page_url"] = self._page.url
            info["status_api"] = await self._page.evaluate("""
                async () => {
                    const out = {};
                    for (const u of ['/socrates/api/current-user/status',
                                     'https://pt-hoteldo.e-agencias.com/socrates/api/current-user/status']) {
                        try { const r = await fetch(u, {credentials: 'include'}); out[u] = r.status; }
                        catch (e) { out[u] = 'ERR ' + e; }
                    }
                    return out;
                }
            """)
            info["body_snippet"] = (await self._page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 300)"
            ))
        except Exception as exc:
            info["error"] = str(exc)
        return info
