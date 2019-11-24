import time
import asyncio
import traceback

from .strategy_interface import strategy_interface
from orders_manager import orders_manager

from logger import logging

from definitions import (
    tob,
    order_request,
    order_type,
    order_side,
    exchange_orders,
    new_order_ack,
    amend_ack,
    new_order_nack,
    amend_nack,
    order_fill_ack,
    order_full_fill_ack,
)


class market_maker(strategy_interface):
    TIME_TO_WAIT_SINCE_START_SECS = 10
    MAX_NUMBER_OF_ATTEMPTS_SECS = 5

    def __init__(self, cfg, exchange_adapter):
        self.logger = logging.getLogger()

        self._load_configuration(cfg)

        self.config = cfg
        self.exchange_adapter = exchange_adapter
        self.exchange_adapter.set_order_update_callback(self.on_market_update)
        self.orders_manager = orders_manager(self.exchange_adapter)

        if self.cancel_orders_on_start is True:
            self.exchange_adapter.cancel_orders_on_start = True
        else:
            self.exchange_adapter.cancel_orders_on_start = False

        self.update_orders = False

        self.started_time = time.time()
        self.last_amend_time = None
        self.reconnecting = False

        self.active = True
        self.tob = None
        self.num_of_sent_orders = 0
        self.cancel_all_request_was_sent = False

        self.user_asks = self.config.orders.asks
        self.user_bids = self.config.orders.bids

    def _load_configuration(self, cfg):
        option_names = (
            "instrument_name",
            "mid_price_based_calculation",
            "tick_size",
            "price_rounding",
            "cancel_orders_on_start",
            "stop_strategy_on_error",
            "cancel_orders_on_reconnection",
        )
        for option_name in option_names:
            option = getattr(cfg, option_name)
            if option is None:
                self.logger.error("%s was not found", option_name)
                raise Exception("{0} was not found".format(option_name))
            setattr(self, option_name, option)

    async def handle_exception(self, err_msg):
        self.logger.error("handle_exception traceback: {}".format(err_msg))
        for line in traceback.format_stack():
            self.logger.error(line.strip())

        stack_str = traceback.format_stack()
        self.logger.error("additional handle_exception traceback: {}".format(stack_str))

        count = 0
        while count < 5:
            try:
                await self._handle_exception(err_msg, self.stop_strategy_on_error)
                return True
            except Exception as err:
                self.logger.exception("Exception raised {}".format(err))
                err_msg = err

            count += 1
            self.logger.warning("reconnection failed, performing new attempt")
        raise Exception("{}, handle_exception was unsuccessfully tried 5 times".format(get_filename_and_lineno()))

    async def _handle_exception(self, err_msg, stop_strategy):
        self.logger.warning("Gateway will be reconnected because of {}".format(err_msg))
        if stop_strategy is True:
            await self.stop_strategy()

        self.reconnecting = True

        await self.reset(err_msg)
        self.started_time = time.time()

        self.reconnecting = False

        self.logger.warning("Gateway was reconnected because of {}".format(err_msg))
        return True

    async def stop_strategy(self):
        try:
            self.logger.info("Cancelling orders because strategy is stopped")
            await self._cancel_orders()
        except Exception as err:
            self.logger.warning("stop_strategy failed on {}".format(err))
            raise

        self.active = False


    async def reset(self, reset_reason):
        self.cancel_all_request_was_sent = False

        if self.cancel_orders_on_reconnection:
            await self._cancel_orders()
            self.cancel_all_request_was_sent = True
            self.last_amend_time = None
            self.num_of_sent_orders = 0
        await self.exchange_adapter.reconnect()


    async def _cancel_orders(self):
        try:
            await self.orders_manager.cancel_active_orders()
        except Exception as err:
            res = await self.handle_exception(err)
            if res is False:
                self.logger.exception("_cancel_orders, msg {}".format(err))
                raise Exception("_cancel_orders, msg {}".format(err))
            return

    async def process_active_orders_on_start(self, orders_msg):
        if len(orders_msg.bids + orders_msg.asks) == 0 or self.cancel_orders_on_start is True:
            return
        elif len(orders_msg.bids + orders_msg.asks) % 2 != 0:
            await self._cancel_orders()
            return

        self.orders_manager.activate_orders(orders_msg)

    async def on_market_update(self, update):
        if self.active is False:
            self.logger.info("Strategy is not active, update will be ignored")
            return
        elif isinstance(update, tob):
            if self.tob is None:
                self.update_orders = True
                self.tob = update
            elif self.tob_moved(update):
                self.update_orders = True
                self.tob = update
            return
        elif isinstance(update, exchange_orders):
            await self.process_active_orders_on_start(update)
            return
        elif isinstance(update, (amend_nack, new_order_nack)):
            self.logger.info("Received order nack {}".format(update.__dict__))

        try:
            self.orders_manager.update_order_state(update.orderid, update)
        except Exception as err:
            self.logger.error("update_order_state failed on {}".format(update))
            raise Exception("on_market_update raised. update = {}, reason = {}".format(
                type(update), str(err)))

    async def run(self):
        if self.active is False:
            self.logger.info("Strategy is not active, method run will be stopped")
            return
        elif self.tob is None:
            return
        elif self.update_orders is False:
            return
        elif self.started_time + self.TIME_TO_WAIT_SINCE_START_SECS > time.time():
            return

        self.update_orders = False
        await self.process_market_move()

    def tob_moved(self, tob):
        if self.tob.best_bid_price != tob.best_bid_price or self.tob.best_ask_price != tob.best_ask_price:
            return True
        return False

    def _orders_are_ready_for_amend(self):
        known_statuses = self.orders_manager.get_number_of_ready_for_amend()
        if self.last_amend_time and len(
                self.orders_manager.live_orders_ids) > 0 and known_statuses != self.num_of_sent_orders:
            return known_statuses
        return True

    def generate_orders(self):
        best_ask, best_bid = self.tob.best_ask_price, self.tob.best_bid_price
        if self.mid_price_based_calculation:
            mid_price = (self.tob.best_ask_price + self.tob.best_bid_price) / 2.0
            rounded_mid = round(round(mid_price / self.tick_size) * self.tick_size,
                                self.price_rounding)

            if best_ask - best_bid == 2.0 * self.tick_size:
                best_ask = round(rounded_mid + self.tick_size, self.price_rounding)
                best_bid = round(rounded_mid - self.tick_size, self.price_rounding)
            elif rounded_mid >= mid_price:
                best_ask = rounded_mid
                best_bid = round(best_ask - self.tick_size, self.price_rounding)
            else:
                best_bid = rounded_mid
                best_ask = round(best_bid + self.tick_size, self.price_rounding)

        orders = []
        for quote in self.user_asks:
            level, qty = quote
            order = order_request()
            order.instrument_name = self.config.instrument_name
            order.side = order_side.sell
            order.type = order_type.limit

            order.price = round(best_ask + self.tick_size * level, self.price_rounding)
            order.quantity = qty
            orders.append(order)

        for quote in self.user_bids:
            level, qty = quote
            order = order_request()
            order.instrument_name = self.config.instrument_name
            order.side = order_side.buy
            order.type = order_type.limit

            order.price = round(best_bid - self.tick_size * level, self.price_rounding)
            order.quantity = qty
            orders.append(order)
        return orders

    async def process_market_move(self):
        if self.active is False:
            self.logger.info("Strategy is not active, process_market_move will be stopped")
            return
        elif self.reconnecting is True:
            self.logger.info("Ongoing reconnection, process_market_move will be stopped")
            return

        self.logger.info("process_market_move started")

        res = self._orders_are_ready_for_amend()
        if res is not True:

            self.logger.info("_orders_are_ready_for_amend returned False")

            known_statuses = res
            if self.last_amend_time + self.MAX_NUMBER_OF_ATTEMPTS_SECS < time.time():
                err_msg = (
                    "Will be reconnected since only {} "
                    "active orders were updated within {} seconds".format(
                        known_statuses,
                        self.MAX_NUMBER_OF_ATTEMPTS_SECS
                    )
                )

                res = await self.handle_exception(err_msg)
                if res is False:
                    self.logger.log("Error: %s", err_msg)
                    raise Exception("handle_exception failed")
                return
            return

        orders = self.generate_orders()

        try:
            await self.orders_manager.amend_active_orders(orders)
        except Exception as err:
            res = await self.handle_exception(err)
            if res is False:
                self.logger.exception("Exception")
                raise GatewayError("Orders amend failed {}".format(err))
            return

        self.last_amend_time = time.time()
        self.num_of_sent_orders = len(orders)
