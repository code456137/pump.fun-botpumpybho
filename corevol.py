import struct
import json
import time
from dataclasses import dataclass
from typing import Optional

# Import from Solana and SPL
from solana.rpc.api import Client
from solana.rpc.commitment import Processed, Confirmed
from solana.rpc.types import TokenAccountOpts, TxOpts
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from spl.token.instructions import (
    CloseAccountParams,
    close_account,
    create_associated_token_account,
    get_associated_token_address,
)
from construct import Bytes, Flag, Int64ul, Padding, Struct

# --- 1. GLOBAL CONSTANTS (Pump.fun) ---
GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
FEE_RECIPIENT = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOC_TOKEN_ACC_PROG = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
GLOBAL_VOL_ACC = Pubkey.from_string("Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y")
FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# --- 2. TOKEN CONSTANTS (Hardcoded) ---

TARGET_MINT = Pubkey.from_string("")
TARGET_BONDING_CURVE = Pubkey.from_string("")
TARGET_ASSOC_BONDING_CURVE = Pubkey.from_string("")

# --- 3. BONDING CURVE LOGIC ---
@dataclass
class BondingCurve:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    token_total_supply: int
    complete: bool
    creator: Pubkey

def get_virtual_reserves(client: Client, bonding_curve: Pubkey):
    bonding_curve_struct = Struct(
        Padding(8),
        "virtualTokenReserves" / Int64ul,
        "virtualSolReserves" / Int64ul,
        "realTokenReserves" / Int64ul,
        "realSolReserves" / Int64ul,
        "tokenTotalSupply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32)
    )
    try:
        account_info = client.get_account_info(bonding_curve)
        if not account_info.value:
            return None
        return bonding_curve_struct.parse(account_info.value.data)
    except Exception:
        return None

def get_bonding_curve(client: Client) -> Optional[BondingCurve]:
    # We no longer need to derive, use the constant
    virtual_reserves = get_virtual_reserves(client, TARGET_BONDING_CURVE)
    if virtual_reserves is None:
        return None

    try:
        return BondingCurve(
            virtual_token_reserves=int(virtual_reserves.virtualTokenReserves),
            virtual_sol_reserves=int(virtual_reserves.virtualSolReserves),
            token_total_supply=int(virtual_reserves.tokenTotalSupply),
            complete=bool(virtual_reserves.complete),
            creator=Pubkey.from_bytes(virtual_reserves.creator)
        )
    except Exception as e:
        print(f"Error creating BondingCurve: {e}")
        return None

def sol_for_tokens(sol_spent, sol_reserves, token_reserves):
    new_sol_reserves = sol_reserves + sol_spent
    if new_sol_reserves == 0: return 0
    new_token_reserves = (sol_reserves * token_reserves) / new_sol_reserves
    token_received = token_reserves - new_token_reserves
    return round(token_received)

def tokens_for_sol(tokens_to_sell, sol_reserves, token_reserves):
    new_token_reserves = token_reserves + tokens_to_sell
    if new_token_reserves == 0: return 0
    new_sol_reserves = (sol_reserves * token_reserves) / new_token_reserves
    sol_received = sol_reserves - new_sol_reserves
    return sol_received

# --- 4. UTILITY FUNCTIONS ---
def get_token_balance(client: Client, pub_key: Pubkey) -> float | None:
    try:
        # Use TARGET_MINT directly
        response = client.get_token_accounts_by_owner_json_parsed(
            pub_key,
            TokenAccountOpts(mint=TARGET_MINT),
            commitment=Processed
        )
        accounts = response.value
        if accounts:
            token_amount = accounts[0].account.data.parsed['info']['tokenAmount']['uiAmount']
            return float(token_amount)
        return 0.0
    except Exception as e:
        print(f"Error fetching token balance: {e}")
        return None

def confirm_txn(client: Client, txn_sig: Signature, max_retries: int = 30, retry_interval: int = 2) -> bool:
    retries = 1
    while retries <= max_retries:
        try:
            time.sleep(retry_interval)
            txn_res = client.get_transaction(txn_sig, encoding="json", commitment=Confirmed, max_supported_transaction_version=0)
            if txn_res.value:
                txn_json = json.loads(txn_res.value.transaction.meta.to_json())
                if txn_json['err'] is None:
                    print(f"Transaction confirmed (Attempt: {retries})")
                    return True
                else:
                    print(f"Error in transaction: {txn_json['err']} (Attempt: {retries})")
                    return False
        except Exception:
            pass
        print(f"Waiting for confirmation... (Attempt: {retries}/{max_retries})")
        retries += 1
    print("Max retries reached. Transaction confirmation failed.")
    return False

# --- 5. TRADING FUNCTIONS (Simplified) ---
def buy(client: Client, payer_keypair: Keypair, sol_in: float = 0.01, slippage: int = 5, unit_budget: int = 100_000, unit_price: int = 1_000_000) -> bool:
    try:
        print(f"Starting buy transaction for token: {TARGET_MINT}")

        bonding_curve_data = get_bonding_curve(client)
        
        if not bonding_curve_data:
            print("Failed to retrieve bonding curve data.")
            return False

        if bonding_curve_data.complete:
            print("Warning: This token is complete and is only tradable on PumpSwap/Raydium.")
            return False

        # Use constants
        mint = TARGET_MINT
        bonding_curve = TARGET_BONDING_CURVE
        associated_bonding_curve = TARGET_ASSOC_BONDING_CURVE
        user = payer_keypair.pubkey()
        creator = bonding_curve_data.creator
        creator_vault = Pubkey.find_program_address([b'creator-vault', bytes(creator)], PUMP_FUN_PROGRAM)[0]
        user_volume_accumulator = Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PUMP_FUN_PROGRAM)[0]
        pump_fee_config_pda  = Pubkey.find_program_address([b"fee_config", bytes(PUMP_FUN_PROGRAM)], FEE_PROGRAM)[0]
                
        print("Checking or creating associated token account...")
        token_account_check = client.get_token_accounts_by_owner(payer_keypair.pubkey(), TokenAccountOpts(mint), Processed)
        
        token_account_instruction = None
        if token_account_check.value:
            associated_user = token_account_check.value[0].pubkey
            print("Found existing token account.")
        else:
            associated_user = get_associated_token_address(user, mint)
            token_account_instruction = create_associated_token_account(user, user, mint)
            print(f"Creating token account: {associated_user}")

        print("Calculating transaction amounts...")
        sol_dec = 1e9
        token_dec = 1e6
        virtual_sol_reserves = bonding_curve_data.virtual_sol_reserves / sol_dec
        virtual_token_reserves = bonding_curve_data.virtual_token_reserves / token_dec
        
        amount = sol_for_tokens(sol_in, virtual_sol_reserves, virtual_token_reserves)
        amount = int(amount * token_dec)
        
        slippage_adjustment = 1 + (slippage / 100)
        max_sol_cost = int((sol_in * slippage_adjustment) * sol_dec)
        print(f"Tokens to receive (min): {amount} | Max SOL cost: {max_sol_cost / sol_dec}")

        print("Creating swap instructions...")
        keys = [
            AccountMeta(pubkey=GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_RECIPIENT, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_user, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
            AccountMeta(pubkey=EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=GLOBAL_VOL_ACC, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_volume_accumulator, is_signer=False, is_writable=True),
            AccountMeta(pubkey=pump_fee_config_pda, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_PROGRAM, is_signer=False, is_writable=False)
        ]

        data = bytearray()
        data.extend(bytes.fromhex("66063d1201daebea"))
        data.extend(struct.pack('<Q', amount))
        data.extend(struct.pack('<Q', max_sol_cost))
        swap_instruction = Instruction(PUMP_FUN_PROGRAM, bytes(data), keys)

        instructions = [
            set_compute_unit_limit(unit_budget),
            set_compute_unit_price(unit_price),
        ]
        
        if token_account_instruction:
            instructions.append(token_account_instruction)
        instructions.append(swap_instruction)

        print("Compiling transaction message...")
        compiled_message = MessageV0.try_compile(
            payer_keypair.pubkey(),
            instructions,
            [],
            client.get_latest_blockhash().value.blockhash,
        )

        print("Sending transaction...")
        txn = VersionedTransaction(compiled_message, [payer_keypair])
        txn_sig = client.send_transaction(txn, opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)).value
        print(f"Transaction Signature: {txn_sig}")

        print("Confirming transaction...")
        confirmed = confirm_txn(client, txn_sig)
        
        print(f"Buy confirmed: {confirmed}")
        return confirmed
    except Exception as e:
        print(f"Error during buy: {e}")
        return False

def sell(client: Client, payer_keypair: Keypair, percentage: int = 100, slippage: int = 5, unit_budget: int = 100_000, unit_price: int = 1_000_000) -> bool:
    try:
        print(f"Starting sell transaction for token: {TARGET_MINT}")

        if not (1 <= percentage <= 100):
            print("Percentage must be between 1 and 100.")
            return False

        bonding_curve_data = get_bonding_curve(client)
        
        if not bonding_curve_data:
            print("Failed to retrieve bonding curve data.")
            return False

        if bonding_curve_data.complete:
            print("Warning: This token is complete and is only tradable on PumpSwap/Raydium.")
            return False

        # Use constants
        mint = TARGET_MINT
        bonding_curve = TARGET_BONDING_CURVE
        associated_bonding_curve = TARGET_ASSOC_BONDING_CURVE
        user = payer_keypair.pubkey()
        associated_user = get_associated_token_address(user, mint)
        creator = bonding_curve_data.creator
        creator_vault = Pubkey.find_program_address([b'creator-vault', bytes(creator)], PUMP_FUN_PROGRAM)[0]
        pump_fee_config_pda  = Pubkey.find_program_address([b"fee_config", bytes(PUMP_FUN_PROGRAM)], FEE_PROGRAM)[0]
        
        print("Retrieving token balance...")
        balance = get_token_balance(client, payer_keypair.pubkey()) # Mint is no longer needed here
        if balance is None or balance == 0:
            print("Zero token balance. Nothing to sell.")
            return False
        print(f"Token Balance: {balance}")
        
        token_balance_to_sell = balance * (percentage / 100)
        
        print("Calculating transaction amounts...")
        sol_dec = 1e9
        token_dec = 1e6
        
        amount_to_sell = int(token_balance_to_sell * token_dec)
        
        virtual_sol_reserves = bonding_curve_data.virtual_sol_reserves / sol_dec
        virtual_token_reserves = bonding_curve_data.virtual_token_reserves / token_dec
        
        sol_out = tokens_for_sol(token_balance_to_sell, virtual_sol_reserves, virtual_token_reserves)
        
        slippage_adjustment = 1 - (slippage / 100)
        min_sol_output = int((sol_out * slippage_adjustment) * sol_dec)
        print(f"Tokens to sell: {amount_to_sell} | Min SOL output: {min_sol_output / sol_dec}")
        
        print("Creating swap instructions...")
        keys = [
            AccountMeta(pubkey=GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_RECIPIENT, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_user, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
            AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=pump_fee_config_pda, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_PROGRAM, is_signer=False, is_writable=False)
        ]

        data = bytearray()
        data.extend(bytes.fromhex("33e685a4017f83ad"))
        data.extend(struct.pack('<Q', amount_to_sell))
        data.extend(struct.pack('<Q', min_sol_output))
        swap_instruction = Instruction(PUMP_FUN_PROGRAM, bytes(data), keys)

        instructions = [
            set_compute_unit_limit(unit_budget),
            set_compute_unit_price(unit_price),
            swap_instruction,
        ]

        if percentage == 100:
            print("Adding instruction to close token account...")
            close_account_instruction = close_account(CloseAccountParams(
                program_id=TOKEN_PROGRAM,
                account=associated_user,
                dest=user,
                owner=user
            ))
            instructions.append(close_account_instruction)

        print("Compiling transaction message...")
        compiled_message = MessageV0.try_compile(
            payer_keypair.pubkey(),
            instructions,
            [],
            client.get_latest_blockhash().value.blockhash,
        )

        print("Sending transaction...")
        txn = VersionedTransaction(compiled_message, [payer_keypair])
        txn_sig = client.send_transaction(txn, opts=TxOpts(skip_preflight=True, preflight_commitment=Processed)).value
        print(f"Transaction Signature: {txn_sig}")

        print("Confirming transaction...")
        confirmed = confirm_txn(client, txn_sig)
        
        print(f"Sell confirmed: {confirmed}")
        return confirmed

    except Exception as e:
        print(f"Error during sell: {e}")
        return False