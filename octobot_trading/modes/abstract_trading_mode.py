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
import abc
import contextlib
import decimal
import time

import octobot_commons.channels_name as channels_name
import octobot_commons.constants as common_constants
import octobot_commons.enums as common_enums
import octobot_commons.logging as logging
import octobot_commons.databases as databases
import octobot_commons.configuration as commons_configuration
import octobot_commons.tentacles_management as abstract_tentacle
import octobot_commons.authentication as authentication

import async_channel.constants as channel_constants

import octobot_tentacles_manager.api as tentacles_manager_api
import octobot_tentacles_manager.configuration as tm_configuration

import octobot_backtesting.api as backtesting_api

import octobot_trading.api as trading_api
import octobot_trading.constants as constants
import octobot_trading.enums as enums
import octobot_trading.exchange_channel as exchanges_channel
import octobot_trading.modes.script_keywords as script_keywords
import octobot_trading.modes.modes_factory as modes_factory
import octobot_trading.modes.channel.abstract_mode_producer as abstract_mode_producer
import octobot_trading.modes.channel.abstract_mode_consumer as abstract_mode_consumer
import octobot_trading.personal_data.orders as orders
import octobot_trading.personal_data.portfolios as portfolios
import octobot_trading.signals as signals
import octobot_trading.modes.script_keywords.basic_keywords as basic_keywords
import octobot_trading.modes.script_keywords.context_management as context_management


class AbstractTradingMode(abstract_tentacle.AbstractTentacle):
    __metaclass__ = abc.ABCMeta
    USER_INPUT_TENTACLE_TYPE = common_enums.UserInputTentacleTypes.TRADING_MODE


    MODE_PRODUCER_CLASSES = []
    MODE_CONSUMER_CLASSES = []
    # maximum seconds before sending a trading signal if orders are slow to create on exchange
    TRADING_SIGNAL_TIMEOUT = 10
    SAVED_RUN_METADATA_DB_BY_BOT_ID = {}

    def __init__(self, config, exchange_manager):
        super().__init__()
        self.logger = logging.get_logger(self.get_name())

        # Global OctoBot configuration
        self.config: dict = config

        # Mode related exchange manager instance
        self.exchange_manager = exchange_manager

        # The id of the OctoBot using this trading mode
        self.bot_id: str = None

        # Evaluator specific config (Is loaded from tentacle specific file)
        self.trading_config: dict = None

        # If this mode is enabled
        self.enabled: bool = True

        # Specified Cryptocurrency for this instance (Should be None if wildcard)
        self.cryptocurrency: str = None

        # Symbol is the cryptocurrency pair (Should be None if wildcard)
        self.symbol: str = None

        # Time_frame is the chart time frame (Should be None if wildcard)
        self.time_frame = None

        # producers is the list of producers created by this trading mode
        self.producers = []

        # producers is the list of consumers created by this trading mode
        self.consumers = []

        # True when this trading mode is waken up only after full candles close
        self.is_triggered_after_candle_close = False

        self.start_time = time.time()
        self.are_metadata_saved = False

    # Used to know the current state of the trading mode.
    # Overwrite in subclasses
    def get_current_state(self) -> tuple:
        """
        :return: (str, float): (current state description, current state value)
        """
        return "N/A", 0

    @classmethod
    def get_is_cryptocurrency_wildcard(cls) -> bool:
        """
        :return: True if the mode is not cryptocurrency dependant else False
        """
        return True

    @classmethod
    def get_is_symbol_wildcard(cls) -> bool:
        """
        :return: True if the mode is not symbol dependant else False
        """
        return True

    @classmethod
    def get_is_time_frame_wildcard(cls) -> bool:
        """
        :return: True if the mode is not time_frame dependant else False
        """
        return True

    @classmethod
    def get_supported_exchange_types(cls) -> list:
        """
        :return: The list of supported exchange types
        """
        return [
            enums.ExchangeTypes.SPOT
        ]

    def should_emit_trading_signals_user_input(self, inputs: dict):
        if self.user_input(
            common_constants.CONFIG_EMIT_TRADING_SIGNALS, common_enums.UserInputTypes.BOOLEAN, False, inputs,
            title="Emit trading signals on Astrolab for people to follow.", order=998
        ):
            self.user_input(
                common_constants.CONFIG_TRADING_SIGNALS_STRATEGY, common_enums.UserInputTypes.TEXT, self.get_name(),
                inputs,
                title="Name of the strategy to send signals on.", order=999, other_schema_values={"minLength": 0}
            )

    def is_trading_signal_emitter(self) -> bool:
        """
        :return: True if the mode should be emitting trading signals according to configuration
        """
        try:
            return self.trading_config[common_constants.CONFIG_EMIT_TRADING_SIGNALS]
        except KeyError:
            return False

    def should_emit_trading_signal(self) -> bool:
        """
        :return: True if the mode should be emitting trading signals according to configuration and trading environment
        """
        return not self.exchange_manager.is_backtesting and self.is_trading_signal_emitter()

    def get_trading_signal_identifier(self) -> str:
        """
        :return: The identifier of the trading signal from config or the name of the tentacle if missing
        """
        try:
            return self.trading_config[common_constants.CONFIG_TRADING_SIGNALS_STRATEGY] or self.get_name()
        except KeyError:
            return self.get_name()

    @classmethod
    def get_is_trading_on_exchange(cls, exchange_name,
                                   tentacles_setup_config: tm_configuration.TentaclesSetupConfiguration) -> bool:
        """
        :return: When returning false, the associated exchange_manager.is_trading will be set to false, which will
        prevent the initialization of trade related elements. Default is True
        """
        return True

    @classmethod
    def get_parent_trading_mode_classes(cls, higher_parent_class_limit=None) -> list:
        return [
            class_type
            for class_type in cls.mro()
            if (higher_parent_class_limit if higher_parent_class_limit else AbstractTradingMode) in class_type.mro()
        ]

    @staticmethod
    def is_backtestable() -> bool:
        """
        Should be overwritten
        :return: True if the TradingMode can be used in a backtesting else False
        """
        return True

    async def initialize(self) -> None:
        """
        Triggers producers and consumers creation
        """
        await self.reload_config(self.exchange_manager.bot_id)
        await databases.RunDatabasesProvider.instance().get_run_databases_identifier(self.exchange_manager.bot_id)\
            .initialize(self.exchange_manager.exchange_name)
        await self.save_exchange_init_data()
        self.producers = await self.create_producers()
        self.consumers = await self.create_consumers()

    async def stop(self) -> None:
        """
        Stops all producers and consumers
        """
        if self.exchange_manager.is_backtesting:
            await self.save_backtesting_data()
        for producer in self.producers:
            await producer.stop()
        for consumer in self.consumers:
            await consumer.stop()
        self.exchange_manager = None

    async def create_producers(self) -> list:
        """
        Creates the instance of producers listed in MODE_PRODUCER_CLASSES
        :return: the list of producers created
        """
        return [
            await self._create_mode_producer(mode_producer_class)
            for mode_producer_class in self.MODE_PRODUCER_CLASSES
        ]

    async def _create_mode_producer(self, mode_producer_class):
        """
        Creates a new :mode_producer_class: instance and starts it
        :param mode_producer_class: the trading mode producer class to create
        :return: the producer class created
        """
        mode_producer = mode_producer_class(
            exchanges_channel.get_chan(constants.MODE_CHANNEL, self.exchange_manager.id),
            self.config, self, self.exchange_manager)
        await mode_producer.run()
        return mode_producer

    async def create_consumers(self) -> list:
        """
        Creates the instance of consumers listed in MODE_CONSUMER_CLASSES
        :return: the list of consumers created
        """
        base_consumers = [
            await self._create_mode_consumer(mode_consumer_class)
            for mode_consumer_class in self.MODE_CONSUMER_CLASSES
        ]
        return base_consumers + await self._add_temp_consumers()

    # TODO remove when proper run storage strategy
    async def _add_temp_consumers(self):
        consumers = []
        if not self.exchange_manager.is_backtesting:
            async def _trades_callback(
                    _self,
                    exchange: str,
                    exchange_id: str,
                    cryptocurrency: str,
                    symbol: str,
                    trade: dict,
                    old_trade: bool,
            ):
                if trade[enums.ExchangeConstantsOrderColumns.STATUS.value] != enums.OrderStatus.CANCELED.value:
                    db = databases.RunDatabasesProvider.instance().get_trades_db(_self.bot_id,
                                                                                 _self.exchange_manager.exchange_name)
                    await basic_keywords.store_trade(None, trade, exchange_manager=_self.exchange_manager, writer=db)
            consumers.append(
                await exchanges_channel.get_chan(channels_name.OctoBotTradingChannelsName.TRADES_CHANNEL.value,
                                                 self.exchange_manager.id).new_consumer(
                    _trades_callback,
                    symbol=self.symbol
                )
            )
        return consumers

    async def save_transactions(self):
        await basic_keywords.store_transactions(
            self.exchange_manager,
            self.exchange_manager.exchange_personal_data.transactions_manager.transactions.values()
        )

    async def save_trades(self):
        await basic_keywords.store_trades(
            self.exchange_manager,
            self.exchange_manager.exchange_personal_data.trades_manager.trades.values()
        )

    async def save_portfolio(self):
        await basic_keywords.store_portfolio(self.exchange_manager)

    async def save_live_metadata(self):
        await basic_keywords.save_metadata(
            databases.RunDatabasesProvider.instance().get_run_db(self.exchange_manager.bot_id),
            await self.get_live_metadata()
        )

    async def save_exchange_init_data(self):
        await self.save_portfolio()
        await self.save_live_metadata()

    # END TODO remove when proper run storage strategy

    async def _create_mode_consumer(self, mode_consumer_class):
        """
        Creates a new :mode_consumer_class: instance and subscribe this new consumer to the trading mode channel
        :param mode_consumer_class: the trading mode consumer class to create
        :return: the consumer class created
        """
        mode_consumer = mode_consumer_class(self)
        await exchanges_channel.get_chan(constants.MODE_CHANNEL, self.exchange_manager.id).new_consumer(
            consumer_instance=mode_consumer,
            trading_mode_name=self.get_name(),
            cryptocurrency=self.cryptocurrency if self.cryptocurrency else channel_constants.CHANNEL_WILDCARD,
            symbol=self.symbol if self.symbol else channel_constants.CHANNEL_WILDCARD,
            time_frame=self.time_frame if self.time_frame else channel_constants.CHANNEL_WILDCARD)
        return mode_consumer

    def load_config(self) -> None:
        """
        Try to load TradingMode tentacle config.
        Calls set_default_config() if the tentacle config is empty
        """
        # try with this class name
        self.trading_config = tentacles_manager_api.get_tentacle_config(self.exchange_manager.tentacles_setup_config,
                                                                        self.__class__)

        # set default config if nothing found
        if not self.trading_config:
            self.set_default_config()

    LIGHT_VOLUME_WEIGHT = "light_weight_volume_multiplier"
    MEDIUM_VOLUME_WEIGHT = "medium_weight_volume_multiplier"
    HEAVY_VOLUME_WEIGHT = "heavy_weight_volume_multiplier"
    VOLUME_WEIGH_TO_VOLUME_PERCENT = {}

    LIGHT_PRICE_WEIGHT = "light_weight_price_multiplier"
    MEDIUM_PRICE_WEIGHT = "medium_weight_price_multiplier"
    HEAVY_PRICE_WEIGHT = "heavy_weight_price_multiplier"

    async def reload_config(self, bot_id: str) -> None:
        """
        Try to load TradingMode tentacle config.
        Calls set_default_config() if the tentacle config is empty
        """
        self.trading_config = tentacles_manager_api.get_tentacle_config(self.exchange_manager.tentacles_setup_config,
                                                                        self.__class__)
        # set default config if nothing found
        if not self.trading_config:
            self.set_default_config()
        await self.load_and_save_user_inputs(bot_id)
        for element in self.consumers + self.producers:
            if isinstance(element, (abstract_mode_consumer.AbstractTradingModeConsumer,
                                    abstract_mode_producer.AbstractTradingModeProducer)):
                element.reload_config()

    def get_local_config(self):
        return self.trading_config

    @classmethod
    def create_local_instance(cls, config, tentacles_setup_config, tentacle_config):
        return modes_factory.create_temporary_trading_mode_with_local_config(
            cls, config, tentacle_config
        )

    # to implement in subclasses if config is necessary
    def set_default_config(self) -> None:
        pass

    """
    Strategy related methods
    """

    @classmethod
    def get_required_strategies_names_and_count(cls,
                                                tentacles_config: tm_configuration.TentaclesSetupConfiguration,
                                                trading_mode_config=None):
        config = trading_mode_config or tentacles_manager_api.get_tentacle_config(tentacles_config, cls)
        if constants.TRADING_MODE_REQUIRED_STRATEGIES in config:
            return config[constants.TRADING_MODE_REQUIRED_STRATEGIES], cls.get_required_strategies_count(config)
        raise Exception(f"'{constants.TRADING_MODE_REQUIRED_STRATEGIES}' is missing in configuration file")

    @classmethod
    def get_default_strategies(cls,
                               tentacles_config: tm_configuration.TentaclesSetupConfiguration,
                               trading_mode_config=None):
        config = trading_mode_config or tentacles_manager_api.get_tentacle_config(tentacles_config, cls)
        if common_constants.TENTACLE_DEFAULT_CONFIG in config:
            return config[common_constants.TENTACLE_DEFAULT_CONFIG]

        strategies_classes, _ = cls.get_required_strategies_names_and_count(tentacles_config, config)
        return strategies_classes

    @classmethod
    def get_required_strategies_count(cls, config):
        min_strategies_count = 1
        if constants.TRADING_MODE_REQUIRED_STRATEGIES_MIN_COUNT in config:
            min_strategies_count = config[constants.TRADING_MODE_REQUIRED_STRATEGIES_MIN_COUNT]
        return min_strategies_count

    @classmethod
    def get_required_candles_count(cls, tentacles_setup_config: tm_configuration.TentaclesSetupConfiguration):
        return tentacles_manager_api.get_tentacle_config(tentacles_setup_config, cls).get(
            constants.CONFIG_CANDLES_HISTORY_SIZE_KEY,
            common_constants.DEFAULT_IGNORED_VALUE
        )

    @classmethod
    async def get_backtesting_plot(cls, exchange, symbol, backtesting_id, optimizer_id,
                                   optimization_campaign, backtesting_analysis_settings):
        try:
            import tentacles.Meta.Keywords.scripting_library as scripting_library
            ctx = context_management.Context.minimal(cls, logging.get_logger(cls.get_name()), exchange, symbol,
                                                     backtesting_id, optimizer_id,
                                                     optimization_campaign, backtesting_analysis_settings)
            return await scripting_library.default_backtesting_analysis_script(ctx)
        except ImportError:
            raise ImportError("scripting_library keywords are required")

    @contextlib.asynccontextmanager
    async def remote_signal_publisher(self, symbol: str):
        if self.should_emit_trading_signal():
            try:
                async with signals.SignalPublisher.instance().remote_signal_bundle_builder(
                    symbol,
                    self.get_trading_signal_identifier(),
                    self.TRADING_SIGNAL_TIMEOUT,
                    signals.TradingSignalBundleBuilder,
                    (self.get_name(), )
                ) as signal_builder:
                    yield signal_builder
            except (authentication.AuthenticationRequired, authentication.UnavailableError) as e:
                self.logger.exception(e, True, f"Failed to send trading signals: {e}")
        else:
            yield None

    async def create_order(self, order, loaded: bool = False, params: dict = None, pre_init_callback=None):
        order_pf_percent = f"0{script_keywords.QuantityType.PERCENT.value}"
        if self.should_emit_trading_signal():
            percent = await orders.get_order_size_portfolio_percent(
                self.exchange_manager,
                order.origin_quantity,
                order.side,
                order.symbol
            )
            order_pf_percent = f"{float(percent)}{script_keywords.QuantityType.PERCENT.value}"
        created_order = await self.exchange_manager.trader.create_order(
            order, loaded=loaded, params=params, pre_init_callback=pre_init_callback
        )
        if created_order is not None and self.should_emit_trading_signal():
            signals.SignalPublisher.instance().get_signal_bundle_builder(order.symbol).add_created_order(
                    created_order, self.exchange_manager, target_amount=order_pf_percent
                )
        return created_order

    async def cancel_order(self, order, ignored_order: object = None) -> bool:
        cancelled = await self.exchange_manager.trader.cancel_order(order, ignored_order=ignored_order)
        if self.should_emit_trading_signal() and cancelled:
            signals.SignalPublisher.instance().get_signal_bundle_builder(order.symbol).add_cancelled_order(
                order, self.exchange_manager
            )
        return cancelled

    async def edit_order(self, order,
                         edited_quantity: decimal.Decimal = None,
                         edited_price: decimal.Decimal = None,
                         edited_stop_price: decimal.Decimal = None,
                         edited_current_price: decimal.Decimal = None,
                         params: dict = None) -> bool:
        changed = await self.exchange_manager.trader.edit_order(
            order,
            edited_quantity=edited_quantity,
            edited_price=edited_price,
            edited_stop_price=edited_stop_price,
            edited_current_price=edited_current_price,
            params=params
        )
        if self.should_emit_trading_signal() and changed:
            signals.SignalPublisher.instance().get_signal_bundle_builder(order.symbol).add_edited_order(
                order,
                self.exchange_manager,
                updated_target_amount=edited_quantity,
                updated_limit_price=edited_price,
                updated_stop_price=edited_stop_price,
                updated_current_price=edited_current_price,
            )
        return changed

    async def save_backtesting_data(self):
        if not self.are_metadata_saved and self.exchange_manager is not None:
            run_dbs_identifier = databases.RunDatabasesProvider.instance().get_run_databases_identifier(
                self.exchange_manager.bot_id
            )
            run_data_writer = databases.RunDatabasesProvider.instance().get_run_db(self.exchange_manager.bot_id)
            await run_data_writer.flush()
            user_inputs = await commons_configuration.get_user_inputs(run_data_writer)

            # TODO remove when proper run storage strategy
            await self.save_transactions()
            await self.save_trades()
            # END TODO

            if not self.__class__.SAVED_RUN_METADATA_DB_BY_BOT_ID.get(self.exchange_manager.bot_id, False):
                try:
                    self.__class__.SAVED_RUN_METADATA_DB_BY_BOT_ID[self.exchange_manager.bot_id] = True
                    async with databases.DBWriter.database(
                            run_dbs_identifier.get_backtesting_metadata_identifier(),
                            with_lock=True) as writer:
                        await basic_keywords.save_metadata(writer, await self.get_backtesting_metadata(
                            user_inputs,
                            run_dbs_identifier
                        ))
                        self.are_metadata_saved = True
                except Exception:
                    self.__class__.SAVED_RUN_METADATA_DB_BY_BOT_ID[self.exchange_manager.bot_id] = False
                    raise

    async def get_live_metadata(self):
        start_time = backtesting_api.get_backtesting_starting_time(self.exchange_manager.exchange.backtesting) \
            if trading_api.get_is_backtesting(self.exchange_manager) \
            else trading_api.get_exchange_current_time(self.exchange_manager)
        end_time = backtesting_api.get_backtesting_ending_time(self.exchange_manager.exchange.backtesting) \
            if trading_api.get_is_backtesting(self.exchange_manager) \
            else -1
        exchange_type = "spot"
        exchange_names = [
            exchange
            for exchange, config in self.config[common_constants.CONFIG_EXCHANGES].items()
            if config.get(common_constants.CONFIG_ENABLED_OPTION, True)
        ]
        future_contracts_by_exchange = {}
        if self.exchange_manager.is_future and hasattr(self.exchange_manager.exchange, "pair_contracts"):
            exchange_type = "future"
            future_contracts_by_exchange = {
                self.exchange_manager.exchange_name: {
                    symbol: {
                        "contract_type": contract.contract_type.value,
                        "position_mode": contract.position_mode.value,
                        "margin_type": contract.margin_type.value
                    }
                    for symbol, contract in self.exchange_manager.exchange.pair_contracts.items()
                    if symbol in self.exchange_manager.exchange_config.traded_symbol_pairs
                }
            }
        return {
            **{
                common_enums.DBRows.REFERENCE_MARKET.value: trading_api.get_reference_market(self.config),
                common_enums.DBRows.START_TIME.value: start_time,
                common_enums.DBRows.END_TIME.value: end_time,
                common_enums.DBRows.TRADING_TYPE.value: exchange_type,
                common_enums.DBRows.EXCHANGES.value: exchange_names,
                common_enums.DBRows.FUTURE_CONTRACTS.value: future_contracts_by_exchange,
                common_enums.DBRows.SYMBOLS.value: self.exchange_manager.exchange_config.traded_symbol_pairs,
            },
            **(await self.get_additional_backtesting_metadata())
        }

    async def get_backtesting_metadata(self, user_inputs, run_dbs_identifier) -> dict:
        """
        Override this method to get add addition metadata
        :return: the metadata dict related to this backtesting run
        """
        symbols = trading_api.get_trading_pairs(self.exchange_manager)
        profitability, profitability_percent, _, _, _ = trading_api.get_profitability_stats(self.exchange_manager)
        origin_portfolio = portfolios.portfolio_to_float(
            self.exchange_manager.exchange_personal_data.portfolio_manager.
            portfolio_value_holder.origin_portfolio.portfolio)
        end_portfolio = portfolios.portfolio_to_float(
            self.exchange_manager.exchange_personal_data.portfolio_manager.portfolio.portfolio)
        for portfolio in (origin_portfolio, end_portfolio):
            for values in portfolio.values():
                values.pop("available", None)
        if self.exchange_manager.is_future:
            for position in self.exchange_manager.exchange_personal_data.positions_manager.positions.values():
                end_portfolio[position.get_currency()]["position"] = float(position.quantity)
        time_frames = [
            tf.value
            for tf in trading_api.get_exchange_available_required_time_frames(
                self.exchange_manager.exchange_name,
                self.exchange_manager.id
            )
        ]
        formatted_user_inputs = {}
        for user_input in user_inputs:
            if not user_input["is_nested_config"]:
                try:
                    formatted_user_inputs[user_input["tentacle"]][user_input["name"]] = user_input["value"]
                except KeyError:
                    formatted_user_inputs[user_input["tentacle"]] = {
                        user_input["name"]: user_input["value"]
                    }
        leverage = 0
        if self.exchange_manager.is_future and hasattr(self.exchange_manager.exchange, "get_pair_future_contract"):
            leverage = float(self.exchange_manager.exchange.get_pair_future_contract(symbols[0]).current_leverage)
        trades = trading_api.get_trade_history(self.exchange_manager)
        entries = [
            trade
            for trade in trades
            if trade.status is enums.OrderStatus.FILLED and trade.side is enums.TradeOrderSide.BUY
        ]
        win_rate = round(float(trading_api.get_win_rate(self.exchange_manager) * 100), 3)
        wins = round(win_rate * len(entries) / 100)
        draw_down = round(float(trading_api.get_draw_down(self.exchange_manager)), 3)
        r_sq_end_balance = await trading_api.get_coefficient_of_determination(
            self.exchange_manager,
            use_high_instead_of_end_balance=False
        )
        r_sq_max_balance = await trading_api.get_coefficient_of_determination(self.exchange_manager)

        return {
            **{
                common_enums.BacktestingMetadata.OPTIMIZATION_CAMPAIGN.value:
                    run_dbs_identifier.optimization_campaign_name,
                common_enums.BacktestingMetadata.ID.value: run_dbs_identifier.backtesting_id,
                common_enums.BacktestingMetadata.GAINS.value: round(float(profitability), 8),
                common_enums.BacktestingMetadata.PERCENT_GAINS.value: round(float(profitability_percent), 3),
                common_enums.BacktestingMetadata.END_PORTFOLIO.value: str(end_portfolio),
                common_enums.BacktestingMetadata.START_PORTFOLIO.value: str(origin_portfolio),
                common_enums.BacktestingMetadata.WIN_RATE.value: win_rate,
                common_enums.BacktestingMetadata.DRAW_DOWN.value: draw_down or 0,
                common_enums.BacktestingMetadata.COEFFICIENT_OF_DETERMINATION_MAX_BALANCE.value: r_sq_max_balance or 0,
                common_enums.BacktestingMetadata.COEFFICIENT_OF_DETERMINATION_END_BALANCE.value: r_sq_end_balance or 0,
                common_enums.BacktestingMetadata.SYMBOLS.value: symbols,
                common_enums.BacktestingMetadata.TIME_FRAMES.value: time_frames,
                common_enums.BacktestingMetadata.START_TIME.value: backtesting_api.get_backtesting_starting_time(
                    self.exchange_manager.exchange.backtesting),
                common_enums.BacktestingMetadata.END_TIME.value: backtesting_api.get_backtesting_ending_time(
                    self.exchange_manager.exchange.backtesting),
                common_enums.BacktestingMetadata.DURATION.value: round(backtesting_api.get_backtesting_duration(
                    self.exchange_manager.exchange.backtesting), 3),
                common_enums.BacktestingMetadata.ENTRIES.value: len(entries),
                common_enums.BacktestingMetadata.WINS.value: wins,
                common_enums.BacktestingMetadata.LOSES.value: len(entries) - wins,
                common_enums.BacktestingMetadata.TRADES.value: len(trades),
                common_enums.BacktestingMetadata.TIMESTAMP.value: self.start_time,
                common_enums.BacktestingMetadata.NAME.value: self.get_name(),
                common_enums.BacktestingMetadata.LEVERAGE.value: leverage,
                common_enums.BacktestingMetadata.USER_INPUTS.value: formatted_user_inputs,
                common_enums.BacktestingMetadata.BACKTESTING_FILES.value: trading_api.get_backtesting_data_files(
                    self.exchange_manager),
                common_enums.BacktestingMetadata.EXCHANGE.value: self.exchange_manager.exchange_name
            },
            **(await self.get_additional_backtesting_metadata())
        }

    async def get_additional_live_metadata(self):
        """
        Override if necessary
        """
        return {}

    async def get_additional_backtesting_metadata(self):
        """
        Override if necessary
        """
        return {}
