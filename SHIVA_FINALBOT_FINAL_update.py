#!/usr/bin/env python3
"""
Expert-Level Crypto Futures Signal Bot for Binance USDT-M Futures

Features:
- Scans Binance USDT-M futures every 5 minutes, focusing on small-cap coins whitelist
- Dual Modes:
  - Auto mode: signals sent to all users, default settings (SL=1%, TP1=1%, TP2=2%, TP3=3%, TP4=4%)
  - Custom mode: per-user settings for leverage, investment, acceptable loss, signals filtered accordingly
- Signals sent via Telegram with pair, entry, SL, TP1-4, direction, timeframe
- Reliable retry mechanism for failed signal deliveries with fallback notification
- User commands: /start, /mode, /settings, /status, /stop
- Admin commands: /admin_view, /ban, /unban, /toggle_bot
- Fully async, uses python-telegram-bot v20+ async API
- No private Binance keys, uses public endpoints only
- Designed for easy deployment on Replit, Render, PythonAnywhere

Author: SHIVA BHARDWAJ
"""

import asyncio
import logging
import os
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timedelta

import ccxt.async_support as ccxt
import pandas as pd
import numpy as np

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "7874987699:AAG-0AVNeXlaP59KKApcTcF2YIAu8LVQaVo"


# === Constants ===

SCAN_INTERVAL = 300  # 5 minutes in seconds
SMALL_CAP_WHITELIST = {
    "FET/USDT",
    "HBAR/USDT",
    "WRX/USDT",
    "BTT/USDT",
    "CHR/USDT",
    "ONE/USDT",
    "ANKR/USDT",
    "HOT/USDT",
    "STMX/USDT",
    "RVN/USDT",
    "LINA/USDT",
    "HUMA/USDT",
    "HOOK/USDT",
    "GALA/USDT",
    "ALICE/USDT",
    "MTL/USDT",
    "CTSI/USDT",
    "FLM/USDT",
    "NKN/USDT",
    "SFP/USDT",
    "CVC/USDT",
    "BEL/USDT",
    "TLM/USDT",
    "VITE/USDT",
    "KEY/USDT",
    "PEOPLE/USDT",
    "IDEX/USDT",
    "BLZ/USDT",
    "AMB/USDT",
    "DEGO/USDT",
    "LIT/USDT",
    "DREP/USDT",
    "ERN/USDT",
    "RIF/USDT",
    "QKC/USDT",
    "TRU/USDT",
    "GRASS/USDT",
    "LONG/USDT",
    "FLOKI/USDT",
    "MDT/USDT",
    "PHB/USDT",
    "FARM/USDT",
    "DODO/USDT",
    "DAR/USDT",
    "VRA/USDT",
    "FORTH/USDT",
    "FRONT/USDT",
    "PERL/USDT"
}
TIMEFRAME = "5m"

# Conversation states for custom mode settings
(
    STATE_CUSTOM_LEVERAGE,
    STATE_CUSTOM_INVESTMENT,
    STATE_CUSTOM_ACCEPTABLE_LOSS,
) = range(3)


# === Global Variables ===

# user_id: {
#   mode: 'auto' or 'custom',
#   leverage: int or None,
#   investment: float or None,
#   acceptable_loss: float % or None,
#   banned: bool
# }
authorized_users: Dict[int, Dict] = {}

admin_user_ids = {1129607634}

bot_enabled = True

# last signal per symbol: 'LONG' or 'SHORT'
last_signals: Dict[str, str] = {}

# Retry queue: list of dicts with keys: user_id, signal_data, attempts, last_attempt_time
retry_queue: List[Dict] = []

# ccxt Binance future client instance
binance_client = None


# === Helper Classes & Functions ===

class BinanceFuturesClient:
    def __init__(self):
        self.exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
                "defaultMarket": "futures",
                "recvWindow": 10000,
            },
        })

    async def load_usdt_futures_symbols(self) -> List[str]:
        try:
            print("⏳ Binance se symbols load karne ki koshish ho rahi hai...") 
            markets = await self.exchange.load_markets()
            print(f"✅ Binance markets loaded: {len(markets)} total symbols")
            symbols = [s for s in markets if s.endswith("/USDT") and markets[s]["future"]]
            # Filter only small-cap whitelist
            print(f"✅ USDT Futures symbols found: {len(symbols)}")
            filtered_symbols = [s for s in symbols if s in SMALL_CAP_WHITELIST]
            # If whitelist empty or no match, fallback to all USDT futures
            return filtered_symbols if filtered_symbols else symbols
        except Exception as e:
            logger.error(f"Error loading symbols: {e}")
            return []

    async def fetch_ohlcv(self, symbol: str, timeframe: str = TIMEFRAME, limit: int = 100) -> pd.DataFrame:
        try:
            data = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return pd.DataFrame()

    async def close(self):
        if self.exchange:
            await self.exchange.close()


def calculate_ma_signal(df: pd.DataFrame) -> Optional[str]:
    """
    Calculate signal using 5EMA and 20EMA crossover on close prices:
    Return 'LONG' if buy signal, 'SHORT' if sell signal, else None.
    """
    if df.empty or len(df) < 20:
        return None

    df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()

    ema5_current = df['ema5'].iloc[-1]
    ema20_current = df['ema20'].iloc[-1]
    ema5_prev = df['ema5'].iloc[-2]
    ema20_prev = df['ema20'].iloc[-2]

    if ema5_prev < ema20_prev and ema5_current > ema20_current:
        return 'LONG'
    if ema5_prev > ema20_prev and ema5_current < ema20_current:
        return 'SHORT'
    return None


def calculate_trade_levels(
    signal: str,
    entry_price: float,
    acceptable_loss_pct: float,
    tp_levels: int = 4,
) -> Tuple[float, List[float]]:
    """
    Calculate SL and 1-4 TP levels based on signal direction and acceptable loss.
    SL is set strictly by acceptable loss.
    TP levels follow multiples of acceptable_loss_pct or fixed scalping levels.
    """

    if signal == 'LONG':
        stop_loss = entry_price * (1 - acceptable_loss_pct / 100)
        targets = [
            entry_price * (1 + 0.01 * i) for i in range(1, tp_levels + 1)
        ]  # 1%, 2%, 3%, 4%
    else:  # SHORT
        stop_loss = entry_price * (1 + acceptable_loss_pct / 100)
        targets = [
            entry_price * (1 - 0.01 * i) for i in range(1, tp_levels + 1)
        ]

    return stop_loss, targets


def format_signal_message(signal_data: Dict) -> str:
    """
    Format the signal information in MarkdownV2 for Telegram
    """
    def fmt_val(v): return f"{v:.4f}"

    tp_lines = "\n".join(
        f" - TP{i+1}: `{fmt_val(tp)}`" for i, tp in enumerate(signal_data['targets'])
    )

    msg = (
        f"*Crypto Futures Signal*\n"
        f"Pair: *{signal_data['symbol']}*\n"
        f"Direction: *{signal_data['signal']}*\n"
        f"Timeframe: `{signal_data['timeframe']}`\n"
        f"Entry Price: `{fmt_val(signal_data['entry'])}`\n"
        f"Stop Loss (SL): `{fmt_val(signal_data['stop_loss'])}`\n"
        f"{tp_lines}\n"
        f"\nLeverage: {signal_data['leverage']}x\n"
        f"Investment: {signal_data['investment']} USDT\n"
        f"Position Size (approx.): {signal_data['investment'] * signal_data['leverage']:.2f} USDT\n"
        f"\n_Risk management: Acceptable loss set to user's configured percentage._"
    )
    return msg


async def send_signal_to_user(app, user_id: int, signal_data: Dict, retry_attempt=False):
    """
    Send signal message to user, handle failure by adding to retry queue.
    If retry_attempt and failed again, send fallback message.
    """
    msg = format_signal_message(signal_data)
    try:
        await app.bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
        logger.info(f"Signal sent to user {user_id} for {signal_data['symbol']}")
        return True
    except Exception as e:
        logger.error(f"Failed to send signal to user {user_id}: {e}")
        if retry_attempt:
            # Send fallback message notifying user of missed signal
            fallback = (
                f"❌ Signal delivery failed due to Telegram/network issue.\n"
                f"Missed Signal: {signal_data['symbol']}, {signal_data['signal']}, "
                f"Entry: {signal_data['entry']:.4f}, Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            )
            try:
                await app.bot.send_message(user_id, fallback)
            except Exception as e2:
                logger.error(f"Also failed to send fallback message to user {user_id}: {e2}")
            return False
        else:
            # Add to retry queue
            retry_queue.append({
                'user_id': user_id,
                'signal_data': signal_data,
                'attempts': 1,
                'last_attempt_time': datetime.utcnow()
            })
            return False


async def retry_failed_signals(app):
    while True:
        now = datetime.utcnow()
        to_remove = []
        for i, item in enumerate(retry_queue):
            elapsed = (now - item['last_attempt_time']).total_seconds()
            if elapsed > 90:  # Retry after ~1.5 minutes
                success = await send_signal_to_user(app, item['user_id'], item['signal_data'], retry_attempt=True)
                if success or item['attempts'] >= 2:
                    to_remove.append(i)
                else:
                    item['attempts'] += 1
                    item['last_attempt_time'] = now
        # Remove successful or max attempt items from retry queue
        for idx in reversed(to_remove):
            retry_queue.pop(idx)
        await asyncio.sleep(30)  # Check retry queue every 30s


# === Telegram Command Handlers ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name
    if user_id not in authorized_users:
        authorized_users[user_id] = {
            "mode": None,
            "leverage": None,
            "investment": None,
            "acceptable_loss": None,
            "banned": False,
        }
    if authorized_users[user_id]['banned']:
        await update.message.reply_text("Sorry, you are banned from using this bot.")
        return
    await update.message.reply_text(
        f"Welcome {username} to Crypto Futures Signal Bot!\n\n"
        "Use /mode <auto|custom> to select your mode.\n"
        "Use /settings to configure your custom mode preferences.\n"
        "Use /status to see your current settings.\n"
        "Use /stop to unregister and stop receiving signals.\n"
        "Use /help for more commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "/start - Start & register\n"
        "/mode <auto|custom> - Select signal mode\n"
        "/settings - Configure settings for custom mode\n"
        "/status - View your current settings\n"
        "/stop - Unregister and stop signals\n"
        "\nAdmin Commands:\n"
        "/admin_view - View all users (admin only)\n"
        "/ban <user_id> - Ban a user (admin only)\n"
        "/unban <user_id> - Unban a user (admin only)\n"
        "/toggle_bot - Enable/disable bot globally (admin only)\n"
    )
    await update.message.reply_text(help_text)


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users or authorized_users[user_id]['banned']:
        await update.message.reply_text("You are banned or not registered. Use /start to register.")
        return

    args = context.args
    if not args or args[0].lower() not in ('auto', 'custom'):
        await update.message.reply_text("Usage: /mode auto OR /mode custom")
        return

    mode = args[0].lower()
    authorized_users[user_id]["mode"] = mode

    if mode == "auto":
        # Clear custom settings as not relevant
        authorized_users[user_id]["leverage"] = None
        authorized_users[user_id]["investment"] = None
        authorized_users[user_id]["acceptable_loss"] = None
        await update.message.reply_text(
            "Mode set to AUTO. You will receive signals with default settings."
        )
    else:
        await update.message.reply_text(
            "Mode set to CUSTOM. Please configure your settings now using /settings."
        )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users or authorized_users[user_id]['banned']:
        await update.message.reply_text("You are banned or not registered. Use /start to register.")
        return
    if authorized_users[user_id]["mode"] != "custom":
        await update.message.reply_text("You must set mode to custom first with /mode custom")
        return
    await update.message.reply_text("Enter desired leverage (1 to 125):")
    return STATE_CUSTOM_LEVERAGE


async def set_custom_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    txt = update.message.text.strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 125):
        await update.message.reply_text("Please enter a valid leverage between 1 and 125.")
        return STATE_CUSTOM_LEVERAGE
    authorized_users[user_id]["leverage"] = int(txt)
    await update.message.reply_text("Enter your investment amount in USDT (e.g. 50):")
    return STATE_CUSTOM_INVESTMENT


async def set_custom_investment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        val = float(update.message.text.strip())
        if not (10 <= val <= 100000):
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Investment must be a number between 10 and 100000 USDT.")
        return STATE_CUSTOM_INVESTMENT
    authorized_users[user_id]["investment"] = val
    await update.message.reply_text("Enter acceptable loss percentage (0.1 to 10):")
    return STATE_CUSTOM_ACCEPTABLE_LOSS


async def set_custom_acceptable_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        val = float(update.message.text.strip())
        if not (0.1 <= val <= 10):
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Acceptable loss must be a number between 0.1% and 10%.")
        return STATE_CUSTOM_ACCEPTABLE_LOSS
    authorized_users[user_id]["acceptable_loss"] = val
    u = authorized_users[user_id]
    await update.message.reply_text(
        f"Settings saved:\n- Leverage: {u['leverage']}x\n- Investment: {u['investment']} USDT\n- Acceptable Loss: {u['acceptable_loss']}%\n"
        "You will start receiving signals based on these settings."
    )
    return ConversationHandler.END


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("You are not registered. Use /start to register.")
        return
    u = authorized_users[user_id]
    if u.get("banned", False):
        await update.message.reply_text("You are banned from this bot.")
        return
    mode = u.get("mode", "Not set")
    leverage = u.get("leverage", "N/A")
    investment = u.get("investment", "N/A")
    loss = u.get("acceptable_loss", "N/A")
    await update.message.reply_text(
        f"Your settings:\nMode: {mode}\nLeverage: {leverage}\nInvestment: {investment}\nAcceptable Loss %: {loss}"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in authorized_users:
        del authorized_users[user_id]
    await update.message.reply_text("You have stopped receiving signals. Use /start to register again.")


# === Admin Commands ===

def is_admin(user_id: int) -> bool:
    return user_id in admin_user_ids


async def admin_view_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("You are not authorized to use admin commands.")
        return
    if not authorized_users:
        await update.message.reply_text("No registered users.")
        return
    lines = ["Users [id: mode, leverage, investment, loss %, banned]:"]
    for uid, data in authorized_users.items():
        lines.append(f"{uid}: {data.get('mode')}, {data.get('leverage')}, {data.get('investment')}, {data.get('acceptable_loss')}, banned={data.get('banned')}")
    await update.message.reply_text("\n".join(lines))


async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        target = int(context.args[0])
        if target in authorized_users:
            authorized_users[target]["banned"] = True
            await update.message.reply_text(f"User {target} banned.")
        else:
            await update.message.reply_text("User not found.")
    except:
        await update.message.reply_text("Invalid user ID.")


async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        target = int(context.args[0])
        if target in authorized_users:
            authorized_users[target]["banned"] = False
            await update.message.reply_text(f"User {target} unbanned.")
        else:
            await update.message.reply_text("User not found.")
    except:
        await update.message.reply_text("Invalid user ID.")


async def admin_toggle_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_enabled
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Unauthorized.")
        return
    bot_enabled = not bot_enabled
    status = "enabled" if bot_enabled else "disabled"
    await update.message.reply_text(f"Bot is now {status}.")


# === Market Scanning & Signal Dispatch Logic ===

async def market_scanner_task(app):
    global binance_client
    binance_client = BinanceFuturesClient()
    logger.info("Loading symbols for scanning...")
    symbols = await binance_client.load_usdt_futures_symbols()
    logger.info(f"Symbols loaded for scanning: {len(symbols)}")

    global bot_enabled
    global last_signals

    while True:
        if not bot_enabled:
            logger.info("Bot disabled, skipping scan cycle.")
            await asyncio.sleep(SCAN_INTERVAL)
            continue

        logger.info("Starting market scan cycle...")
        for symbol in symbols:
            try:
                df = await binance_client.fetch_ohlcv(symbol)
                if df.empty:
                    continue
                signal = calculate_ma_signal(df)
                logger.info(f"{symbol} - Signal: {signal}")

                if signal is None:
                    last_signals.pop(symbol, None)
                    continue
                if last_signals.get(symbol) == signal:
                    continue
                last_signals[symbol] = signal

                entry_price = df['close'].iloc[-1]

                # Prepare signal data for sending to users
                base_stop_loss, base_targets = calculate_trade_levels(
                    signal, entry_price, acceptable_loss_pct=1.0, tp_levels=4
                )

                # Send signals based on user mode and preferences
                for user_id, udata in authorized_users.items():
                    if udata.get('banned', False):
                        continue
                    if udata.get('mode') == 'auto':
                        # Auto mode: use default settings
                        signal_data = {
                            "symbol": symbol,
                            "signal": signal,
                            "entry": entry_price,
                            "stop_loss": base_stop_loss,
                            "targets": base_targets,
                            "timeframe": TIMEFRAME,
                            "leverage": "Auto",
                            "investment": "Auto",
                        }
                        asyncio.create_task(send_signal_to_user(app, user_id, signal_data))

                    elif udata.get('mode') == 'custom':
                        # Custom mode: requires full settings
                        if any(udata.get(k) is None for k in ['leverage', 'investment', 'acceptable_loss']):
                            continue
                        # Calculate user-specific stop loss and targets
                        stop_loss_custom, targets_custom = calculate_trade_levels(
                            signal,
                            entry_price,
                            udata['acceptable_loss'],
                            tp_levels=4
                        )
                        signal_data = {
                            "symbol": symbol,
                            "signal": signal,
                            "entry": entry_price,
                            "stop_loss": stop_loss_custom,
                            "targets": targets_custom,
                            "timeframe": TIMEFRAME,
                            "leverage": udata['leverage'],
                            "investment": udata['investment'],
                        }
                        asyncio.create_task(send_signal_to_user(app, user_id, signal_data))

                # Respect rate limit per symbol
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Error scanning symbol {symbol}: {e}")

        logger.info("Scan cycle completed, sleeping...")
        await asyncio.sleep(SCAN_INTERVAL)


# === Main Bot Setup and Launch ===

def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_CUSTOM_LEVERAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_leverage)],
            STATE_CUSTOM_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_investment)],
            STATE_CUSTOM_ACCEPTABLE_LOSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_acceptable_loss)],
        },
        fallbacks=[CommandHandler("stop", stop_command)],
        name="settings_conversation",
        persistent=False,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mode", mode_command))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))

    # Admin commands
    application.add_handler(CommandHandler("admin_view", admin_view_users))
    application.add_handler(CommandHandler("ban", admin_ban))
    application.add_handler(CommandHandler("unban", admin_unban))
    application.add_handler(CommandHandler("toggle_bot", admin_toggle_bot))

    # Launch background scanning and retry tasks
    async def on_startup(app):
        logger.info("Starting background tasks...")
        app.create_task(market_scanner_task(app))
        app.create_task(retry_failed_signals(app))

    application.post_init = on_startup

    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()