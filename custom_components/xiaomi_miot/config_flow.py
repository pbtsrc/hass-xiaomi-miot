"""Config flow to configure Xiaomi Miot."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import *
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac
import homeassistant.helpers.config_validation as cv

from miio import (
    Device as MiioDevice,
    DeviceException,
)

from . import (
    DOMAIN,
    CONF_MODEL,
    CONF_SERVER_COUNTRY,
    CONF_CONFIG_VERSION,
    DEFAULT_NAME,
)
from .core.miot_spec import MiotSpec
from .core.xiaomi_cloud import (
    MiotCloud,
    MiCloudException,
)
from .core.utils import async_analytics_track_event

_LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 30

# 0.1 support multiple integration to add the same device
# 0.2 new entity id format (model_mac[-4:]_suffix)
ENTRY_VERSION = 0.2

CLOUD_SERVERS = {
    'cn': 'China',
    'de': 'Europe',
    'i2': 'India',
    'ru': 'Russia',
    'sg': 'Singapore',
    'us': 'United States',
}


async def check_miio_device(hass, user_input, errors):
    host = user_input.get(CONF_HOST)
    token = user_input.get(CONF_TOKEN)
    try:
        device = MiioDevice(host, token)
        info = await hass.async_add_executor_job(device.info)
    except DeviceException:
        device = None
        info = None
        errors['base'] = 'cannot_connect'
    _LOGGER.debug('Xiaomi Miot config flow: %s', {
        'user_input': user_input,
        'miio_info': info,
        'errors': errors,
    })
    model = ''
    if info is not None:
        if not user_input.get(CONF_MODEL):
            model = str(info.model or '')
            user_input[CONF_MODEL] = model
        user_input['miio_info'] = dict(info.raw or {})
        miot_type = await MiotSpec.async_get_model_type(hass, model)
        if not miot_type:
            miot_type = await MiotSpec.async_get_model_type(hass, model, use_remote=True)
        user_input['miot_type'] = miot_type
        user_input['unique_did'] = format_mac(info.mac_address)
        if miot_type and device:
            try:
                pms = [
                    {'did': 'miot', 'siid': 2, 'piid': 1},
                    {'did': 'miot', 'siid': 2, 'piid': 2},
                    {'did': 'miot', 'siid': 3, 'piid': 1},
                ]
                results = device.get_properties(pms, property_getter='get_properties') or []
                for prop in results:
                    if not isinstance(prop, dict):
                        continue
                    if prop.get('code') == 0:
                        # Collect supported models in LAN
                        await async_analytics_track_event(hass, 'miot', 'local', model)
                        break
            except DeviceException:
                pass
    return user_input


async def check_xiaomi_account(hass, user_input, errors, renew_devices=False):
    dvs = []
    try:
        mic = await MiotCloud.from_token(hass, user_input)
        if not mic:
            raise MiCloudException('Login error')
        if not await mic.async_check_auth(False):
            raise MiCloudException('Login failed')
        await mic.async_stored_auth(mic.user_id, save=True)
        user_input['xiaomi_cloud'] = mic
        dvs = await mic.async_get_devices(renew=renew_devices) or []
    except MiCloudException as exc:
        errors['base'] = 'cannot_login'
        _LOGGER.error('Setup xiaomi cloud for user: %s failed: %s', user_input.get(CONF_USERNAME), exc)
    if renew_devices:
        await MiotSpec.async_get_model_type(hass, 'xiaomi.miot.auto', use_remote=True)
    if not errors:
        user_input['devices'] = dvs
    return user_input


async def get_cloud_filter_schema(hass, user_input, errors, schema=None, via_did=False):
    if not schema:
        schema = vol.Schema({})
    dvs = user_input.get('devices') or []
    if not dvs:
        errors['base'] = 'none_devices'
    else:
        grp = {}
        vls = {}
        fls = ['did'] if via_did else ['model', 'ssid', 'bssid']
        for d in dvs:
            for f in fls:
                v = d.get(f)
                if v is None:
                    continue
                grp.setdefault(v, 0)
                grp[v] += 1
                vls.setdefault(f, {})
                des = '<empty>' if v == '' else v
                vls[f][v] = f'{des} ({grp[v]})'
                if f in ['did']:
                    dip = d.get('localip')
                    if not dip or d.get('pid') not in ['0', '8', '', None]:
                        dip = d.get('model')
                    vls[f][v] = f'{d.get("name")} ({dip})'
                if f in ['model']:
                    dnm = f'{d.get("name")}'
                    if grp[v] > 1:
                        dnm += f' * {grp[v]}'
                    vls[f][v] = f'{des} ({dnm})'
        ies = {
            'exclude': 'Exclude (排除)',
            'include': 'Include (包含)',
        }
        for f in fls:
            if not vls.get(f):
                continue
            fk = f'filter_{f}'
            fl = f'{f}_list'
            lst = vls.get(f, {})
            lst = dict(sorted(lst.items()))
            schema = schema.extend({
                vol.Optional(fk, default=user_input.get(fk, 'exclude')): vol.In(ies),
                vol.Optional(fl, default=user_input.get(fl, [])): cv.multi_select(lst),
            })
        hass.data[DOMAIN]['prev_input'] = user_input
    return schema


class XiaomiMiotFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        self.hass.data.setdefault(DOMAIN, {})
        errors = {}
        if user_input is None:
            user_input = {}
        else:
            action = user_input.get('action')
            if action == 'cloud':
                return await self.async_step_cloud()
            else:
                return await self.async_step_token()
        return self.async_show_form(
            step_id='user',
            data_schema=vol.Schema({
                vol.Required('action', default=user_input.get('action', 'cloud')): vol.In({
                    'cloud': 'Add devices using Mi Account (账号集成)',
                    'token': 'Add device using host/token (局域网集成)',
                }),
            }),
            errors=errors,
        )

    async def async_step_token(self, user_input=None):
        errors = {}
        if user_input is None:
            user_input = {}
        else:
            await check_miio_device(self.hass, user_input, errors)
            if user_input.get('unique_did'):
                await self.async_set_unique_id(user_input['unique_did'])
                self._abort_if_unique_id_configured()
            if user_input.get('miio_info'):
                user_input[CONF_CONFIG_VERSION] = ENTRY_VERSION
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME),
                    data=user_input,
                )
        return self.async_show_form(
            step_id='token',
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=user_input.get(CONF_HOST, vol.UNDEFINED)): str,
                vol.Required(CONF_TOKEN, default=user_input.get(CONF_TOKEN, vol.UNDEFINED)):
                    vol.All(str, vol.Length(min=32, max=32)),
                vol.Optional(CONF_NAME, default=user_input.get(CONF_NAME, DEFAULT_NAME)): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=user_input.get(CONF_SCAN_INTERVAL, DEFAULT_INTERVAL)):
                    cv.positive_int,
            }),
            errors=errors,
        )

    async def async_step_cloud(self, user_input=None):
        # pylint: disable=invalid-name
        self.CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL
        errors = {}
        if user_input is None:
            user_input = {}
        else:
            await check_xiaomi_account(self.hass, user_input, errors)
            if not errors:
                return await self.async_step_cloud_filter(user_input)
        return self.async_show_form(
            step_id='cloud',
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, default=user_input.get(CONF_USERNAME, vol.UNDEFINED)): str,
                vol.Required(CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, vol.UNDEFINED)): str,
                vol.Required(CONF_SERVER_COUNTRY, default=user_input.get(CONF_SERVER_COUNTRY, 'cn')):
                    vol.In(CLOUD_SERVERS),
                vol.Optional('filter_models', default=user_input.get('filter_models', False)): bool,
            }),
            errors=errors,
        )

    async def async_step_cloud_filter(self, user_input=None):
        errors = {}
        schema = vol.Schema({})
        if user_input is None:
            user_input = {}
        if 'devices' in user_input:
            via_did = not user_input.get('filter_models')
            schema = await get_cloud_filter_schema(self.hass, user_input, errors, schema, via_did=via_did)
        elif 'prev_input' in self.hass.data[DOMAIN]:
            prev_input = self.hass.data[DOMAIN].pop('prev_input', None) or {}
            cfg = prev_input['xiaomi_cloud'].to_config() or {}
            cfg.update(user_input or {})
            cfg[CONF_CONFIG_VERSION] = ENTRY_VERSION
            return self.async_create_entry(
                title=f"MiCloud: {cfg.get('user_id')}",
                data=cfg,
            )
        else:
            errors['base'] = 'unknown'
        return self.async_show_form(
            step_id='cloud_filter',
            data_schema=schema,
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info):
        name = discovery_info.get('name')
        host = discovery_info.get('host')
        mac_address = discovery_info.get('properties', {}).get('mac')
        if not name or not host or not mac_address:
            return self.async_abort(reason='not_xiaomi_miio')
        if not name.startswith('xiaomi'):
            _LOGGER.debug('Device %s discovered with host %s, not xiaomi device', name, host)
            return self.async_abort(reason='not_xiaomi_miio')
        unique_id = format_mac(mac_address)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured({CONF_HOST: host})
        # pylint: disable=no-member # https://github.com/PyCQA/pylint/issues/3167
        self.context.update({
            'title_placeholders': {'name': f'{name}({host})'}
        })
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return OptionsFlowHandler(entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        self.hass.data.setdefault(DOMAIN, {})
        if CONF_USERNAME in self.config_entry.data:
            return await self.async_step_cloud()
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}
        if isinstance(user_input, dict):
            cfg = {}
            opt = {}
            for k, v in user_input.items():
                if k in [CONF_HOST, CONF_TOKEN, CONF_NAME, CONF_SCAN_INTERVAL]:
                    cfg[k] = v
                else:
                    opt[k] = v
            await check_miio_device(self.hass, user_input, errors)
            if user_input.get('miio_info'):
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, **cfg}
                )
                return self.async_create_entry(title='', data=opt)
        else:
            user_input = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id='user',
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=user_input.get(CONF_HOST, vol.UNDEFINED)): str,
                vol.Required(CONF_TOKEN, default=user_input.get(CONF_TOKEN, vol.UNDEFINED)):
                    vol.All(str, vol.Length(min=32, max=32)),
                vol.Optional(CONF_SCAN_INTERVAL, default=user_input.get(CONF_SCAN_INTERVAL, DEFAULT_INTERVAL)):
                    cv.positive_int,
                vol.Optional('miot_cloud', default=user_input.get('miot_cloud', False)): bool,
            }),
            errors=errors,
        )

    async def async_step_cloud(self, user_input=None):
        errors = {}
        prev_input = {
            **self.config_entry.data,
            **self.config_entry.options,
        }
        if isinstance(user_input, dict):
            user_input = {
                **self.config_entry.data,
                **self.config_entry.options,
                **user_input,
            }
            renew = not not user_input.pop('renew_devices', False)
            await check_xiaomi_account(self.hass, user_input, errors, renew_devices=renew)
            if not errors:
                user_input['filter_models'] = prev_input.get('filter_models') and True
                if prev_input.get('filter_model'):
                    user_input['filter_models'] = True
                return await self.async_step_cloud_filter(user_input)
        else:
            user_input = prev_input
        return self.async_show_form(
            step_id='cloud',
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, default=user_input.get(CONF_USERNAME, vol.UNDEFINED)): str,
                vol.Required(CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, vol.UNDEFINED)): str,
                vol.Required(CONF_SERVER_COUNTRY, default=user_input.get(CONF_SERVER_COUNTRY, 'cn')):
                    vol.In(CLOUD_SERVERS),
                vol.Optional('renew_devices', default=user_input.get('renew_devices', False)): bool,
            }),
            errors=errors,
        )

    async def async_step_cloud_filter(self, user_input=None):
        errors = {}
        schema = vol.Schema({})
        if user_input is None:
            user_input = {}
        if 'devices' in user_input:
            user_input = {**self.config_entry.data, **self.config_entry.options, **user_input}
            via_did = not user_input.get('filter_models')
            schema = await get_cloud_filter_schema(self.hass, user_input, errors, schema, via_did=via_did)
        elif 'prev_input' in self.hass.data[DOMAIN]:
            prev_input = self.hass.data[DOMAIN].pop('prev_input', None) or {}
            cfg = prev_input['xiaomi_cloud'].to_config() or {}
            cfg.update(user_input or {})
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, **cfg}
            )
            return self.async_create_entry(title='', data={})
        else:
            errors['base'] = 'unknown'
        return self.async_show_form(
            step_id='cloud_filter',
            data_schema=schema,
            errors=errors,
        )
