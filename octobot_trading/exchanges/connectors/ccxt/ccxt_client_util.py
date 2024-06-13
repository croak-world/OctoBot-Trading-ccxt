#  Drakkar-Software OctoBot-Trading
#  Copyright (c) Drakkar-Software, All rights reserved.
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library.
import copy
import logging
import typing
import ccxt
import ccxt.pro as ccxt_pro

import octobot_commons.time_frame_manager as time_frame_manager
import octobot_trading.constants as constants
import octobot_trading.enums as enums
import octobot_trading.errors as errors
import octobot_trading.exchanges.connectors.ccxt.enums as ccxt_enums
import octobot_trading.exchanges.connectors.ccxt.ccxt_clients_cache as ccxt_clients_cache
import octobot_trading.exchanges.util.exchange_util as exchange_util


def create_client(
    exchange_class, exchange_manager, logger, options, headers,
    additional_config, should_authenticate, unauthenticated_exchange_fallback=None,
    keys_adapter=None
):
    """
    Exchange instance creation
    :return: the created ccxt (pro, async or sync) client
    """
    is_authenticated = False
    if not exchange_manager.exchange_only:
        # avoid logging version on temporary exchange_only exchanges
        exchange_type = exchange_util.get_exchange_type(exchange_manager)
        logger.info(f"Creating {exchange_class.__name__} {exchange_type.name} "
                    f"exchange with ccxt in version {ccxt.__version__}")
    if exchange_manager.ignore_config or exchange_manager.check_config(exchange_manager.exchange_name):
        try:
            key, secret, password = exchange_manager.get_exchange_credentials(exchange_manager.exchange_name)
            if keys_adapter:
                key, secret, password = keys_adapter(key, secret, password)
            if not (key and secret) and not exchange_manager.is_simulated and not exchange_manager.ignore_config:
                logger.warning(f"No exchange API key set for {exchange_manager.exchange_name}. "
                               f"Enter your account details to enable real trading on this exchange.")
            if should_authenticate and not exchange_manager.is_backtesting:
                client = exchange_class(_get_client_config(options, headers, additional_config,
                                                           key, secret, password))
                is_authenticated = True
                if exchange_manager.check_credentials:
                    client.checkRequiredCredentials()
            else:
                client = exchange_class(_get_client_config(options, headers, additional_config))
        except (ccxt.AuthenticationError, Exception) as e:
            if unauthenticated_exchange_fallback is None:
                return get_unauthenticated_exchange(
                    exchange_class, options, headers, additional_config
                ), False
            return unauthenticated_exchange_fallback(e), False
    else:
        client = get_unauthenticated_exchange(exchange_class, options, headers, additional_config)
        logger.error("configuration issue: missing login information !")
    client.logger.setLevel(logging.INFO)
    _use_http_proxy_if_necessary(client)
    return client, is_authenticated


async def close_client(client):
    await client.close()
    client.markets = {}
    client.markets_by_id = {}
    client.ids = []
    client.last_json_response = {}
    client.last_http_response = ""
    client.last_response_headers = {}
    client.markets_loading = None
    client.currencies = {}
    client.baseCurrencies = {}
    client.quoteCurrencies = {}
    client.currencies_by_id = {}
    client.codes = []
    client.symbols = {}
    client.accounts = []
    client.accounts_by_id = {}
    client.ohlcvs = {}
    client.trades = {}
    client.orderbooks = {}


def get_unauthenticated_exchange(exchange_class, options, headers, additional_config):
    client = exchange_class(_get_client_config(options, headers, additional_config))
    _use_http_proxy_if_necessary(client)
    return client


def set_sandbox_mode(exchange_connector, is_sandboxed):
    try:
        exchange_connector.client.setSandboxMode(is_sandboxed)
    except ccxt.NotSupported as e:
        default_type = exchange_connector.client.options.get('defaultType', None)
        additional_info = f" in type {default_type}" if default_type else ""
        exchange_connector.logger.warning(f"{exchange_connector.name} does not support sandboxing {additional_info}: {e}")
        # raise exception to stop this exchange and prevent dealing with a real funds exchange
        raise e
    return None


def load_markets_from_cache(client, market_filter: typing.Union[None, typing.Callable[[dict], bool]] = None):
    client.set_markets(
        market
        for market in ccxt_clients_cache.get_exchange_parsed_markets(ccxt_clients_cache.get_client_key(client))
        if market_filter is None or market_filter(market)
    )


def set_markets_cache(client):
    if client.markets:
        ccxt_clients_cache.set_exchange_parsed_markets(
            ccxt_clients_cache.get_client_key(client), copy.deepcopy(list(client.markets.values()))
        )


def get_ccxt_client_login_options(exchange_manager):
    """
    :return: ccxt client login option dict, can be overwritten to custom exchange login
    """
    if exchange_manager.is_future:
        return {'defaultType': 'future'}
    if exchange_manager.is_margin:
        return {'defaultType': 'margin'}
    return {'defaultType': 'spot'}


def get_symbols(client, active_only):
    try:
        if active_only:
            return set(
                symbol
                for symbol in client.symbols
                if client.markets.get(symbol, {}).get(
                    enums.ExchangeConstantsMarketStatusColumns.ACTIVE.value, True
                )
            )
        return set(client.symbols)
    except (AttributeError, TypeError):
        # ccxt exchange load_markets failed
        return set()


def get_time_frames(client):
    try:
        if isinstance(client, ccxt_pro.Exchange):
            # ccxt pro exchanges might have different timeframes in options
            options_time_frames = client.safe_value(client.options, 'timeframes')
            if options_time_frames:
                values = set([
                    time_frame
                    for time_frame in options_time_frames
                    if time_frame_manager.is_time_frame(time_frame)
                ])
                if values:
                    return values
        # use normal client timeframes (values of rest exchange)
        return set(client.timeframes)
    except (AttributeError, TypeError):
        # ccxt exchange describe() is invalid
        return set()


def get_exchange_pair(client, pair) -> str:
    if pair in client.symbols:
        try:
            return client.market(pair)["id"]
        except KeyError:
            pass
    raise ValueError(f'{pair} is not supported')


def get_pair_cryptocurrency(client, pair) -> str:
    if pair in client.symbols:
        try:
            return client.market(pair)["base"]
        except KeyError:
            pass
    raise ValueError(f'{pair} is not supported')


def get_contract_size(client, pair) -> float:
    return client.markets[pair][ccxt_enums.ExchangeConstantsMarketStatusCCXTColumns.CONTRACT_SIZE.value]


def get_fees(market_status) -> dict:
    return {
        enums.ExchangeConstantsMarketPropertyColumns.TAKER.value:
            market_status.get(enums.ExchangeConstantsMarketPropertyColumns.TAKER.value,
                              constants.CONFIG_DEFAULT_FEES),
        enums.ExchangeConstantsMarketPropertyColumns.MAKER.value:
            market_status.get(enums.ExchangeConstantsMarketPropertyColumns.MAKER.value,
                              constants.CONFIG_DEFAULT_FEES),
        enums.ExchangeConstantsMarketPropertyColumns.FEE.value:
            market_status.get(enums.ExchangeConstantsMarketPropertyColumns.FEE.value,
                              constants.CONFIG_DEFAULT_FEES)
    }


def add_headers(client, headers_dict):
    """
    Add new headers to ccxt client
    :param headers_dict: the additional header keys and values as dict
    """
    for header_key, header_value in headers_dict.items():
        client.headers[header_key] = header_value


def add_options(client, options_dict):
    """
    Add new options to ccxt client
    :param options_dict: the additional option keys and values as dict
    """
    for option_key, option_value in options_dict.items():
        client.options[option_key] = option_value


def converted_ccxt_common_errors(f):
    async def converted_ccxt_common_errors_wrapper(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except ccxt.RateLimitExceeded as err:
            raise errors.RateLimitExceeded(err) from err
        except ccxt.NotSupported as err:
            raise errors.NotSupported(err) from err
    return converted_ccxt_common_errors_wrapper


def _use_http_proxy_if_necessary(client):
    client.aiohttp_trust_env = constants.ENABLE_EXCHANGE_HTTP_PROXY_FROM_ENV


def _get_client_config(options, headers, additional_config, api_key=None, secret=None, password=None):
    config = {
        'verbose': constants.ENABLE_CCXT_VERBOSE,
        'enableRateLimit': constants.ENABLE_CCXT_RATE_LIMIT,
        'timeout': constants.DEFAULT_REQUEST_TIMEOUT,
        'options': options,
        'headers': headers
    }
    if api_key is not None:
        config['apiKey'] = api_key
    if secret is not None:
        config['secret'] = secret
    if password is not None:
        config['password'] = password
    config.update(additional_config or {})
    return config
