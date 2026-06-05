"""FSM state groups for multi-step flows."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class BuyFlow(StatesGroup):
    searching = State()


class WalletFlow(StatesGroup):
    custom_amount = State()


class AdminFlow(StatesGroup):
    give = State()
    markup = State()
    bid = State()
    esim_comm = State()
    finduser = State()
    broadcast = State()
    channelpost = State()
