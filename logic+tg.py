import asyncio
import logging
import json
import random
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# --- FIX 1: Correct import syntax ---
# Do not include .py in the import statement
# Assicurati che il file si chiami 'corevol.py' e sia nella stessa cartella
try:
    from corevol import (
        buy, 
        sell, 
        get_token_balance, 
        TARGET_MINT
    )
except ImportError:
    print("ERRORE: Impossibile importare 'corevol'. Assicurati che 'corevol.py' esista nella stessa cartella.")
    exit()

from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey

# --- 1. CONFIGURATION ---
TELEGRAM_TOKEN = "paste_your_token_here"
RPC_URL = "paste_your_rpc_url_here"

# --- 2. AUTOBOT ---

# --- FIX 2: Added placeholder values to prevent SyntaxError ---
# !!! DEVI CAMBIARE QUESTI VALORI !!!
AUTOBOT_GLOBAL_SLIPPAGE = 5
AUTOBOT_COMPUTE_PRICE = 1000000
AUTOBOT_COMPUTE_BUDGET = 100000

# wallet file (5 private key, one for line)
# first line is also used to manual wallet
AUTOBOT_WALLET_FILE = "paste_path_to_wallets.txt"

# amount  (random)
# !!! DEVI CAMBIARE QUESTI VALORI !!!
AUTOBOT_BUY_AMOUNT_MIN_SOL = 0.001
AUTOBOT_BUY_AMOUNT_MAX_SOL = 0.005


AUTOBOT_SELL_PERCENT = 75

# wait time for transactions in second
# !!! DEVI CAMBIARE QUESTI VALORI !!!
WAIT_TIME_BETWEEN_STEPS = 10
WAIT_TIME_AFTER_CYCLE = 60



# logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO,
    handlers=[
        logging.FileHandler("telegram_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# conversation step about manual wallet
(START, ASK_BUY_AMOUNT, ASK_SELL_PERCENTAGE) = range(3)



def load_wallets(filepath: str) -> list[Keypair]:
    """upload wallets from text file"""
    try:
        with open(filepath, 'r') as f:
            
            keys = [line.strip() for line in f.readlines() if line.strip()]
        
        if not keys:
            logger.critical(f"Error: the file {filepath} it's empty or not finded.")
            return []
            
        wallets = [Keypair.from_base58_string(k) for k in keys]
        logger.info(f"uploaded {len(wallets)} wallet from {filepath}.")
        return wallets
    except FileNotFoundError:
        logger.critical(f"FATAL ERROR: Wallet file not found at path: {filepath}")
        return []
    except Exception as e:
        logger.error(f"fatal error on uploading wallets: {e}")
        return []


try:
    logger.info("solana's RPC connection...")
    client = Client(RPC_URL)
    if not client.is_connected():
        raise Exception("failed connection to RPC")
    logger.info("Connect.")

    
    all_wallets = load_wallets(AUTOBOT_WALLET_FILE)
    
    if not all_wallets:
        logger.critical(f"impossible starting:no wallet uploaded from {AUTOBOT_WALLET_FILE}.")
        exit()

    # manual wallet is the first of the line
    manual_payer_keypair = all_wallets[0]
    logger.info(f"manual wallet uploaded: {manual_payer_keypair.pubkey()} (from first line of {AUTOBOT_WALLET_FILE})")
    
    if len(all_wallets) < 5:
        # --- FIX 3: Corrected typo in log message ("wallet." -> "wallets.") ---
        logger.warning(f"find just {len(all_wallets)} wallets.)")

except Exception as e:
    logger.critical(f"fatal error in starting: {e}")
    exit()




# --- AUTOBOT cycle (Background Task) ---
async def autobot_loop(app: Application, user_id: int):
    """automatic trading cycle."""
    logger.info(f"[Autobot] cycle started from user {user_id}.")
    bot_data = app.bot_data

    
    wallets = load_wallets(AUTOBOT_WALLET_FILE)
    if len(wallets) < 5:
        logger.error(f"[Autobot] not enough wallets for strategy (requires 5, finded {len(wallets)}). stop.")
        bot_data[f'autobot_running_{user_id}'] = False
        await app.bot.send_message(user_id, f"❌ AUTOBOT error: autobot was stopped.\nThe file '{AUTOBOT_WALLET_FILE}' needs min 5 keys.")
        return

    
    wallet_a, wallet_b, wallet_c, wallet_d, wallet_e = wallets[0], wallets[1], wallets[2], wallets[3], wallets[4]

    while bot_data.get(f'autobot_running_{user_id}', False):
        try:
            logger.info("[Autobot] --- starting cycle (9 steps) ---")

        
            async def do_buy(wallet: Keypair, name: str):
                if not bot_data.get(f'autobot_running_{user_id}', False): return False
                
                amount = round(random.uniform(AUTOBOT_BUY_AMOUNT_MIN_SOL, AUTOBOT_BUY_AMOUNT_MAX_SOL), 9)
                logger.info(f"[Autobot] phase: buy with {name} ({wallet.pubkey()}) for {amount} SOL...")
                
                success = await asyncio.to_thread(
                    buy, client, wallet, amount, AUTOBOT_GLOBAL_SLIPPAGE, 
                    AUTOBOT_COMPUTE_BUDGET, AUTOBOT_COMPUTE_PRICE
                )
                if success:
                    logger.info(f"[Autobot] ✅ buy {name} complete.")
                else:
                    logger.error(f"[Autobot] ❌ buy {name} failed.")
                
                # --- FIX 4: Corrected typo ("wating" -> "waiting") ---
                logger.info(f"[Autobot] waiting for {WAIT_TIME_BETWEEN_STEPS}s...")
                await asyncio.sleep(WAIT_TIME_BETWEEN_STEPS)
                return True

            # --- Internal helper function for selling ---
            async def do_sell(wallet: Keypair, name: str):
                if not bot_data.get(f'autobot_running_{user_id}', False): return False
                
                logger.info(f"[Autobot] Phase: Sell with {name} ({wallet.pubkey()}) of {AUTOBOT_SELL_PERCENT}%...")
                
                success = await asyncio.to_thread(
                    sell, client, wallet, AUTOBOT_SELL_PERCENT, AUTOBOT_GLOBAL_SLIPPAGE, 
                    AUTOBOT_COMPUTE_BUDGET, AUTOBOT_COMPUTE_PRICE
                )
                if success:
                    logger.info(f"[Autobot] ✅ Sell {name} complete.")
                else:
                    logger.error(f"[Autobot] ❌ Sell {name} failed.")
                
                logger.info(f"[Autobot] Waiting for {WAIT_TIME_BETWEEN_STEPS}s...")
                await asyncio.sleep(WAIT_TIME_BETWEEN_STEPS)
                return True

            # --- Sequence Execution ---
            # (The loop breaks if the user presses STOP)
            if not await do_buy(wallet_a, "Wallet A"): break # Step 1
            if not await do_buy(wallet_b, "Wallet B"): break # Step 2
            if not await do_buy(wallet_c, "Wallet C"): break # Step 3

            if not await do_sell(wallet_a, "Wallet A"): break # Step 4
            if not await do_buy(wallet_d, "Wallet D"): break # Step 5
            if not await do_sell(wallet_b, "Wallet B"): break # Step 6

            if not await do_buy(wallet_e, "Wallet E"): break # Step 7
            if not await do_sell(wallet_c, "Wallet C"): break # Step 8
            if not await do_buy(wallet_b, "Wallet A"): break # Step 9 (Re-buy)

            # End of the complete cycle
            logger.info(f"[Autobot] Full cycle complete. Waiting for {WAIT_TIME_AFTER_CYCLE}s before restarting...")
            await asyncio.sleep(WAIT_TIME_AFTER_CYCLE)

        except Exception as e:
            logger.error(f"[Autobot] Serious error in loop: {e}")
            await asyncio.sleep(10)
    
    logger.info(f"[Autobot] Loop stopped for user {user_id}.")

# --- Telegram Conversation Functions ---

def _get_start_menu_components(user_name_html: str, user_id: int, bot_data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Returns the text and keyboard of the main menu."""
    is_running = bot_data.get(f'autobot_running_{user_id}', False)
    
    text = (
        f"Hi {user_name_html}!\n\n"
        f"<b>Manual Wallet (Wallet A):</b>\n<code>{manual_payer_keypair.pubkey()}</code>\n"
        f"<b>Token:</b> <code>{TARGET_MINT}</code>\n\n"
    )
    
    if is_running:
        text += (
            f"🟢 **AUTOBOT ACTIVE** 🟢\n"
            f"Logic: 9-step sequence with 5 wallets.\n"
            f"Pause between steps: {WAIT_TIME_BETWEEN_STEPS}s | Cycle pause: {WAIT_TIME_AFTER_CYCLE}s"
        )
    else:
        text += "🔴 **AUTOBOT INACTIVE** 🔴"

    keyboard = [
        [
            InlineKeyboardButton("▶️ Start Autobot", callback_data="AUTOBOT_START"),
            InlineKeyboardButton("⏹️ Stop Autobot", callback_data="AUTOBOT_STOP"),
        ],
        [
            InlineKeyboardButton("📈 Manual Buy", callback_data="MANUAL_BUY"),
            InlineKeyboardButton("📉 Manual Sell", callback_data="MANUAL_SELL"),
        ],
        [InlineKeyboardButton("💰 Balance (Manual Wallet)", callback_data="BALANCE")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    return text, reply_markup

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/start function. Shows the main menu with buttons."""
    user = update.effective_user
    text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
    if update.message:
        await update.message.reply_html(text, reply_markup=reply_markup)
    elif update.callback_query:
        # This might be called from cancel() or other handlers returning to start
        try:
            await update.callback_query.message.edit_text(text, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Failed to edit message in start_command: {e}")
            # If editing fails (e.g., message unchanged), just ignore
            pass
    return START 

async def button_press(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses from the menu."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    # --- Autobot Logic ---
    if query.data == "AUTOBOT_START":
        user_id = user.id
        if context.bot_data.get(f'autobot_running_{user_id}', False):
            await query.edit_message_text("Autobot is already active.", reply_markup=query.message.reply_markup)
        else:
            logger.info(f"Autobot start requested by user {user_id}")
            context.bot_data[f'autobot_running_{user_id}'] = True
            asyncio.create_task(autobot_loop(context.application, user_id))
            
            text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        return START

    elif query.data == "AUTOBOT_STOP":
        user_id = user.id
        if not context.bot_data.get(f'autobot_running_{user_id}', False):
            await query.edit_message_text("Autobot is already stopped.", reply_markup=query.message.reply_markup)
        else:
            logger.info(f"Autobot stop requested by user {user_id}")
            context.bot_data[f'autobot_running_{user_id}'] = False
            
            text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        return START
    
    # --- Manual Logic ---
    elif query.data == "MANUAL_BUY":
        await query.edit_message_text(text="How much SOL do you want to use to buy (with the manual wallet)?\n\n(e.g. `0.01`)", parse_mode='MarkdownV2')
        return ASK_BUY_AMOUNT  

    elif query.data == "MANUAL_SELL":
        await query.edit_message_text(text="What percentage (%) of your balance (from the manual wallet) do you want to sell?\n\n(e.g. `100`)")
        return ASK_SELL_PERCENTAGE
    
    elif query.data == "BALANCE":
        await query.edit_message_text(text="Checking MANUAL wallet balances...")
        
        try:
            # The 'manual_payer_keypair' variable is global and set at startup
            def get_balances_sync():
                sol_balance_lamports = client.get_balance(manual_payer_keypair.pubkey()).value
                sol_balance = sol_balance_lamports / 1e9
                token_balance = get_token_balance(client, manual_payer_keypair.pubkey())
                return sol_balance, token_balance

            sol_balance, token_balance = await asyncio.to_thread(get_balances_sync)
            
            balance_text = (
                f"<b>Manual Wallet Balances:</b>\n"
                f"<code>{manual_payer_keypair.pubkey()}</code>\n\n"
                f"<b>SOL:</b> {sol_balance:.6f}\n"
                f"<b>Token:</b> {token_balance} (<code>{TARGET_MINT}</code>)"
            )
            await query.edit_message_text(balance_text, parse_mode='HTML')
            
            # Wait 3 seconds, then show main menu again
            await asyncio.sleep(3) 
            text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
            # Check if the message is still the balance text before editing back
            current_message = await context.bot.get_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
            if current_message.text == balance_text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""):
                 await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
            
        except Exception as e:
            logger.error(f"Error in /balance: {e}")
            await query.edit_message_text(f"Error retrieving balances: {e}")
            await asyncio.sleep(3)
            # Go back to main menu even on error
            text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        
        return START 

    return START

async def handle_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the message with the amount to buy (MANUAL)."""
    user = update.effective_user
    try:
        sol_in = float(update.message.text)
        if sol_in <= 0:
            await update.message.reply_text("The amount must be a positive number. Try again.")
            return ASK_BUY_AMOUNT 

        await update.message.reply_text(f"Sending manual order: Buying for {sol_in} SOL...")

        success = await asyncio.to_thread(
            buy,
            client=client,
            payer_keypair=manual_payer_keypair, # Use the MANUAL wallet
            sol_in=sol_in,
            slippage=AUTOBOT_GLOBAL_SLIPPAGE,
            unit_budget=AUTOBOT_COMPUTE_BUDGET,
            unit_price=AUTOBOT_COMPUTE_PRICE
        )

        if success:
            await update.message.reply_text("✅ Manual buy complete!")
        else:
            await update.message.reply_text("❌ Manual buy failed. Check logs.")
    
    except ValueError:
        await update.message.reply_text("Error: The amount must be a number.\ne.g. `0.01`. Try again.")
        return ASK_BUY_AMOUNT 
    except Exception as e:
        logger.error(f"Error in manual buy: {e}")
        await update.message.reply_text(f"Unexpected error: {e}")

    # Show main menu again
    text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
    await update.message.reply_html(text, reply_markup=reply_markup)
    return ConversationHandler.END 

async def handle_sell_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the message with the percentage to sell (MANUAL)."""
    user = update.effective_user
    try:
        percentage = int(update.message.text)
        if not (1 <= percentage <= 100):
            await update.message.reply_text("The percentage must be a number between 1 and 100. Try again.")
            return ASK_SELL_PERCENTAGE 

        await update.message.reply_text(f"Sending manual order: Selling {percentage}% of tokens...")

        success = await asyncio.to_thread(
            sell,
            client=client,
            payer_keypair=manual_payer_keypair, # Use the MANUAL wallet
            percentage=percentage,
            slippage=AUTOBOT_GLOBAL_SLIPPAGE,
            unit_budget=AUTOBOT_COMPUTE_BUDGET,
            unit_price=AUTOBOT_COMPUTE_PRICE
        )

        if success:
            await update.message.reply_text("✅ Manual sell complete!")
        else:
            await update.message.reply_text("❌ Manual sell failed. Check logs.")

    except ValueError:
        await update.message.reply_text("Error: The percentage must be an integer.\ne.g. `100`. Try again.")
        return ASK_SELL_PERCENTAGE 
    except Exception as e:
        logger.error(f"Error in manual sell: {e}")
        await update.message.reply_text(f"Unexpected error: {e}")

    # Show main menu again
    text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
    await update.message.reply_html(text, reply_markup=reply_markup)
    return ConversationHandler.END 

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current operation and returns to the menu."""
    user = update.effective_user
    await update.message.reply_text("Action cancelled.")
    # Show main menu again
    text, reply_markup = _get_start_menu_components(user.mention_html(), user.id, context.bot_data)
    await update.message.reply_html(text, reply_markup=reply_markup)
    return ConversationHandler.END

# --- Main startup function ---

def main() -> None:
    """Starts the bot."""
    # Check for placeholder values
    if "paste_your" in TELEGRAM_TOKEN or "paste_your" in RPC_URL or "paste_path" in AUTOBOT_WALLET_FILE:
        logger.critical("FATAL ERROR: Devi impostare TELEGRAM_TOKEN, RPC_URL, e AUTOBOT_WALLET_FILE nel file .py!")
        return

    logger.info("Starting Telegram bot...")
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            START: [
                CallbackQueryHandler(button_press), 
            ],
            ASK_BUY_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buy_amount),
            ],
            ASK_SELL_PERCENTAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sell_percentage),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command), CommandHandler("start", start_command)],
    )

    application.add_handler(conv_handler)
    # Add start handler separately as well in case the conversation breaks
    application.add_handler(CommandHandler("start", start_command))

    logger.info("Bot started. Polling...")
    application.run_polling()


if __name__ == "__main__":
    main()