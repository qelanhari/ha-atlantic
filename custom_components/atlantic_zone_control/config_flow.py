"""Config flow for Atlantic Zone Control integration."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError
from pyoverkiz.client import OverkizClient
from pyoverkiz.const import SUPPORTED_SERVERS
from pyoverkiz.enums import Server
from pyoverkiz.exceptions import (
    BadCredentialsException,
    MaintenanceException,
    NotAuthenticatedException,
    TooManyAttemptsBannedException,
    TooManyRequestsException,
)
from pyoverkiz.utils import is_overkiz_gateway
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN, LOGGER

SERVER = Server.SOMFY_EUROPE


class AtlanticZoneControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Atlantic Zone Control."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the credentials step."""
        errors: dict[str, str] = {}

        if user_input:
            session = async_create_clientsession(self.hass)
            client = OverkizClient(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                server=SUPPORTED_SERVERS[SERVER],
                session=session,
            )

            try:
                await client.login(register_event_listener=False)

                if gateways := await client.get_gateways():
                    for gateway in gateways:
                        if is_overkiz_gateway(gateway.id):
                            await self.async_set_unique_id(
                                gateway.id, raise_on_progress=False
                            )
                            break

            except TooManyRequestsException:
                errors["base"] = "too_many_requests"
            except (BadCredentialsException, NotAuthenticatedException):
                errors["base"] = "invalid_auth"
            except (TimeoutError, ClientError):
                errors["base"] = "cannot_connect"
            except MaintenanceException:
                errors["base"] = "server_in_maintenance"
            except TooManyAttemptsBannedException:
                errors["base"] = "too_many_attempts"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
                LOGGER.exception("Unknown error")
            else:
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication when credentials become invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth credential input."""
        errors: dict[str, str] = {}

        if user_input:
            session = async_create_clientsession(self.hass)
            client = OverkizClient(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                server=SUPPORTED_SERVERS[SERVER],
                session=session,
            )

            try:
                await client.login(register_event_listener=False)
            except (BadCredentialsException, NotAuthenticatedException):
                errors["base"] = "invalid_auth"
            except (TimeoutError, ClientError):
                errors["base"] = "cannot_connect"
            except TooManyRequestsException:
                errors["base"] = "too_many_requests"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
                LOGGER.exception("Unknown error during reauth")
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        reauth_entry = self._get_reauth_entry()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=reauth_entry.data.get(CONF_USERNAME),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )
